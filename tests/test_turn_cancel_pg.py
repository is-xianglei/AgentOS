"""取消路径的真实 PostgreSQL 集成测试:半截回答落库、隔离不写、交互取消补写 run_end。

沿用 test_turn_lease_pg 的做法:整个用例包在外层事务里,结束时回滚;各会话以
create_savepoint 方式共享同一连接,commit 只释放 SAVEPOINT,不落库。
执行体不长期持有会话:先把本轮消息写入一个不提交的会话(关闭即回滚,等价于取消时的回滚),
再只经事件总线 emit,等待被取消。
"""

import asyncio
import uuid
from contextlib import asynccontextmanager
from datetime import timedelta
from typing import Any

import pytest
from sqlalchemy import func, update
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.pool import NullPool

import database.engine  # noqa: F401  副作用注册全部映射与全局软删过滤
from core.config import settings
from core.errors import LeaseLostError
from core.event_bus import NullStreamBus
from core.events import (
    ORCHESTRATOR_ACTOR,
    BlockType,
    InteractionInfo,
    RunEvent,
    StreamEvent,
    ToolInfo,
)
from core.run_events import LocalRunEventStore
from core.run_state import restore
from interaction.schemas import InteractionRequestCreate
from interaction.service import InteractionService
from llm.types import ToolResultMessage, ToolUse, build_assistant_blocks
from runtime import execution, recovery, turn_follow
from runtime.agent import AgentRuntime
from runtime.cancel_partial import CANCELLED_TOOL_OUTPUT
from session.models import SessionRecord, SessionTurnRecord
from session.service import SessionService
from tools.registry import build_tool_registry
from tools.service import ToolService
from user.models import UserRecord
from workspace.models import WorkspaceRecord

pytestmark = pytest.mark.anyio

_LEAD = ORCHESTRATOR_ACTOR


class _Shared:
    def __init__(self, conn) -> None:
        self.conn = conn
        self.lock = asyncio.Lock()

    @asynccontextmanager
    async def session(self):
        async with self.lock:
            db = AsyncSession(
                bind=self.conn, expire_on_commit=False, join_transaction_mode="create_savepoint"
            )
            try:
                yield db
            finally:
                await db.close()


@pytest.fixture
async def pg():
    engine = create_async_engine(settings.database_url, poolclass=NullPool)
    try:
        conn = await engine.connect()
    except Exception as exc:  # noqa: BLE001  任何连接错误都只用于决定跳过
        await engine.dispose()
        pytest.skip(f"PostgreSQL 不可达(AGENTOS_DATABASE_URL): {type(exc).__name__}")
    transaction = await conn.begin()
    shared = _Shared(conn)
    suffix = uuid.uuid4().hex[:12]
    async with shared.session() as db:
        user = UserRecord(
            email=f"cancel-{suffix}@test.local", username=f"cancel-{suffix}", password="x"
        )
        workspace = WorkspaceRecord(
            name=f"cancel-{suffix}", slug=f"cancel-{suffix}", display_name="取消测试"
        )
        db.add_all([user, workspace])
        await db.flush()
        await db.commit()
        ids = (user.id, workspace.id)
    try:
        yield shared, ids
    finally:
        await execution.shutdown_executions(timeout=2)
        await transaction.rollback()
        await conn.close()
        await engine.dispose()


def _wire(monkeypatch: pytest.MonkeyPatch, shared: _Shared, store, *, lease: float = 30.0) -> None:
    """执行、跟随与回收模块的短会话和事件流存储都接到测试连接与测试存储上。"""
    monkeypatch.setattr(execution, "AsyncSessionLocal", shared.session)
    monkeypatch.setattr(execution, "get_run_event_store", lambda: store)
    monkeypatch.setattr(turn_follow, "get_run_event_store", lambda: store)
    monkeypatch.setattr(execution, "lease_seconds", lambda: lease)


async def _new_turn(shared: _Shared, ids, content: str = "问题") -> tuple[int, uuid.UUID]:
    user_id, workspace_id = ids
    async with shared.session() as db:
        session = SessionRecord(
            title="t", status="running", user_id=user_id, workspace_id=workspace_id
        )
        db.add(session)
        await db.flush()
        turn, _ = await SessionService(db).start_turn(session, user_id, workspace_id, content)
        await db.commit()
        return session.id, turn.id


async def _turn(shared: _Shared, turn_id) -> SessionTurnRecord:
    async with shared.session() as db:
        return await SessionService(db).repo.get_turn_for_update(turn_id)


async def _session_status(shared: _Shared, session_id: int) -> str:
    async with shared.session() as db:
        return (await SessionService(db).lock_required(session_id)).status


async def _rows(shared: _Shared, session_id: int) -> list[tuple[str, Any]]:
    async with shared.session() as db:
        messages = await SessionService(db).list_messages(session_id)
        return [(message.role, message.content) for message in messages]


async def _entries(store, turn_id) -> list[dict[str, Any]]:
    return [event for _, event in (await store.capture(str(turn_id))).entries]


async def _write_uncommitted(shared: _Shared, session_id: int, turn_id, rows) -> None:
    """模拟主循环本轮 add_message 只 flush 不提交:会话关闭即回滚,等价于取消时的 rollback。"""
    async with shared.session() as db:
        service = SessionService(db)
        for role, content in rows:
            await service.add_message(session_id, role, content, turn_id=turn_id)


def _use(tool_id: str) -> ToolUse:
    return ToolUse(id=tool_id, name="Read", input={"path": f"{tool_id}.txt"})


def _result(tool_id: str, output: str, *, is_error: bool = False) -> dict[str, Any]:
    return ToolResultMessage(
        tool_use_id=tool_id,
        tool_name="Read",
        input_args={"path": f"{tool_id}.txt"},
        output=output,
        is_error=is_error,
    ).to_content_dict()


async def _emit_text(bus, sid: int, index: int, *chunks: str, close: bool = True) -> None:
    await bus.emit(StreamEvent.block_start(_LEAD, sid, index, BlockType.TEXT))
    for chunk in chunks:
        await bus.emit(StreamEvent.text_delta(_LEAD, sid, index, chunk))
    if close:
        await bus.emit(StreamEvent.block_stop(_LEAD, sid, index))


async def _emit_tool(bus, sid: int, index: int, tool_id: str, *, complete: bool = True) -> None:
    await bus.emit(StreamEvent.block_start(_LEAD, sid, index, BlockType.TOOL_USE))
    if complete:
        info = ToolInfo(id=tool_id, name="Read", input={"path": f"{tool_id}.txt"})
        await bus.emit(StreamEvent.tool_use(_LEAD, sid, index, info))
        await bus.emit(StreamEvent.block_stop(_LEAD, sid, index))


async def _emit_result(bus, sid: int, tool_id: str, output: str) -> None:
    info = ToolInfo(id=tool_id, name="Read", output=output, is_error=False)
    await bus.emit(StreamEvent.tool_result(_LEAD, sid, info))


async def _launch(shared: _Shared, session_id: int, turn_id, script) -> str:
    """认领租约并启动后台执行;script(bus) 写完本轮内容后应阻塞等待被取消。"""
    owner = execution.new_lease_owner()
    async with shared.session() as db:
        offset = await SessionService(db).claim_turn_lease(turn_id, owner, 30)
        await db.commit()
    await execution.launch_turn_execution(
        turn_id=turn_id, session_id=session_id, owner=owner, start_offset=offset, body=script
    )
    return owner


@asynccontextmanager
async def _api_session(shared: _Shared):
    """取消接口的请求会话:不占测试锁,取消等待期间本地任务的收尾短会话才能执行。"""
    db = AsyncSession(
        bind=shared.conn, expire_on_commit=False, join_transaction_mode="create_savepoint"
    )
    try:
        yield db
    finally:
        await db.close()


async def _next_turn_context(shared: _Shared, ids, session_id: int) -> tuple[int, list[dict]]:
    """取消后马上发下一条消息,返回其消息 ID 与按现有历史加载路径构造的请求 messages。"""
    user_id, workspace_id = ids
    async with shared.session() as db:
        service = SessionService(db)
        session = await service.prepare_for_message(session_id, "下一问", user_id, workspace_id)
        turn, message = await service.start_turn(session, user_id, workspace_id, "下一问")
        await db.commit()
        history, current, _ = await service.load_context_for_turn(turn)
        return message.id, history + current


async def _message_ids(shared: _Shared, session_id: int) -> list[int]:
    async with shared.session() as db:
        return [message.id for message in await SessionService(db).list_messages(session_id)]


async def test_local_cancel_during_tool_saves_partial_atomically(
    pg, monkeypatch: pytest.MonkeyPatch, assert_replayable
) -> None:
    """本进程取消:任务自己在一个事务里补写半截回答并改 interrupted/idle,下一条消息排在其后。"""
    shared, ids = pg
    store = LocalRunEventStore(maxlen=5000)
    _wire(monkeypatch, shared, store)
    session_id, turn_id = await _new_turn(shared, ids)
    blocked = asyncio.Event()

    async def script(bus) -> None:
        await bus.emit(StreamEvent.turn_start(_LEAD, session_id))
        await _emit_text(bus, session_id, 0, "先读", "文件")
        await _emit_tool(bus, session_id, 1, "a")
        await _emit_result(bus, session_id, "a", "内容A")
        await _emit_text(bus, session_id, 2, "再读一个")
        await _emit_tool(bus, session_id, 3, "b")
        await _write_uncommitted(
            shared,
            session_id,
            turn_id,
            [
                ("assistant", build_assistant_blocks("先读文件", [_use("a")])),
                ("tool", _result("a", "内容A")),
                ("assistant", build_assistant_blocks("再读一个", [_use("b")])),
            ],
        )
        blocked.set()
        await asyncio.Event().wait()  # 工具 b 执行中

    async def _no_fallback(turn_id):
        raise AssertionError("本进程取消不应走事件流快照兜底")

    monkeypatch.setattr(execution, "_captured_partial", _no_fallback)
    await _launch(shared, session_id, turn_id, script)
    await blocked.wait()
    await asyncio.sleep(0.1)
    async with _api_session(shared) as db:
        termination = await execution.cancel_turn(db, session_id, turn_id)
        await db.commit()

    assert (termination.status, termination.session_status) == ("interrupted", "idle")
    assert await _rows(shared, session_id) == [
        ("user", "问题"),
        ("assistant", build_assistant_blocks("先读文件", [_use("a")])),
        ("tool", _result("a", "内容A")),
        ("assistant", build_assistant_blocks("再读一个", [_use("b")])),
        ("tool", _result("b", CANCELLED_TOOL_OUTPUT, is_error=True)),
    ]
    turn = await _turn(shared, turn_id)
    assert turn.status == "interrupted" and turn.lease_owner is None
    assert await _session_status(shared, session_id) == "idle"
    assert (await _entries(store, turn_id))[-1] == {
        "type": "run_end",
        "turn_id": str(turn_id),
        "status": "interrupted",
        "cancelled": True,
        "sequence": turn.last_event_offset,
    }
    earlier = await _message_ids(shared, session_id)
    next_message_id, messages = await _next_turn_context(shared, ids, session_id)
    assert next_message_id > max(earlier)
    roles = [m["role"] for m in messages]
    assert roles == ["user", "assistant", "tool", "assistant", "tool", "user"]
    assert_replayable(messages)


async def test_shutdown_cancel_saves_partial_and_marks_failed(
    pg, monkeypatch: pytest.MonkeyPatch
) -> None:
    shared, ids = pg
    store = LocalRunEventStore(maxlen=5000)
    _wire(monkeypatch, shared, store)
    session_id, turn_id = await _new_turn(shared, ids)
    blocked = asyncio.Event()

    async def script(bus) -> None:
        await bus.emit(StreamEvent.turn_start(_LEAD, session_id))
        await _emit_text(bus, session_id, 0, "进程关闭前", "写了一半", close=False)
        blocked.set()
        await asyncio.Event().wait()

    await _launch(shared, session_id, turn_id, script)
    await blocked.wait()
    await execution.shutdown_executions(timeout=3)

    assert await _rows(shared, session_id) == [
        ("user", "问题"),
        ("assistant", [{"kind": "text", "text": "进程关闭前写了一半"}]),
    ]
    turn = await _turn(shared, turn_id)
    assert turn.status == "interrupted" and turn.lease_owner is None
    assert await _session_status(shared, session_id) == "failed"
    run_end = (await _entries(store, turn_id))[-1]
    assert (run_end["type"], run_end["status"]) == ("run_end", "interrupted")
    assert "cancelled" not in run_end


async def test_local_cancel_without_new_text_creates_no_message(
    pg, monkeypatch: pytest.MonkeyPatch
) -> None:
    shared, ids = pg
    store = LocalRunEventStore(maxlen=5000)
    _wire(monkeypatch, shared, store)
    session_id, turn_id = await _new_turn(shared, ids)
    blocked = asyncio.Event()

    async def script(bus) -> None:
        await bus.emit(StreamEvent.turn_start(_LEAD, session_id))
        await bus.emit(StreamEvent.block_start(_LEAD, session_id, 0, BlockType.THINKING))
        await bus.emit(StreamEvent.thinking_delta(_LEAD, session_id, 0, "还在想"))
        blocked.set()
        await asyncio.Event().wait()

    await _launch(shared, session_id, turn_id, script)
    await blocked.wait()
    async with _api_session(shared) as db:
        await execution.cancel_turn(db, session_id, turn_id)
        await db.commit()

    assert await _rows(shared, session_id) == [("user", "问题")]
    assert (await _turn(shared, turn_id)).status == "interrupted"
    assert await _session_status(shared, session_id) == "idle"
    assert (await _entries(store, turn_id))[-1]["cancelled"] is True


async def test_cross_process_cancel_restores_partial_from_snapshot(
    pg, monkeypatch: pytest.MonkeyPatch, run_store_factory, assert_replayable
) -> None:
    """其他进程持有执行:取消方从事件流快照还原半截回答,与清租约、interrupted/idle 同一事务。"""
    shared, ids = pg
    store = run_store_factory()
    _wire(monkeypatch, shared, store)
    session_id, turn_id = await _new_turn(shared, ids)
    owner = "other-process:holder"
    async with shared.session() as db:
        await SessionService(db).claim_turn_lease(turn_id, owner, 30)
        await db.commit()
    # 持有者在另一个进程:只有它写进事件流的事件,本进程注册表里没有它的任务。
    bus = execution.RunStreamBus(
        store, str(turn_id), execution.TurnOffsets(turn_id, owner), restore(None, [])
    )
    await bus.emit(RunEvent.run_started(str(turn_id), session_id))
    await bus.emit(StreamEvent.turn_start(_LEAD, session_id))
    await _emit_text(bus, session_id, 0, "查一下")
    await _emit_tool(bus, session_id, 1, "a")
    await _emit_result(bus, session_id, "a", "内容A")
    await _emit_text(bus, session_id, 2, "跨进程写了", "一半", close=False)
    await _emit_tool(bus, session_id, 3, "b", complete=False)  # tool_use 只输出了一半
    await bus.close()

    async with shared.session() as db:
        termination = await execution.cancel_turn(db, session_id, turn_id)
        await db.commit()

    assert (termination.status, termination.session_status) == ("interrupted", "idle")
    assert await _rows(shared, session_id) == [
        ("user", "问题"),
        ("assistant", build_assistant_blocks("查一下", [_use("a")])),
        ("tool", _result("a", "内容A")),
        ("assistant", [{"kind": "text", "text": "跨进程写了一半"}]),
    ]
    turn = await _turn(shared, turn_id)
    assert turn.lease_owner is None and await _session_status(shared, session_id) == "idle"
    run_end = (await _entries(store, turn_id))[-1]
    assert run_end["cancelled"] is True and run_end["sequence"] == turn.last_event_offset
    # 持有者随后提交终态时被隔离,不会再写消息或改状态。
    with pytest.raises(LeaseLostError):
        async with shared.session() as db:
            await SessionService(db).finish_turn_execution(
                session_id, turn_id, owner, partial=[]
            )
    assert len(await _rows(shared, session_id)) == 4

    frames = [frame async for frame in turn_follow.subscribe_turn(turn_id)]
    assert [frame["type"] for frame in frames] == ["run_snapshot", "run_end"]
    snapshot = frames[0]["state"]
    assert (snapshot["status"], snapshot["cancelled"], snapshot["interaction"]) == (
        "interrupted",
        True,
        None,
    )
    _, messages = await _next_turn_context(shared, ids, session_id)
    assert [m["role"] for m in messages] == ["user", "assistant", "tool", "assistant", "user"]
    assert_replayable(messages)


async def test_local_stop_timeout_falls_back_to_snapshot_and_fences_task(
    pg, monkeypatch: pytest.MonkeyPatch
) -> None:
    """本地任务迟迟不收尾:取消方按快照补写并清租约,之后任务被隔离,不再写入。"""
    shared, ids = pg
    store = LocalRunEventStore(maxlen=5000)
    _wire(monkeypatch, shared, store)
    monkeypatch.setattr(execution, "LOCAL_STOP_TIMEOUT_SECONDS", 0.3)
    session_id, turn_id = await _new_turn(shared, ids)
    blocked = asyncio.Event()

    async def script(bus) -> None:
        await bus.emit(StreamEvent.turn_start(_LEAD, session_id))
        await _emit_text(bus, session_id, 0, "卡住前", "写了一半", close=False)
        blocked.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            await asyncio.sleep(30)  # 第一次取消时卡住,超时后第二次取消才结束
            raise

    await _launch(shared, session_id, turn_id, script)
    await blocked.wait()
    await asyncio.sleep(0.1)
    task = execution.local_execution_task(turn_id)
    async with _api_session(shared) as db:
        termination = await execution.cancel_turn(db, session_id, turn_id)
        await db.commit()

    assert (termination.status, termination.session_status) == ("interrupted", "idle")
    assert task.done()
    assert await _rows(shared, session_id) == [
        ("user", "问题"),
        ("assistant", [{"kind": "text", "text": "卡住前写了一半"}]),
    ]
    turn = await _turn(shared, turn_id)
    assert turn.lease_owner is None
    run_ends = [e for e in await _entries(store, turn_id) if e["type"] == "run_end"]
    assert run_ends == [
        {
            "type": "run_end",
            "turn_id": str(turn_id),
            "status": "interrupted",
            "cancelled": True,
            "sequence": turn.last_event_offset,
        }
    ]


def _fenceable_script(session_id: int, blocked: asyncio.Event, release: asyncio.Event):
    """写出半截回答后等 release;之后的下一次写入会在 offset 校验处发现租约已不属于自己。"""

    async def script(bus) -> None:
        await bus.emit(StreamEvent.turn_start(_LEAD, session_id))
        await _emit_text(bus, session_id, 0, "被隔离前写了一半", close=False)
        blocked.set()
        await release.wait()
        await asyncio.sleep(execution.CANCEL_VISIBILITY_SECONDS + 0.05)
        await bus.emit(StreamEvent.block_stop(_LEAD, session_id, 0))
        await asyncio.Event().wait()

    return script


async def test_fenced_by_takeover_writes_nothing(pg, monkeypatch: pytest.MonkeyPatch) -> None:
    shared, ids = pg
    store = LocalRunEventStore(maxlen=5000)
    _wire(monkeypatch, shared, store)
    session_id, turn_id = await _new_turn(shared, ids)
    blocked, release = asyncio.Event(), asyncio.Event()
    await _launch(shared, session_id, turn_id, _fenceable_script(session_id, blocked, release))
    await blocked.wait()
    task = execution.local_execution_task(turn_id)

    async with shared.session() as db:
        await db.execute(
            update(SessionTurnRecord)
            .where(SessionTurnRecord.id == turn_id)
            .values(
                lease_owner="takeover",
                lease_expires_at=func.clock_timestamp() + timedelta(seconds=60),
            )
        )
        await db.commit()
    release.set()
    await asyncio.wait({task}, timeout=3)

    assert task.done() and task.cancelled()
    assert await _rows(shared, session_id) == [("user", "问题")]
    turn = await _turn(shared, turn_id)
    assert (turn.status, turn.lease_owner) == ("running", "takeover")
    assert await _session_status(shared, session_id) == "running"
    assert all(event["type"] != "run_end" for event in await _entries(store, turn_id))


async def test_fenced_by_reaper_writes_nothing(pg, monkeypatch: pytest.MonkeyPatch) -> None:
    shared, ids = pg
    store = LocalRunEventStore(maxlen=5000)
    _wire(monkeypatch, shared, store)
    session_id, turn_id = await _new_turn(shared, ids)
    blocked, release = asyncio.Event(), asyncio.Event()
    await _launch(shared, session_id, turn_id, _fenceable_script(session_id, blocked, release))
    await blocked.wait()
    task = execution.local_execution_task(turn_id)
    async with shared.session() as db:
        await db.execute(
            update(SessionTurnRecord)
            .where(SessionTurnRecord.id == turn_id)
            .values(lease_expires_at=func.clock_timestamp() - timedelta(seconds=60))
        )
        await db.commit()

    await recovery.reap_orphan_turns()
    release.set()
    await asyncio.wait({task}, timeout=3)

    assert task.done() and task.cancelled()
    assert await _rows(shared, session_id) == [("user", "问题")]
    turn = await _turn(shared, turn_id)
    assert (turn.status, turn.lease_owner) == ("interrupted", None)
    assert await _session_status(shared, session_id) == "failed"
    run_ends = [e for e in await _entries(store, turn_id) if e["type"] == "run_end"]
    assert run_ends == [
        {
            "type": "run_end",
            "turn_id": str(turn_id),
            "status": "interrupted",
            "sequence": turn.last_event_offset,
        }
    ]


async def _suspend(shared: _Shared, ids, store) -> tuple[int, uuid.UUID, uuid.UUID]:
    """走真实执行包装:执行体落 assistant 与等待交互的工具调用、建暂停点并 emit 交互请求,
    收尾写 run_end(awaiting_interaction)、设置 EXPIRE、清租约。返回 (会话, 轮次, 暂停点)。"""
    session_id, turn_id = await _new_turn(shared, ids)
    suspension_ids: list[uuid.UUID] = []

    async def script(bus) -> None:
        await bus.emit(StreamEvent.turn_start(_LEAD, session_id))
        await _emit_text(bus, session_id, 0, "需要审批")
        await _emit_tool(bus, session_id, 1, "w")
        async with shared.session() as db:
            service = SessionService(db)
            message = await service.add_message(
                session_id,
                "assistant",
                build_assistant_blocks("需要审批", [_use("w")]),
                turn_id=turn_id,
            )
            record = await ToolService(db, build_tool_registry()).start_call(
                session_id, "Read", {"path": "w.txt"}, message_id=message.id
            )
            await ToolService(db, build_tool_registry()).tool_repo.mark_awaiting_interaction(record)
            suspension, request = await InteractionService(db).create_suspension_with_request(
                session_id,
                turn_id,
                {"assistant_message_id": message.id, "pending": []},
                {"mode": "sequential"},
                InteractionRequestCreate(
                    kind="tool_approval",
                    request_payload={"tool": {"id": "w", "name": "Read"}},
                    tool_call_id=record.id,
                    tool_use_id="w",
                ),
            )
            session = await service.lock_required(session_id)
            await service.repo.update_status(session, "awaiting_interaction")
            turn = await service.get_turn_required(turn_id)
            await service.mark_turn_status(turn, "awaiting_interaction")
            await db.commit()
            suspension_ids.append(suspension.id)
        info = InteractionInfo(
            request_id=str(request.id),
            suspension_id=str(suspension.id),
            kind="tool_approval",
            schema_version=1,
            request_payload={},
        )
        await bus.emit(StreamEvent.interaction_request(_LEAD, session_id, info))

    await _launch(shared, session_id, turn_id, script)
    await asyncio.wait({execution.local_execution_task(turn_id)}, timeout=3)
    assert (await _entries(store, turn_id))[-1]["status"] == "awaiting_interaction"
    return session_id, turn_id, suspension_ids[0]


def _interaction_runtime(db: AsyncSession, ids) -> AgentRuntime:
    """交互取消只用到会话、交互与工具服务;不构造 LLM 客户端。"""
    runtime = AgentRuntime.__new__(AgentRuntime)
    runtime.db = db
    runtime.user_id, runtime.workspace_id = ids
    runtime.session_service = SessionService(db)
    runtime.interaction_service = InteractionService(db)
    runtime.tool_service = ToolService(db, build_tool_registry())
    runtime.bus = NullStreamBus()
    return runtime


async def _has_ttl(store, turn_id) -> bool:
    if isinstance(store, LocalRunEventStore):
        log = store._logs.get(str(turn_id))
        return log is not None and log.expires_at is not None
    return await store._clients.command.pttl(store.events_key(str(turn_id))) > 0


async def test_interaction_cancel_writes_cancelled_run_end(
    pg, monkeypatch: pytest.MonkeyPatch, run_store_factory
) -> None:
    """交互取消提交后补写 run_end(interrupted, cancelled):快照离开 awaiting、不再有待处理交互。"""
    shared, ids = pg
    store = run_store_factory()
    _wire(monkeypatch, shared, store)
    session_id, turn_id, suspension_id = await _suspend(shared, ids, store)
    awaiting = restore(None, await _entries(store, turn_id))
    assert awaiting.status == "awaiting_interaction" and awaiting.interaction is not None
    await store.persist(str(turn_id))  # 撤掉上次 run_end 的 EXPIRE,验证取消会重新设置
    assert not await _has_ttl(store, turn_id)

    async with shared.session() as db:
        await _interaction_runtime(db, ids).cancel_interaction(session_id, suspension_id)

    turn = await _turn(shared, turn_id)
    assert (turn.status, turn.lease_owner) == ("interrupted", None)
    assert await _session_status(shared, session_id) == "idle"
    assert (await _entries(store, turn_id))[-1] == {
        "type": "run_end",
        "turn_id": str(turn_id),
        "status": "interrupted",
        "cancelled": True,
        "sequence": turn.last_event_offset,
    }
    assert await _has_ttl(store, turn_id)
    # 取消之后才订阅的客户端拿到正确快照。
    frames = [frame async for frame in turn_follow.subscribe_turn(turn_id)]
    assert [frame["type"] for frame in frames] == ["run_snapshot", "run_end"]
    state = frames[0]["state"]
    assert (state["status"], state["cancelled"], state["interaction"]) == (
        "interrupted",
        True,
        None,
    )
    assert frames[1]["cancelled"] is True
    rows = await _rows(shared, session_id)
    assert rows[-1][0] == "tool" and rows[-1][1]["is_error"] is True


async def test_detached_run_end_offset_never_reuses_reserved_offsets(pg) -> None:
    """没有执行持有租约也能补写:offset 与预留同一行原子分配,残留持有者随即被隔离。"""
    shared, ids = pg
    session_id, turn_id = await _new_turn(shared, ids)
    async with shared.session() as db:
        service = SessionService(db)
        await service.claim_turn_lease(turn_id, "owner-a", 30)
        assert await service.reserve_turn_offsets(turn_id, "owner-a", 64) == 1
        await db.commit()

    async with shared.session() as db:
        service = SessionService(db)
        session = await service.lock_required(session_id)
        turn = await service.repo.get_turn_for_update(turn_id)
        termination = await service.release_turn_for_detached_end(session, turn)
        await db.commit()

    assert termination.offset == 65
    assert (await _turn(shared, turn_id)).lease_owner is None
    with pytest.raises(LeaseLostError):
        async with shared.session() as db:
            await SessionService(db).reserve_turn_offsets(turn_id, "owner-a", 1)
    with pytest.raises(LeaseLostError):
        async with shared.session() as db:
            await SessionService(db).finish_turn_execution(session_id, turn_id, "owner-a")
