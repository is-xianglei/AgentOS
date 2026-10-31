"""流式接口与订阅接口的 HTTP 层测试:真实路由函数、执行包装、事件总线与本地事件流后端。

租约表用内存替身(租约 SQL 语义由 test_turn_lease_pg 覆盖),LLM 用脚本化的执行体替代。
"""

import asyncio
import json
from dataclasses import dataclass, field
from types import SimpleNamespace
from typing import Any
from uuid import UUID, uuid4

import pytest

from core.errors import AgentException, LeaseLostError
from core.events import ORCHESTRATOR_ACTOR, BlockType, RuntimeEvent, StreamEvent, Usage
from core.run_events import LocalRunEventStore
from core.run_state import TurnState, fold_all
from core.sse import HEARTBEAT_FRAME, with_heartbeat
from runtime import execution, turn_follow
from session import api as session_api
from session.schemas import SessionSendMessageRequest
from session.service import SessionService, TurnExecutionState, TurnTermination

pytestmark = pytest.mark.anyio

_USER = SimpleNamespace(id=1)
_WS = 1
_LEAD = ORCHESTRATOR_ACTOR


@dataclass
class _Turn:
    session_id: int
    status: str = "running"
    owner: str | None = None
    offset: int = 0


@dataclass
class _Leases:
    turns: dict[UUID, _Turn] = field(default_factory=dict)
    sessions: dict[int, str] = field(default_factory=dict)
    # 取消收尾时补写的半截回答,按轮次记录。
    partials: dict[UUID, list[Any]] = field(default_factory=dict)


class _FakeDb:
    def __init__(self) -> None:
        self.commits = 0

    async def commit(self) -> None:
        self.commits += 1

    async def rollback(self) -> None:
        return None


class _Scope:
    async def __aenter__(self) -> _FakeDb:
        return _FakeDb()

    async def __aexit__(self, *exc_info: object) -> bool:
        return False


def _make_service(leases: _Leases) -> type:
    """内存版 SessionService:只实现执行包装、跟随与新路由用到的方法。"""

    class _Service:
        def __init__(self, db: object) -> None:
            self.db = db

        async def require_read_access(self, session_id, user_id, workspace_id):
            return SimpleNamespace(id=session_id)

        require_write_access = require_read_access
        resolve_effective_workflow_id = staticmethod(SessionService.resolve_effective_workflow_id)

        async def claim_turn_lease(self, turn_id, owner, lease_seconds):
            turn = leases.turns[turn_id]
            if turn.owner is not None:
                raise AgentException.message("该轮次已有执行中的任务", status_code=409)
            turn.owner = owner
            return turn.offset

        async def reserve_turn_offsets(self, turn_id, owner, count):
            turn = leases.turns[turn_id]
            if turn.owner != owner:
                raise LeaseLostError("执行租约已失效")
            first = turn.offset + 1
            turn.offset += count
            return first

        async def verify_turn_lease(self, turn_id, owner):
            if leases.turns[turn_id].owner != owner:
                raise LeaseLostError("执行租约已失效")

        async def renew_turn_lease(self, turn_id, owner, lease_seconds):
            return leases.turns[turn_id].owner == owner

        async def finish_turn_execution(
            self,
            session_id,
            turn_id,
            owner,
            *,
            partial=None,
            interrupted_session_status="failed",
        ):
            await self.verify_turn_lease(turn_id, owner)
            turn = leases.turns[turn_id]
            if turn.status == "running":
                leases.partials[turn_id] = list(partial or [])
                turn.status = "interrupted"
                leases.sessions[session_id] = interrupted_session_status
            turn.offset += 1
            return TurnTermination(
                turn_id, session_id, turn.status, leases.sessions[session_id], turn.offset
            )

        async def release_turn_lease(self, turn_id, owner):
            if leases.turns[turn_id].owner == owner:
                leases.turns[turn_id].owner = None

        async def get_turn_execution(self, turn_id):
            turn = leases.turns.get(turn_id)
            if turn is None:
                return None
            return TurnExecutionState(
                turn_id, turn.session_id, turn.status, turn.owner is not None, turn.offset
            )

        async def get_session_turn_execution(self, session_id, turn_id):
            state = await self.get_turn_execution(turn_id)
            if state is None or state.session_id != session_id:
                raise AgentException.message("交互轮次不存在", status_code=404)
            return state

        async def get_active_turn(self, session_id):
            for turn_id in reversed(list(leases.turns)):
                if leases.turns[turn_id].session_id == session_id:
                    return await self.get_turn_execution(turn_id)
            return None

        async def cancel_turn_execution(self, session_id, turn_id, *, partial_loader=None):
            turn = leases.turns[turn_id]
            if turn.owner is None:
                raise AgentException.message("该轮次没有执行中的任务", status_code=409)
            if turn.status == "awaiting_interaction":
                raise AgentException.message("请使用交互取消接口", status_code=409)
            if turn.status == "running" and partial_loader is not None:
                leases.partials[turn_id] = await partial_loader()
            turn.owner = None
            turn.offset += 1
            if turn.status == "running":
                turn.status = "interrupted"
                leases.sessions[session_id] = "idle"
            return TurnTermination(
                turn_id, session_id, turn.status, leases.sessions[session_id], turn.offset
            )

    return _Service


@dataclass
class _Env:
    leases: _Leases
    store: LocalRunEventStore
    gate: asyncio.Event
    reached_gate: asyncio.Event


@pytest.fixture
async def env(monkeypatch: pytest.MonkeyPatch):
    leases = _Leases()
    store = LocalRunEventStore(maxlen=5000)
    service = _make_service(leases)
    monkeypatch.setattr(execution, "AsyncSessionLocal", _Scope)
    monkeypatch.setattr(execution, "SessionService", service)
    monkeypatch.setattr(session_api, "SessionService", service)
    monkeypatch.setattr(execution, "get_run_event_store", lambda: store)
    monkeypatch.setattr(turn_follow, "get_run_event_store", lambda: store)
    monkeypatch.setattr(turn_follow, "STREAM_BLOCK_MS", 100)
    environment = _Env(leases, store, asyncio.Event(), asyncio.Event())
    monkeypatch.setattr(session_api, "AgentRuntime", _scripted_runtime(environment))
    yield environment
    await execution.shutdown_executions(timeout=1)


def _scripted_runtime(env: _Env) -> type:
    """替身 AgentRuntime:start 走真实准入顺序(建轮次 → 认领 → 提交 → 启动后台执行)。"""

    class _Runtime:
        def __init__(self, db, user_id, workspace_id) -> None:
            self.db = db

        async def start(self, session_id, content, **kwargs) -> execution.ExecutionHandle:
            sid = session_id or 42
            turn_id = uuid4()
            env.leases.turns[turn_id] = _Turn(session_id=sid)
            env.leases.sessions[sid] = "running"
            owner = execution.new_lease_owner()
            service = execution.SessionService(self.db)
            offset = await service.claim_turn_lease(turn_id, owner, 90)
            await self.db.commit()

            async def body(bus) -> None:
                await bus.emit(RuntimeEvent.session_ready(sid, "标题", "running"))
                await bus.emit(StreamEvent.turn_start(_LEAD, sid))
                await bus.emit(StreamEvent.block_start(_LEAD, sid, 0, BlockType.TEXT))
                await bus.emit(StreamEvent.text_delta(_LEAD, sid, 0, "第一段"))
                env.reached_gate.set()
                await env.gate.wait()
                await bus.emit(StreamEvent.text_delta(_LEAD, sid, 0, "第二段"))
                await bus.emit(StreamEvent.block_stop(_LEAD, sid, 0))
                await bus.emit(StreamEvent.turn_end(_LEAD, sid, "end_turn", Usage()))
                env.leases.turns[turn_id].status = "completed"
                env.leases.sessions[sid] = "idle"

            return await execution.launch_turn_execution(
                turn_id=turn_id, session_id=sid, owner=owner, start_offset=offset, body=body
            )

    return _Runtime


def _parse(chunk: str) -> dict[str, Any] | None:
    if chunk == HEARTBEAT_FRAME:
        return None
    assert chunk.startswith("data: ") and chunk.endswith("\n\n")
    return json.loads(chunk[len("data: ") : -2])


async def _frames(iterator, *, until: str = "run_end") -> list[dict[str, Any]]:
    frames: list[dict[str, Any]] = []
    async for chunk in iterator:
        frame = _parse(chunk)
        if frame is None:
            continue
        frames.append(frame)
        if frame["type"] == until:
            break
    return frames


async def _send() -> Any:
    payload = SessionSendMessageRequest(content="你好")
    return await session_api.send_message(payload, _FakeDb(), _USER, _WS)


async def _wait_done(turn_id: UUID) -> None:
    task = execution.local_execution_task(turn_id)
    if task is not None:
        await asyncio.wait({task}, timeout=3)

_REQUEST = SimpleNamespace(state=SimpleNamespace(request_id="req-test"))


async def test_post_first_frame_has_turn_id_and_ends_with_run_end(env: _Env) -> None:
    env.gate.set()
    response = await _send()
    frames = await _frames(response.body_iterator)

    assert response.headers["cache-control"] == "no-cache"
    assert response.headers["x-accel-buffering"] == "no"
    first, last = frames[0], frames[-1]
    assert first["type"] == "run_started" and UUID(first["turn_id"]) in env.leases.turns
    assert [f["type"] for f in frames[1:3]] == ["session_ready", "turn_start"]
    assert (last["type"], last["status"]) == ("run_end", "completed")
    assert last["turn_id"] == first["turn_id"]
    sequences = [frame["sequence"] for frame in frames]
    assert sequences == sorted(sequences)
    text = "".join(f.get("text", "") for f in frames if f["type"] == "text_delta")
    assert text == "第一段第二段"


async def test_client_disconnect_does_not_cancel_execution(env: _Env) -> None:
    response = await _send()
    iterator = response.body_iterator
    first = _parse(await anext(iterator))
    await env.reached_gate.wait()
    await iterator.aclose()  # 客户端断开:只停止跟随

    env.gate.set()
    turn_id = UUID(first["turn_id"])
    await _wait_done(turn_id)

    entries = [event for _, event in (await env.store.capture(str(turn_id))).entries]
    assert entries[-1]["type"] == "run_end" and entries[-1]["status"] == "completed"
    assert env.leases.turns[turn_id].owner is None
    assert env.leases.turns[turn_id].status == "completed"


async def test_subscribe_mid_run_gets_snapshot_then_follows(env: _Env) -> None:
    response = await _send()
    first = _parse(await anext(response.body_iterator))
    turn_id = UUID(first["turn_id"])
    await env.reached_gate.wait()

    subscription = await session_api.subscribe_turn_events(42, turn_id, _FakeDb(), _USER, _WS)
    sub = subscription.body_iterator
    snapshot = _parse(await anext(sub))
    assert snapshot["type"] == "run_snapshot" and snapshot["state"]["status"] == "running"
    assert snapshot["state"]["actors"][0]["blocks"][0]["text"] == "第一段"

    env.gate.set()
    rest = await _frames(sub)
    final = fold_all(TurnState.from_dict(snapshot["state"]), rest)
    assert rest[-1]["type"] == "run_end"
    assert final.status == "completed"
    assert final.actors[0].blocks[0].text == "第一段第二段"
    await _frames(response.body_iterator)


async def test_subscribe_after_completion_returns_snapshot_and_run_end(env: _Env) -> None:
    env.gate.set()
    frames = await _frames((await _send()).body_iterator)
    turn_id = UUID(frames[0]["turn_id"])
    await _wait_done(turn_id)

    subscription = await session_api.subscribe_turn_events(42, turn_id, _FakeDb(), _USER, _WS)
    replay = await _frames(subscription.body_iterator)

    assert [frame["type"] for frame in replay] == ["run_snapshot", "run_end"]
    assert replay[0]["state"]["status"] == "completed"
    assert replay[0]["state"]["actors"][0]["blocks"][0]["text"] == "第一段第二段"
    assert replay[1] == frames[-1]


async def test_subscribe_expired_key_returns_expired_run_end(env: _Env) -> None:
    turn_id = uuid4()
    env.leases.turns[turn_id] = _Turn(session_id=42, status="completed", offset=17)

    subscription = await session_api.subscribe_turn_events(42, turn_id, _FakeDb(), _USER, _WS)
    frames = await _frames(subscription.body_iterator)

    assert frames == [
        {
            "type": "run_end",
            "turn_id": str(turn_id),
            "status": "completed",
            "expired": True,
            "sequence": 17,
        }
    ]
    with pytest.raises(AgentException) as missing:
        await session_api.subscribe_turn_events(43, turn_id, _FakeDb(), _USER, _WS)
    assert missing.value.status_code == 404


async def test_active_turn_and_cancel(env: _Env) -> None:
    response = await _send()
    first = _parse(await anext(response.body_iterator))
    turn_id = UUID(first["turn_id"])
    await env.reached_gate.wait()

    active = await session_api.get_active_turn(42, _REQUEST, _FakeDb(), _USER, _WS)
    assert active["data"].turn_id == turn_id and active["data"].executing is True

    cancelled = await session_api.cancel_turn_execution(
        42, turn_id, _REQUEST, _FakeDb(), _USER, _WS
    )
    assert (cancelled["data"].status, cancelled["data"].session_status) == ("interrupted", "idle")
    # 本进程任务自己收尾:半截回答与 interrupted/idle 同一次 finish_turn_execution 提交。
    assert [(d.role, d.content) for d in env.leases.partials[turn_id]] == [
        ("assistant", [{"kind": "text", "text": "第一段"}])
    ]

    rest = await _frames(response.body_iterator)
    assert rest[-1]["type"] == "run_end"
    assert rest[-1]["status"] == "interrupted" and rest[-1]["cancelled"] is True
    task = execution.local_execution_task(turn_id)
    assert task is None or task.done()
    after = await session_api.get_active_turn(42, _REQUEST, _FakeDb(), _USER, _WS)
    assert after["data"].executing is False and after["data"].status == "interrupted"
    with pytest.raises(AgentException) as again:
        await session_api.cancel_turn_execution(42, turn_id, _REQUEST, _FakeDb(), _USER, _WS)
    assert again.value.status_code == 409


async def test_cancel_awaiting_interaction_turn_is_409(env: _Env) -> None:
    turn_id = uuid4()
    env.leases.turns[turn_id] = _Turn(session_id=42, status="awaiting_interaction", owner="x")
    env.leases.sessions[42] = "awaiting_interaction"

    with pytest.raises(AgentException) as exc_info:
        await session_api.cancel_turn_execution(42, turn_id, _REQUEST, _FakeDb(), _USER, _WS)
    assert exc_info.value.status_code == 409
    assert env.leases.turns[turn_id].owner == "x"


async def test_heartbeat_and_stream_error_frames() -> None:
    async def _slow():
        await asyncio.sleep(0.35)
        yield {"type": "run_end"}

    async def _broken():
        raise RuntimeError("redis down")
        yield {}

    slow = [chunk async for chunk in with_heartbeat(_slow(), heartbeat_seconds=0.1)]
    broken = [chunk async for chunk in with_heartbeat(_broken(), heartbeat_seconds=0.1)]

    assert slow[0] == HEARTBEAT_FRAME == ": heartbeat\n\n"
    assert _parse(slow[-1]) == {"type": "run_end"}
    assert _parse(broken[-1])["type"] == "stream_error"
