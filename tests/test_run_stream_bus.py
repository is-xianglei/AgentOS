"""RunStreamBus:offset 跨执行连续、并发 emit 的顺序、租约丢失后停写、projection 与裁剪后重发快照。"""

import asyncio
from uuid import uuid4

import pytest

from core.errors import LeaseLostError
from core.event_bus import RunStreamBus
from core.events import ORCHESTRATOR_ACTOR, BlockType, RunEvent, StreamEvent, subagent_actor
from core.run_state import TurnState, fold_all, restore
from runtime import turn_follow
from session.service import TurnExecutionState

pytestmark = pytest.mark.anyio

_SID = 3
_TURN = "turn-bus"
_LEAD = ORCHESTRATOR_ACTOR


class _Counter:
    """替身 session_turns.last_event_offset:跨执行共享,只增不减。"""

    def __init__(self) -> None:
        self.last = 0

    def reserve(self, count: int) -> int:
        first = self.last + 1
        self.last += count
        return first

    def take_one(self) -> int:
        return self.reserve(1)


class _Offsets:
    """替身 TurnOffsets:每次预留 4 个;lose_after 次后抛 LeaseLostError。"""

    def __init__(self, counter: _Counter, *, lose_after: int | None = None) -> None:
        self._counter = counter
        self._next = 0
        self._remaining = 0
        self._taken = 0
        self._lose_after = lose_after

    async def take(self) -> int:
        if self._lose_after is not None and self._taken >= self._lose_after:
            raise LeaseLostError("执行租约已失效")
        if self._remaining == 0:
            self._next, self._remaining = self._counter.reserve(4), 4
        self._taken += 1
        self._remaining -= 1
        self._next += 1
        return self._next - 1


async def _events(store) -> list[dict]:
    return [event for _, event in (await store.capture(_TURN)).entries]


async def test_offsets_continue_across_executions(run_store_factory) -> None:
    store = run_store_factory()
    counter = _Counter()
    first = RunStreamBus(store, _TURN, _Offsets(counter), TurnState())
    await first.emit(RunEvent.run_started(_TURN, _SID))
    await first.emit(StreamEvent.turn_start(_LEAD, _SID))
    await first.close()
    await first.write_terminal(RunEvent.run_end(_TURN, "awaiting_interaction"), counter.take_one())

    capture = await store.capture(_TURN)
    resumed = restore(capture.projection, [event for _, event in capture.entries])
    second = RunStreamBus(store, _TURN, _Offsets(counter), resumed)
    await second.emit(RunEvent.run_started(_TURN, _SID))
    await second.close()
    await second.write_terminal(RunEvent.run_end(_TURN, "completed"), counter.take_one())

    sequences = [event["sequence"] for event in await _events(store)]
    assert sequences == sorted(sequences) and len(set(sequences)) == len(sequences)
    assert sequences[:3] == [1, 2, 5]  # 第一次执行预留未用的 3、4 不复用
    assert sequences[-1] == counter.last
    assert second.state.status == "completed" and second.state.execution_start_offset == 6


async def test_concurrent_emit_keeps_stream_order_equal_to_offset(run_store_factory) -> None:
    store = run_store_factory()
    bus = RunStreamBus(store, _TURN, _Offsets(_Counter()), TurnState())

    async def _member(run_id: str) -> None:
        actor = subagent_actor("general-purpose", run_id=run_id)
        for index in range(20):
            await bus.emit(StreamEvent.block_stop(actor, _SID, index))
            await asyncio.sleep(0)

    await asyncio.gather(*(_member(f"run-{i}") for i in range(4)))
    await bus.close()
    sequences = [event["sequence"] for event in await _events(store)]

    assert sequences == list(range(1, 81))


async def test_delta_coalescing_and_timer_flush(run_store_factory) -> None:
    store = run_store_factory()
    bus = RunStreamBus(store, _TURN, _Offsets(_Counter()), TurnState())
    for part in "一二三四五":
        await bus.emit(StreamEvent.text_delta(_LEAD, _SID, 0, part))
    await asyncio.sleep(0.2)  # 不调用 close,靠计时器刷出缓冲尾巴
    events = await _events(store)

    assert len(events) < 5
    assert "".join(event["text"] for event in events) == "一二三四五"
    await bus.close()


async def test_lease_lost_stops_writes_and_notifies(run_store_factory) -> None:
    store = run_store_factory()
    lost: list[bool] = []
    bus = RunStreamBus(
        store,
        _TURN,
        _Offsets(_Counter(), lose_after=3),
        TurnState(),
        on_lease_lost=lambda: lost.append(True),
    )
    for index in range(6):
        await bus.emit(StreamEvent.block_stop(_LEAD, _SID, index))
    await bus.close()

    assert [event["sequence"] for event in await _events(store)] == [1, 2, 3]
    assert lost == [True] and bus.lease_lost is True


async def test_projection_checkpoint_on_key_events(run_store_factory) -> None:
    store = run_store_factory()
    bus = RunStreamBus(store, _TURN, _Offsets(_Counter()), TurnState())
    await bus.emit(RunEvent.run_started(_TURN, _SID))
    await bus.emit(StreamEvent.block_stop(_LEAD, _SID, 0))
    capture = await store.capture(_TURN)

    assert capture.projection is not None and capture.projection["last_offset"] == 1
    assert restore(capture.projection, [e for _, e in capture.entries]) == bus.state


async def test_subscription_resends_snapshot_after_maxlen_trim(
    run_store_factory, monkeypatch: pytest.MonkeyPatch
) -> None:
    """MAXLEN 调小后,订阅方游标被裁剪越过,重发快照仍能还原写入方的最终状态。"""
    store = run_store_factory(maxlen=128)
    counter = _Counter()
    turn_id = uuid4()
    bus = RunStreamBus(store, str(turn_id), _Offsets(counter), TurnState())
    finished = False

    async def _execution(_turn_id):
        return TurnExecutionState(turn_id, _SID, "completed", not finished, counter.last)

    monkeypatch.setattr(turn_follow, "get_run_event_store", lambda: store)
    monkeypatch.setattr(turn_follow, "_execution", _execution)
    monkeypatch.setattr(turn_follow, "STREAM_BLOCK_MS", 200)
    await bus.emit(RunEvent.run_started(str(turn_id), _SID))
    await bus.emit(StreamEvent.turn_start(_LEAD, _SID))
    subscriber = turn_follow.subscribe_turn(turn_id)
    first = await anext(subscriber)

    # 订阅方停在快照游标处不读,写入方写到远超 MAXLEN,Stream 头部越过游标。
    for i in range(600):
        await bus.emit(StreamEvent.block_start(_LEAD, _SID, i, BlockType.TEXT))
        await bus.emit(StreamEvent.text_delta(_LEAD, _SID, i, f"段{i}"))
    await bus.close()
    await bus.write_terminal(RunEvent.run_end(str(turn_id), "completed"), counter.take_one())
    finished = True

    frames = [first, *[frame async for frame in subscriber]]

    assert first["type"] == "run_snapshot"
    resent = [i for i, frame in enumerate(frames) if frame["type"] == "run_snapshot"]
    assert len(resent) >= 2
    rebuilt = fold_all(TurnState.from_dict(frames[resent[-1]]["state"]), frames[resent[-1] + 1 :])
    assert rebuilt == bus.state
    assert len(bus.state.actors[0].blocks) == 600
    assert frames[-1]["type"] == "run_end"
