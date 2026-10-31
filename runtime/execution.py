"""后台执行:租约、心跳、offset 预留、run_end 收尾、进程内注册表与取消/关闭。

生命周期:请求阶段完成准入并提交(轮次与租约同一事务)→ launch_turn_execution 启动后台任务
→ 任务用独立 AsyncSessionLocal 执行 body,事件经 RunStreamBus 写入事件流 → 收尾时
刷出合并缓冲、在租约内为 run_end 分配 offset 并写入、设置 EXPIRE、最后清除租约。

谁清除租约谁负责终态与 run_end:正常结束、本进程的用户取消与进程关闭由持有者自己写
(取消时连同半截回答一起提交);跨进程取消、本地收尾超时与孤儿回收由清除方补写,
持有者发现租约丢失后即被隔离,不再写入任何消息、状态或事件。
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import socket
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import TypeVar
from uuid import UUID, uuid4

from sqlalchemy.ext.asyncio import AsyncSession

from core.config import settings
from core.errors import AgentException, LeaseLostError
from core.event_bus import RunStreamBus
from core.events import RunEvent
from core.run_events import START_CURSOR, get_run_event_store
from core.run_state import TurnState, restore
from database.engine import AsyncSessionLocal
from runtime.cancel_partial import extract_unsaved_messages
from session.service import MessageDraft, SessionService, TurnTermination

logger = logging.getLogger(__name__)

T = TypeVar("T")

# 每次向数据库预留的 offset 个数,预留本身即一次带租约校验的写入。
OFFSET_RESERVATION = 64
# 跨进程取消的可见性间隔:两次预留之间至多每隔这么久只读校验一次租约归属。
CANCEL_VISIBILITY_SECONDS = 0.25
# 取消或关闭时等待本进程执行任务收尾的上限。
LOCAL_STOP_TIMEOUT_SECONDS = 5.0
# 本进程主动停止执行的原因:用户取消后会话回到 idle,进程关闭后会话标 failed。
STOP_USER_CANCEL = "user_cancel"
STOP_SHUTDOWN = "shutdown"

# 进程实例 ID 每次启动重新生成;租约持有者在其后再加执行 ID,隔离同进程的前后两次执行。
PROCESS_INSTANCE_ID = (
    f"{settings.instance_id or socket.gethostname()}:{os.getpid()}:{uuid4().hex[:8]}"
)[:120]



def new_lease_owner() -> str:
    """一次执行的租约持有者标识。"""
    return f"{PROCESS_INSTANCE_ID}:{uuid4().hex[:12]}"


def lease_seconds() -> float:
    return float(settings.run_lease_seconds)


async def run_in_session(
    action: Callable[[SessionService], Awaitable[T]],
    *,
    commit: bool = False,
) -> T:
    """在独立短会话里调用 SessionService;租约、心跳与订阅探测都不复用长事务。"""
    async with AsyncSessionLocal() as db:
        result = await action(SessionService(db))
        if commit:
            await db.commit()
        return result


class TurnOffsets:
    """offset 分配:一次预留 OFFSET_RESERVATION 个,预留即带租约校验的写入;
    两次预留之间按 CANCEL_VISIBILITY_SECONDS 只读校验租约,让跨进程取消尽快可见。
    """

    def __init__(self, turn_id: UUID, owner: str) -> None:
        self._turn_id = turn_id
        self._owner = owner
        self._next = 0
        self._remaining = 0
        self._verified_at = 0.0

    async def take(self) -> int:
        if self._remaining == 0:
            self._next = await run_in_session(
                lambda service: service.reserve_turn_offsets(
                    self._turn_id, self._owner, OFFSET_RESERVATION
                ),
                commit=True,
            )
            self._remaining = OFFSET_RESERVATION
            self._verified_at = time.monotonic()
        elif time.monotonic() - self._verified_at >= CANCEL_VISIBILITY_SECONDS:
            await run_in_session(
                lambda service: service.verify_turn_lease(self._turn_id, self._owner)
            )
            self._verified_at = time.monotonic()
        offset = self._next
        self._next += 1
        self._remaining -= 1
        return offset


@dataclass(frozen=True)
class ExecutionHandle:
    """返回给流式接口:从 start_cursor 之后、sequence 大于 start_offset 处开始跟随。"""

    turn_id: UUID
    session_id: int
    start_offset: int
    start_cursor: str


@dataclass
class _Execution:
    turn_id: UUID
    session_id: int
    owner: str
    task: asyncio.Task[None] | None = None
    bus: RunStreamBus | None = None
    in_body: bool = False
    lease_lost: bool = False
    # 本进程主动停止的原因(STOP_*);None 表示未被要求停止。
    stop_reason: str | None = None
    # body 因取消而结束:收尾时需补写半截回答。
    interrupted: bool = False
    # 持有者收尾事务已提交的结果,取消接口据此直接返回。
    termination: TurnTermination | None = None
    heartbeat: asyncio.Task[None] | None = field(default=None, repr=False)


# 进程内注册表:同一 turn 不重复启动,任务结束由 done 回调移除。
_executions: dict[UUID, _Execution] = {}



async def launch_turn_execution(
    *,
    turn_id: UUID,
    session_id: int,
    owner: str,
    start_offset: int,
    body: Callable[[RunStreamBus], Awaitable[None]],
) -> ExecutionHandle:
    """租约已提交后启动后台执行;body 负责业务执行,收尾由本模块统一完成。"""
    existing = _executions.get(turn_id)
    if existing is not None and existing.task is not None and not existing.task.done():
        await run_in_session(
            lambda service: service.release_turn_lease(turn_id, owner),
            commit=True,
        )
        raise AgentException.message("该轮次已有执行中的任务", status_code=409)
    store = get_run_event_store()
    try:
        # 先取尾部游标再启动任务,保证本次执行的事件全部落在游标之后。
        start_cursor = await store.last_cursor(str(turn_id)) or START_CURSOR
    except Exception:
        logger.exception("读取run事件流尾部游标失败，从头跟随", extra={"turn_id": str(turn_id)})
        start_cursor = START_CURSOR
    execution = _Execution(turn_id=turn_id, session_id=session_id, owner=owner)
    execution.task = asyncio.create_task(_run(execution, body), name=f"turn:{turn_id}")
    _executions[turn_id] = execution
    execution.task.add_done_callback(lambda task: _on_done(execution, task))
    logger.info(
        "后台执行已启动",
        extra={"turn_id": str(turn_id), "session_id": session_id, "start_offset": start_offset},
    )
    return ExecutionHandle(turn_id, session_id, start_offset, start_cursor)


def _on_done(execution: _Execution, task: asyncio.Task[None]) -> None:
    if _executions.get(execution.turn_id) is execution:
        _executions.pop(execution.turn_id, None)
    if task.cancelled():
        return
    exc = task.exception()
    if exc is not None:
        logger.error(
            "后台执行出现未处理异常，错误类型=%s",
            type(exc).__name__,
            exc_info=exc,
            extra={"turn_id": str(execution.turn_id)},
        )



async def _run(execution: _Execution, body: Callable[[RunStreamBus], Awaitable[None]]) -> None:
    turn_key = str(execution.turn_id)
    store = get_run_event_store()
    state = TurnState()
    try:
        # 上次执行结束时设置了 EXPIRE,新执行开始先撤销。等待交互期间 key 已过期时,
        # 新执行从 last_event_offset 继续写,快照只含过期之后的部分(truncated=true),
        # 更早的内容由数据库里的消息历史提供,属可接受边界。
        await store.persist(turn_key)
        capture = await store.capture(turn_key)
        state = restore(capture.projection, [event for _, event in capture.entries])
    except Exception:
        # 事件流不可用只影响实时展示,执行照常进行,数据库仍是最终事实来源。
        logger.exception("读取run事件流projection失败，按空状态继续执行", extra={"turn_id": turn_key})
    bus = RunStreamBus(
        store,
        turn_key,
        TurnOffsets(execution.turn_id, execution.owner),
        state,
        on_lease_lost=lambda: _lose_lease(execution),
    )
    execution.bus = bus
    execution.heartbeat = asyncio.create_task(_heartbeat(execution))
    execution.in_body = True
    try:
        if execution.stop_reason is not None:
            # 停止请求早于 body 开始(此前不能 cancel,否则 finally 收尾不会执行)。
            raise asyncio.CancelledError
        await bus.emit(RunEvent.run_started(turn_key, execution.session_id))
        await body(bus)
    except asyncio.CancelledError:
        execution.interrupted = True
        raise
    finally:
        execution.in_body = False
        await _finish(execution)


def _lose_lease(execution: _Execution) -> None:
    """租约丢失:停止写入并取消 body;终态由清除租约的一方负责。"""
    if execution.lease_lost:
        return
    execution.lease_lost = True
    if execution.bus is not None:
        execution.bus.mark_lease_lost()
    if execution.in_body and execution.task is not None:
        execution.task.cancel()


def _request_stop(execution: _Execution, reason: str) -> None:
    """本进程主动停止:只在 body 内取消任务,收尾阶段不打断,避免收尾事务被中途取消。"""
    if execution.stop_reason is None:
        execution.stop_reason = reason
    if execution.in_body and execution.task is not None and not execution.task.done():
        execution.task.cancel()


async def _heartbeat(execution: _Execution) -> None:
    """每隔租约时长的 1/3 用独立短会话续约;影响 0 行即租约丢失。

    数据库瞬时故障不立即判丢失,距上次成功续约超过租约时长才 fail closed。
    """
    interval = lease_seconds() / 3
    last_renewed = time.monotonic()
    while True:
        await asyncio.sleep(interval)
        try:
            renewed = await run_in_session(
                lambda service: service.renew_turn_lease(
                    execution.turn_id, execution.owner, lease_seconds()
                ),
                commit=True,
            )
        except Exception:
            logger.exception("执行租约续约失败，稍后重试", extra={"turn_id": str(execution.turn_id)})
            if time.monotonic() - last_renewed < lease_seconds():
                continue
            renewed = False
        if not renewed:
            logger.warning("执行租约已丢失，取消本次执行", extra={"turn_id": str(execution.turn_id)})
            _lose_lease(execution)
            return
        last_renewed = time.monotonic()



async def _finish(execution: _Execution) -> None:
    """持有者收尾:停心跳 → 刷出缓冲 → 租约内分配 run_end offset → 写 run_end → EXPIRE → 清租约。"""
    turn_key = str(execution.turn_id)
    if execution.heartbeat is not None:
        execution.heartbeat.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await execution.heartbeat
    if execution.bus is not None:
        # 租约仍在时刷出合并缓冲:缓冲里的尾巴文本也要进入下面补写的半截回答。
        await execution.bus.close()
    if execution.lease_lost:
        # 被隔离(回收、接管或跨进程取消已清除租约):不写任何消息、状态或事件。
        return
    partial = _local_partial(execution) if execution.interrupted else []
    cancelled = execution.stop_reason == STOP_USER_CANCEL
    try:
        termination = await run_in_session(
            lambda service: service.finish_turn_execution(
                execution.session_id,
                execution.turn_id,
                execution.owner,
                partial=partial,
                interrupted_session_status="idle" if cancelled else "failed",
            ),
            commit=True,
        )
    except LeaseLostError:
        logger.info("执行收尾时租约已被清除，run_end由清除方负责", extra={"turn_id": turn_key})
        return
    except Exception:
        # 租约保留到过期,由孤儿回收补写 run_end 并收尾。
        logger.exception("执行收尾失败，等待租约过期后回收", extra={"turn_id": turn_key})
        return
    execution.termination = termination
    event = RunEvent.run_end(turn_key, run_end_status(termination.status), cancelled=cancelled)
    try:
        if execution.bus is not None:
            await execution.bus.write_terminal(event, termination.offset)
        await get_run_event_store().expire(turn_key, settings.run_stream_ttl_seconds)
    except Exception:
        logger.exception("写入run_end失败", extra={"turn_id": turn_key})
    try:
        await run_in_session(
            lambda service: service.release_turn_lease(execution.turn_id, execution.owner),
            commit=True,
        )
    except Exception:
        logger.exception("清除执行租约失败，等待租约过期后回收", extra={"turn_id": turn_key})
    logger.info("后台执行已结束", extra={"turn_id": turn_key, "status": event.status})


def run_end_status(turn_status: str) -> str:
    """轮次状态映射为 run_end 状态;仍是 running 说明执行已失联,按 interrupted 处理。"""
    return "interrupted" if turn_status == "running" else turn_status


async def write_detached_run_end(termination: TurnTermination, *, cancelled: bool) -> None:
    """清除租约的一方(取消、回收)补写 run_end 并设置 EXPIRE;失败只记日志。"""
    turn_key = str(termination.turn_id)
    event = RunEvent.run_end(turn_key, run_end_status(termination.status), cancelled=cancelled)
    event.sequence = termination.offset
    store = get_run_event_store()
    try:
        await store.append(turn_key, json.dumps(event.to_dict(), ensure_ascii=False))
        await store.expire(turn_key, settings.run_stream_ttl_seconds)
    except Exception:
        logger.exception("补写run_end失败", extra={"turn_id": turn_key})



def _local_partial(execution: _Execution) -> list[MessageDraft]:
    """同进程取消:从本次执行 bus 的内存状态还原未落库内容;还原失败不阻断收尾。"""
    if execution.bus is None:
        return []
    try:
        return extract_unsaved_messages(execution.bus.state)
    except Exception:
        logger.exception("还原取消前的半截回答失败", extra={"turn_id": str(execution.turn_id)})
        return []


async def _captured_partial(turn_id: UUID) -> list[MessageDraft]:
    """跨进程取消:从事件流快照(projection + 其后事件)还原未落库内容。

    事件流不可用时只记日志:取消本身必须成功,数据库仍是事实来源。
    """
    try:
        capture = await get_run_event_store().capture(str(turn_id))
        state = restore(capture.projection, [event for _, event in capture.entries])
        return extract_unsaved_messages(state)
    except Exception:
        logger.exception("读取事件流快照还原半截回答失败", extra={"turn_id": str(turn_id)})
        return []


async def cancel_turn(db: AsyncSession, session_id: int, turn_id: UUID) -> TurnTermination:
    """用户取消执行中的轮次;半截回答与「会话离开 running」在同一事务提交。

    本进程持有执行:让任务自己收尾,在一个事务里补写半截回答、turn=interrupted、
    会话=idle,随后写 run_end 并清除租约;本接口等它结束(至多 LOCAL_STOP_TIMEOUT_SECONDS)。
    跨进程或等待超时:由本接口加锁,从事件流快照还原半截回答,与清租约、interrupted/idle
    同一事务提交后补写 run_end;持有者此后发现租约丢失即被隔离,不再写入。
    """
    service = SessionService(db)
    state = await service.get_session_turn_execution(session_id, turn_id)
    execution = _executions.get(turn_id)
    if (
        execution is not None
        and execution.task is not None
        and not execution.task.done()
        and state.executing
        and state.status != "awaiting_interaction"
    ):
        # 显式提交只读事务:本地任务收尾要锁同一会话行,等待期间不占着连接上的事务。
        await db.commit()
        _request_stop(execution, STOP_USER_CANCEL)
        await asyncio.wait({execution.task}, timeout=LOCAL_STOP_TIMEOUT_SECONDS)
        if execution.termination is not None:
            logger.info(
                "用户已取消执行，本进程任务已收尾",
                extra={"turn_id": str(turn_id), "status": execution.termination.status},
            )
            return execution.termination
    termination = await service.cancel_turn_execution(
        session_id,
        turn_id,
        partial_loader=lambda: _captured_partial(turn_id),
    )
    # 显式提交:数据库是事实来源,run_end 必须在终态与半截回答提交之后才写入。
    await db.commit()
    if execution is not None and execution.task is not None and not execution.task.done():
        _lose_lease(execution)
        # 等本地任务把残留写入落完,再写 run_end,保证 run_end 是本次执行的最后一条。
        await asyncio.wait({execution.task}, timeout=LOCAL_STOP_TIMEOUT_SECONDS)
    await write_detached_run_end(termination, cancelled=True)
    logger.info(
        "用户已取消执行",
        extra={"turn_id": str(turn_id), "session_id": session_id, "status": termination.status},
    )
    return termination


async def shutdown_executions(timeout: float = LOCAL_STOP_TIMEOUT_SECONDS) -> None:
    """进程退出:停止本进程全部执行,让它们走取消路径保存半截回答、收尾并清除租约。"""
    executions = [
        execution
        for execution in _executions.values()
        if execution.task is not None and not execution.task.done()
    ]
    tasks = [execution.task for execution in executions if execution.task is not None]
    for execution in executions:
        _request_stop(execution, STOP_SHUTDOWN)
    if tasks:
        await asyncio.wait(tasks, timeout=timeout)
        logger.info("进程退出，已取消本进程执行任务", extra={"count": len(tasks)})


def local_execution_task(turn_id: UUID) -> asyncio.Task[None] | None:
    """本进程中该轮次的执行任务(测试与诊断用)。"""
    execution = _executions.get(turn_id)
    return execution.task if execution is not None else None
