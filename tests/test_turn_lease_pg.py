"""执行租约与孤儿回收的真实 PostgreSQL 集成测试。

连接 .env 中 AGENTOS_DATABASE_URL 指向的库,整个用例包在外层事务里,结束时回滚;
各会话以 create_savepoint 方式共享同一连接,commit 只释放 SAVEPOINT,不落库。
共享连接上的并发访问由 _SerialScope 的锁串行化(psycopg 单连接不支持并发语句)。
"""

import asyncio
import uuid
from contextlib import asynccontextmanager
from datetime import timedelta

import pytest
from sqlalchemy import func, update
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.pool import NullPool

import database.engine  # noqa: F401  副作用注册全部映射与全局软删过滤
from core.config import settings
from core.errors import AgentException, LeaseLostError
from core.run_events import LocalRunEventStore
from runtime import execution, recovery
from session.models import SessionRecord, SessionTurnRecord
from session.service import SessionService
from user.models import UserRecord
from workspace.models import WorkspaceRecord

pytestmark = pytest.mark.anyio


class _Shared:
    def __init__(self, conn) -> None:
        self.conn = conn
        self.lock = asyncio.Lock()

    @asynccontextmanager
    async def session(self):
        async with self.lock:
            db = AsyncSession(
                bind=self.conn,
                expire_on_commit=False,
                join_transaction_mode="create_savepoint",
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
            email=f"lease-{suffix}@test.local", username=f"lease-{suffix}", password="x"
        )
        workspace = WorkspaceRecord(
            name=f"lease-{suffix}", slug=f"lease-{suffix}", display_name="租约测试"
        )
        db.add_all([user, workspace])
        await db.flush()
        await db.commit()
        ids = (user.id, workspace.id)
    try:
        yield shared, ids
    finally:
        await transaction.rollback()
        await conn.close()
        await engine.dispose()


async def _new_turn(
    shared: _Shared,
    ids,
    *,
    session_status: str = "running",
) -> tuple[int, uuid.UUID]:
    user_id, workspace_id = ids
    async with shared.session() as db:
        service = SessionService(db)
        session = SessionRecord(
            title="t", status=session_status, user_id=user_id, workspace_id=workspace_id
        )
        db.add(session)
        await db.flush()
        turn, _ = await service.start_turn(session, user_id, workspace_id, "问题")
        await db.commit()
        return session.id, turn.id


async def _call(shared: _Shared, action, *, commit: bool = True):
    async with shared.session() as db:
        result = await action(SessionService(db))
        if commit:
            await db.commit()
        return result


async def _turn(shared: _Shared, turn_id) -> SessionTurnRecord:
    async with shared.session() as db:
        return await SessionService(db).repo.get_turn_for_update(turn_id)


async def _session_status(shared: _Shared, session_id: int) -> str:
    async with shared.session() as db:
        return (await SessionService(db).lock_required(session_id)).status


async def _expire(shared: _Shared, turn_id) -> None:
    """把租约改成已过期(模拟持有者进程崩溃后不再续约)。"""
    async with shared.session() as db:
        await db.execute(
            update(SessionTurnRecord)
            .where(SessionTurnRecord.id == turn_id)
            .values(lease_expires_at=func.clock_timestamp() - timedelta(seconds=60))
        )
        await db.commit()


async def test_claim_conflict_and_reclaim_after_expiry(pg) -> None:
    shared, ids = pg
    _, turn_id = await _new_turn(shared, ids)

    assert await _call(shared, lambda s: s.claim_turn_lease(turn_id, "owner-a", 90)) == 0
    with pytest.raises(AgentException) as conflict:
        await _call(shared, lambda s: s.claim_turn_lease(turn_id, "owner-b", 90))
    assert conflict.value.status_code == 409

    await _expire(shared, turn_id)
    await _call(shared, lambda s: s.claim_turn_lease(turn_id, "owner-b", 90))
    assert (await _turn(shared, turn_id)).lease_owner == "owner-b"


async def test_renew_and_offsets_are_owner_fenced_and_continuous(pg) -> None:
    shared, ids = pg
    _, turn_id = await _new_turn(shared, ids)
    await _call(shared, lambda s: s.claim_turn_lease(turn_id, "owner-a", 90))
    before = (await _turn(shared, turn_id)).lease_expires_at

    assert await _call(shared, lambda s: s.renew_turn_lease(turn_id, "owner-a", 300))
    assert (await _turn(shared, turn_id)).lease_expires_at > before
    assert not await _call(shared, lambda s: s.renew_turn_lease(turn_id, "stranger", 300))
    assert await _call(shared, lambda s: s.reserve_turn_offsets(turn_id, "owner-a", 64)) == 1
    assert await _call(shared, lambda s: s.reserve_turn_offsets(turn_id, "owner-a", 64)) == 65
    with pytest.raises(LeaseLostError):
        await _call(shared, lambda s: s.reserve_turn_offsets(turn_id, "stranger", 64))

    # 下一次执行(交互恢复)从 last_event_offset 继续,预留后未用的 offset 不复用。
    await _call(shared, lambda s: s.release_turn_lease(turn_id, "owner-a"))
    assert await _call(shared, lambda s: s.claim_turn_lease(turn_id, "owner-b", 90)) == 128
    assert await _call(shared, lambda s: s.reserve_turn_offsets(turn_id, "owner-b", 1)) == 129


async def test_cancel_running_turn_sets_interrupted_and_idle(pg) -> None:
    shared, ids = pg
    session_id, turn_id = await _new_turn(shared, ids)
    await _call(shared, lambda s: s.claim_turn_lease(turn_id, "owner-a", 90))

    termination = await _call(shared, lambda s: s.cancel_turn_execution(session_id, turn_id))

    turn = await _turn(shared, turn_id)
    assert (termination.status, termination.session_status) == ("interrupted", "idle")
    assert turn.status == "interrupted" and turn.lease_owner is None
    assert termination.offset == turn.last_event_offset == 1
    assert await _session_status(shared, session_id) == "idle"
    # 被取消的持有者在提交终态前加锁隔离,发现租约已不属于自己。
    with pytest.raises(LeaseLostError):
        await _call(shared, lambda s: s.lock_owned_turn(session_id, turn_id, "owner-a"))
    with pytest.raises(LeaseLostError):
        await _call(shared, lambda s: s.verify_turn_lease(turn_id, "owner-a"))


async def test_cancel_rejects_idle_and_awaiting_turns_and_keeps_completed(pg) -> None:
    shared, ids = pg
    session_id, turn_id = await _new_turn(shared, ids)
    with pytest.raises(AgentException) as idle:
        await _call(shared, lambda s: s.cancel_turn_execution(session_id, turn_id))
    assert idle.value.status_code == 409

    await _call(shared, lambda s: s.claim_turn_lease(turn_id, "owner-a", 90))
    async with shared.session() as db:
        service = SessionService(db)
        turn = await service.get_turn_required(turn_id)
        await service.mark_turn_status(turn, "awaiting_interaction")
        await db.commit()
    with pytest.raises(AgentException) as awaiting:
        await _call(shared, lambda s: s.cancel_turn_execution(session_id, turn_id))
    assert awaiting.value.status_code == 409 and "交互取消接口" in awaiting.value.message
    assert (await _turn(shared, turn_id)).lease_owner == "owner-a"

    with pytest.raises(AgentException) as other:
        await _call(shared, lambda s: s.cancel_turn_execution(session_id + 10_000, turn_id))
    assert other.value.status_code in {404, 500}


async def _complete(shared: _Shared, session_id: int, turn_id) -> None:
    async with shared.session() as db:
        service = SessionService(db)
        turn = await service.get_turn_required(turn_id)
        message = await service.add_message(session_id, "assistant", [], turn_id=turn_id)
        await service.mark_turn_status(turn, "completed", completed_message_id=message.id)
        await service.repo.update_status(await service.get_required(session_id), "idle")
        await db.commit()


async def test_cancel_completed_turn_only_stops_execution(pg) -> None:
    """已 completed 但仍在唤醒队友:只清租约,不改轮次与会话状态。"""
    shared, ids = pg
    session_id, turn_id = await _new_turn(shared, ids)
    await _call(shared, lambda s: s.claim_turn_lease(turn_id, "owner-a", 90))
    await _complete(shared, session_id, turn_id)

    termination = await _call(shared, lambda s: s.cancel_turn_execution(session_id, turn_id))

    assert (termination.status, termination.session_status) == ("completed", "idle")
    assert (await _turn(shared, turn_id)).lease_owner is None


async def test_finish_marks_still_running_turn_interrupted(pg) -> None:
    shared, ids = pg
    session_id, turn_id = await _new_turn(shared, ids)
    await _call(shared, lambda s: s.claim_turn_lease(turn_id, "owner-a", 90))

    termination = await _call(
        shared, lambda s: s.finish_turn_execution(session_id, turn_id, "owner-a")
    )

    assert (termination.status, termination.session_status) == ("interrupted", "failed")
    # run_end 写入之前租约仍在,保证下一次执行的事件排在本次 run_end 之后。
    assert (await _turn(shared, turn_id)).lease_owner == "owner-a"
    with pytest.raises(LeaseLostError):
        await _call(shared, lambda s: s.finish_turn_execution(session_id, turn_id, "stranger"))


async def test_reap_only_expired_and_legacy_unleased_running(pg) -> None:
    shared, ids = pg
    live_session, live_turn = await _new_turn(shared, ids)
    await _call(shared, lambda s: s.claim_turn_lease(live_turn, "alive", 90))
    dead_session, dead_turn = await _new_turn(shared, ids)
    await _call(shared, lambda s: s.claim_turn_lease(dead_turn, "crashed", 90))
    await _expire(shared, dead_turn)
    legacy_session, legacy_turn = await _new_turn(shared, ids)

    periodic = await _call(
        shared, lambda s: s.list_reapable_turn_ids(include_unleased_running=False)
    )
    startup = await _call(shared, lambda s: s.list_reapable_turn_ids(include_unleased_running=True))
    assert dead_turn in periodic and live_turn not in periodic and legacy_turn not in periodic
    assert {dead_turn, legacy_turn} <= set(startup) and live_turn not in startup

    assert await _call(
        shared, lambda s: s.reap_turn(live_turn, include_unleased_running=True)
    ) is None
    assert await _call(
        shared, lambda s: s.reap_turn(legacy_turn, include_unleased_running=False)
    ) is None
    for session_id, turn_id in ((dead_session, dead_turn), (legacy_session, legacy_turn)):
        termination = await _call(
            shared, lambda s, t=turn_id: s.reap_turn(t, include_unleased_running=True)
        )
        assert (termination.status, termination.session_status) == ("interrupted", "failed")
        assert (await _turn(shared, turn_id)).lease_owner is None
        assert await _session_status(shared, session_id) == "failed"


@pytest.fixture
def wired(pg, monkeypatch: pytest.MonkeyPatch):
    """执行模块的短会话、事件流存储与租约时长都接到测试连接上。"""
    shared, ids = pg
    store = LocalRunEventStore(maxlen=5000)
    monkeypatch.setattr(execution, "AsyncSessionLocal", shared.session)
    monkeypatch.setattr(execution, "get_run_event_store", lambda: store)
    monkeypatch.setattr(execution, "lease_seconds", lambda: 0.6)
    return shared, ids, store


async def _stream(store: LocalRunEventStore, turn_id) -> list[dict]:
    return [event for _, event in (await store.capture(str(turn_id))).entries]


async def test_heartbeat_renews_then_cleared_lease_cancels_execution(wired) -> None:
    shared, ids, store = wired
    session_id, turn_id = await _new_turn(shared, ids)
    owner = execution.new_lease_owner()
    offset = await _call(shared, lambda s: s.claim_turn_lease(turn_id, owner, 0.6))
    gate = asyncio.Event()

    async def body(bus) -> None:
        await gate.wait()

    await execution.launch_turn_execution(
        turn_id=turn_id, session_id=session_id, owner=owner, start_offset=offset, body=body
    )
    claimed_until = (await _turn(shared, turn_id)).lease_expires_at
    await asyncio.sleep(0.5)
    assert (await _turn(shared, turn_id)).lease_expires_at > claimed_until

    # 模拟其他进程的取消:直接清除租约,心跳续约影响 0 行后取消本次执行。
    async with shared.session() as db:
        await db.execute(
            update(SessionTurnRecord)
            .where(SessionTurnRecord.id == turn_id)
            .values(lease_owner=None, lease_expires_at=None)
        )
        await db.commit()
    task = execution.local_execution_task(turn_id)
    assert task is not None
    await asyncio.wait({task}, timeout=3)

    assert task.done() and task.cancelled()
    types = [event["type"] for event in await _stream(store, turn_id)]
    assert types == ["run_started"]  # run_end 由清除租约的一方负责,持有者不再写入
    assert execution.local_execution_task(turn_id) is None


async def test_reaper_finishes_expired_turn_and_writes_run_end(wired) -> None:
    shared, ids, store = wired
    session_id, turn_id = await _new_turn(shared, ids)
    await _call(shared, lambda s: s.claim_turn_lease(turn_id, "crashed-process", 90))
    await _call(shared, lambda s: s.reserve_turn_offsets(turn_id, "crashed-process", 64))
    await _expire(shared, turn_id)

    reaped = await recovery.reap_orphan_turns()

    assert reaped >= 1
    turn = await _turn(shared, turn_id)
    assert turn.status == "interrupted" and turn.lease_owner is None
    assert await _session_status(shared, session_id) == "failed"
    (run_end,) = await _stream(store, turn_id)
    assert run_end == {
        "type": "run_end",
        "turn_id": str(turn_id),
        "status": "interrupted",
        "sequence": turn.last_event_offset,
    }
    assert turn.last_event_offset == 65


async def test_abnormal_end_does_not_override_user_cancel(pg) -> None:
    """用户取消已提交 interrupted+idle;被取消的执行走异常收尾时不得改成 failed。"""
    from runtime.agent import AgentRuntime

    shared, ids = pg
    session_id, turn_id = await _new_turn(shared, ids)
    await _call(shared, lambda s: s.claim_turn_lease(turn_id, "owner-a", 90))
    await _call(shared, lambda s: s.cancel_turn_execution(session_id, turn_id))

    async with shared.session() as db:
        runtime = AgentRuntime.__new__(AgentRuntime)
        runtime.db = db
        runtime.session_service = SessionService(db)
        runtime.lease_owner = "owner-a"
        await runtime._mark_abnormal_end(session_id, turn_id, "interrupted")

    assert (await _turn(shared, turn_id)).status == "interrupted"
    assert await _session_status(shared, session_id) == "idle"


async def test_abnormal_end_still_applies_while_lease_is_owned(pg) -> None:
    from runtime.agent import AgentRuntime

    shared, ids = pg
    session_id, turn_id = await _new_turn(shared, ids)
    await _call(shared, lambda s: s.claim_turn_lease(turn_id, "owner-a", 90))

    async with shared.session() as db:
        runtime = AgentRuntime.__new__(AgentRuntime)
        runtime.db = db
        runtime.session_service = SessionService(db)
        runtime.lease_owner = "owner-a"
        await runtime._mark_abnormal_end(session_id, turn_id, "interrupted")

    assert (await _turn(shared, turn_id)).status == "interrupted"
    assert await _session_status(shared, session_id) == "failed"
