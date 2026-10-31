"""SSE 跟随:流式接口从本次执行起点跟随,订阅接口先发快照再跟随,读到 run_end 结束。

两条路径共用 _tail:XREAD BLOCK 等新事件;Stream 头部被裁剪越过游标时重发 run_snapshot;
没有执行中的任务且迟迟等不到 run_end(写入方与回收方都失败)时,按数据库状态合成 run_end。
"""

import logging
from collections.abc import AsyncIterator
from typing import Any
from uuid import UUID

from core.events import RunEvent
from core.run_events import (
    START_CURSOR,
    STREAM_BLOCK_MS,
    RunEventStore,
    cursor_key,
    get_run_event_store,
)
from core.run_state import TurnState, restore, snapshot_frame
from runtime.execution import ExecutionHandle, run_end_status, run_in_session
from session.service import TurnExecutionState

logger = logging.getLogger(__name__)

# 无执行中任务时最多空等几轮阻塞读(每轮 STREAM_BLOCK_MS),覆盖取消方等待本地任务收尾的窗口。
_ORPHAN_IDLE_POLLS = 3


async def _execution(turn_id: UUID) -> TurnExecutionState | None:
    return await run_in_session(lambda service: service.get_turn_execution(turn_id))


def _synth_run_end(
    turn_id: UUID,
    execution: TurnExecutionState | None,
    sequence: int,
) -> dict[str, Any]:
    status = run_end_status(execution.status) if execution else "interrupted"
    event = RunEvent.run_end(str(turn_id), status)
    event.sequence = execution.last_event_offset if execution else sequence
    return event.to_dict()


async def _capture_state(store: RunEventStore, turn_id: UUID) -> tuple[TurnState, str, bool]:
    capture = await store.capture(str(turn_id))
    state = restore(capture.projection, [event for _, event in capture.entries])
    return state, capture.tail_cursor, capture.exists


async def _trimmed_past(store: RunEventStore, turn_id: UUID, cursor: str) -> bool:
    """游标所指 entry 已被裁剪:Stream 首条比游标还新,中间可能丢了事件。"""
    if cursor == START_CURSOR:
        return False
    first = await store.first_cursor(str(turn_id))
    return first is not None and cursor_key(first) > cursor_key(cursor)


async def _tail(
    store: RunEventStore,
    turn_id: UUID,
    cursor: str,
    last_seq: int,
    ended: dict[str, Any] | None = None,
) -> AsyncIterator[dict[str, Any]]:
    """ended 是已折叠进快照、但尚未下发的最近一次 run_end(订阅时执行方正在清租约)。"""
    idle_polls = 0
    while True:
        batch = await store.wait(str(turn_id), cursor, timeout_ms=STREAM_BLOCK_MS)
        if not batch:
            execution = await _execution(turn_id)
            if execution is not None and execution.executing:
                idle_polls = 0
                continue
            if ended is not None:
                yield ended
                return
            idle_polls += 1
            if idle_polls >= _ORPHAN_IDLE_POLLS:
                yield _synth_run_end(turn_id, execution, last_seq)
                return
            continue
        idle_polls = 0
        if await _trimmed_past(store, turn_id, cursor):
            state, cursor, _ = await _capture_state(store, turn_id)
            last_seq = state.last_offset
            ended = state.run_end
            yield snapshot_frame(str(turn_id), state)
            continue
        for entry_cursor, event in batch:
            cursor = entry_cursor
            sequence = event.get("sequence")
            # 迟到或重复的事件(sequence 不大于已发出的)不再下发,与 reducer 的折叠规则一致。
            if not isinstance(sequence, int) or sequence <= last_seq:
                continue
            last_seq = sequence
            if event.get("type") == "run_started":
                ended = None
            yield event
            if event.get("type") == "run_end":
                return


async def follow_execution(handle: ExecutionHandle) -> AsyncIterator[dict[str, Any]]:
    """流式接口:从本次执行起点跟随;第一帧是带 turn_id 的 run_started。"""
    store = get_run_event_store()
    async for event in _tail(store, handle.turn_id, handle.start_cursor, handle.start_offset):
        yield event


async def subscribe_turn(turn_id: UUID) -> AsyncIterator[dict[str, Any]]:
    """订阅接口:先发 run_snapshot,再跟随;已无执行中任务且已有 run_end 时补发后结束。"""
    store = get_run_event_store()
    state, cursor, exists = await _capture_state(store, turn_id)
    execution = await _execution(turn_id)
    executing = execution is not None and execution.executing
    if not exists and not executing:
        # key 已过期:明确告知客户端改用消息历史接口加载。
        status = run_end_status(execution.status) if execution else "interrupted"
        event = RunEvent.run_end(str(turn_id), status, expired=True)
        event.sequence = execution.last_event_offset if execution else 0
        yield event.to_dict()
        return
    yield snapshot_frame(str(turn_id), state)
    if state.run_end is not None and not executing:
        yield state.run_end
        return
    async for event in _tail(store, turn_id, cursor, state.last_offset, state.run_end):
        yield event
