"""Memory Worker 跨进程协调的集成测试：真实 Redis 与真实 PostgreSQL，连不上就跳过。

- Redis 周期锁：SET NX EX 在过期前只有一个进程抢得到，过期时间等于清理间隔；游标跨进程共享；
- 交还租约：只交还本 Worker 认领中的 Job，退回认领时加的 attempts，状态回到 pending / retry，
  立即可被其他 Worker 认领；别的 Worker 的 Job 与终态 Job 不受影响。
"""

import asyncio
import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.pool import NullPool

import database.engine  # noqa: F401  副作用注册全部映射与全局软删过滤
from core.config import settings
from memory.jobs.maintenance_lock import RedisRetentionCoordinator
from memory.jobs.repository import MemoryJobRepository
from memory.models import MemoryJobRecord, MemorySpaceRecord
from user.models import UserRecord
from workspace.models import WorkspaceRecord

pytestmark = pytest.mark.anyio


# ---------- Redis 周期锁 ----------


async def test_redis_lock_admits_one_process_per_period(redis_test_clients) -> None:
    clients, prefix = redis_test_clients
    first = RedisRetentionCoordinator(clients, key_prefix=prefix)
    second = RedisRetentionCoordinator(clients, key_prefix=prefix)

    results = await asyncio.gather(*(c.try_acquire(1) for c in (first, second, first)))
    ttl_ms = await clients.command.pttl(f"{prefix}:lock")
    blocked = await second.try_acquire(1)
    await asyncio.sleep(1.2)
    after_expiry = await second.try_acquire(1)

    assert sorted(results) == [False, False, True]
    assert 0 < ttl_ms <= 1000
    assert blocked is False
    # 持锁进程不释放，锁到期后下一个周期由任意进程接手。
    assert after_expiry is True


async def test_redis_cursor_is_shared_between_processes(redis_test_clients) -> None:
    clients, prefix = redis_test_clients
    writer = RedisRetentionCoordinator(clients, key_prefix=prefix)
    reader = RedisRetentionCoordinator(clients, key_prefix=prefix)

    assert await reader.load_cursor() == 0
    await writer.save_cursor(321)
    assert await reader.load_cursor() == 321

    await clients.command.set(f"{prefix}:cursor", "not-a-number")
    assert await reader.load_cursor() == 0


# ---------- 交还租约（真实 PostgreSQL） ----------


@pytest.fixture
async def pg_session():
    """单连接外层事务，用例结束整体回滚，不在开发库留下数据。"""
    engine = create_async_engine(settings.database_url, poolclass=NullPool)
    try:
        conn = await engine.connect()
    except Exception as exc:  # noqa: BLE001  任何连接错误都只用于决定跳过
        await engine.dispose()
        pytest.skip(f"PostgreSQL 不可达(AGENTOS_DATABASE_URL): {type(exc).__name__}")
    transaction = await conn.begin()
    db = AsyncSession(bind=conn, expire_on_commit=False, join_transaction_mode="create_savepoint")
    try:
        yield db
    finally:
        await db.close()
        await transaction.rollback()
        await conn.close()
        await engine.dispose()


async def _space(db: AsyncSession) -> MemorySpaceRecord:
    suffix = uuid.uuid4().hex[:12]
    user = UserRecord(
        email=f"worker-{suffix}@test.local", username=f"worker-{suffix}", password="x"
    )
    workspace = WorkspaceRecord(
        name=f"worker-{suffix}", slug=f"worker-{suffix}", display_name="Memory Worker测试"
    )
    db.add_all([user, workspace])
    await db.flush()
    space = MemorySpaceRecord(workspace_id=workspace.id, user_id=user.id)
    db.add(space)
    await db.flush()
    return space


def _job(space_id: int, **values) -> MemoryJobRecord:
    now = datetime.now(UTC)
    fields = {
        "id": uuid.uuid4(),
        "job_type": "dream",
        "space_id": space_id,
        "idempotency_key": f"worker-test:{uuid.uuid4().hex}",
        "status": "running",
        "attempts": 1,
        "max_attempts": 5,
        "available_at": now - timedelta(minutes=5),
        "lease_until": now + timedelta(minutes=5),
        "worker_id": "worker-a",
        "base_catalog_version": 0,
        "started_at": now - timedelta(minutes=1),
        "payload": {},
    }
    fields.update(values)
    return MemoryJobRecord(**fields)


async def test_release_returns_own_running_jobs_to_queue(pg_session: AsyncSession) -> None:
    db = pg_session
    space = await _space(db)
    first_try = _job(space.id, attempts=1)
    retried = _job(space.id, attempts=3, last_error_code="model_timeout")
    expired_lease = _job(space.id, attempts=2, lease_until=datetime.now(UTC) - timedelta(seconds=1))
    other_worker = _job(space.id, worker_id="worker-b")
    finished = _job(space.id, status="succeeded", worker_id=None, lease_until=None)
    db.add_all([first_try, retried, expired_lease, other_worker, finished])
    await db.flush()
    repo = MemoryJobRepository(db)

    released = await repo.release_worker_leases("worker-a")
    # 批量 UPDATE 不同步会话里的对象，用 populate_existing 读回数据库里的真实值。
    reloaded = await db.scalars(
        select(MemoryJobRecord)
        .where(MemoryJobRecord.space_id == space.id)
        .execution_options(populate_existing=True)
    )
    rows = {job.id: job for job in reloaded}

    assert released == 3
    # 第一次就被打断的回到 pending，失败过的回到 retry；被打断的这次不计入尝试次数。
    assert (rows[first_try.id].status, rows[first_try.id].attempts) == ("pending", 0)
    assert (rows[retried.id].status, rows[retried.id].attempts) == ("retry", 2)
    assert rows[retried.id].last_error_code == "model_timeout"
    # 租约已过期但没人接手的也交还：worker_id 仍是自己，说明没有别的 Worker 认领过。
    assert (rows[expired_lease.id].status, rows[expired_lease.id].attempts) == ("retry", 1)
    for job_id in (first_try.id, retried.id, expired_lease.id):
        assert rows[job_id].worker_id is None
        assert rows[job_id].lease_until is None
    assert (rows[other_worker.id].status, rows[other_worker.id].worker_id) == (
        "running",
        "worker-b",
    )
    assert rows[finished.id].status == "succeeded"

    # 交还后立即可被其他 Worker 认领，available_at 用的是 DB 时钟。
    claimed = await repo.claim_available(worker_id="worker-c", lease_seconds=60, limit=10)
    assert {job.id for job in claimed} == {first_try.id, retried.id, expired_lease.id}
