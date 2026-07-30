"""Memory Phase 3 Job 的真实 PostgreSQL 集成测试。

使用 AGENTOS_DATABASE_URL 指向的数据库，目标数据库必须已升级到当前 Alembic Head。
"""

import asyncio
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from core.config import settings
from models.memory import (
    MemoryItemRecord,
    MemoryJobRecord,
    MemoryRevisionRecord,
    MemorySourceRecord,
    MemorySpaceRecord,
)
from models.session import SessionMessage, SessionRecord, SessionTurnRecord
from repositories.memory_job_repo import MemoryJobRepository
from services.memory_extraction_service import (
    MemoryExtractionResult,
    MemoryExtractionService,
)

DATABASE_URL = settings.database_url
pytestmark = pytest.mark.skipif(
    not DATABASE_URL,
    reason="未配置 AGENTOS_DATABASE_URL，跳过 PostgreSQL 集成测试",
)


@dataclass(frozen=True)
class _JobScope:
    """测试 Job 共享的可信数据库作用域。"""

    user_id: int
    workspace_id: int
    session_id: int
    turn_id: UUID
    space_id: int
    started_message_id: int
    completed_message_id: int


def _async_url(url: str) -> str:
    if url.startswith("postgresql://"):
        return url.replace("postgresql://", "postgresql+psycopg://", 1)
    return url


async def _create_scope(session: AsyncSession, suffix: str) -> _JobScope:
    user_id = await session.scalar(
        text(
            "INSERT INTO users (username, email, password) "
            "VALUES (:username, :email, 'test-hash') RETURNING id"
        ),
        {
            "username": f"memory-job-user-{suffix}",
            "email": f"memory-job-{suffix}@example.com",
        },
    )
    workspace_id = await session.scalar(
        text(
            "INSERT INTO workspaces "
            "(name, slug, display_name, workspace_type, suspended, plan, quotas, settings) "
            "VALUES (:name, :slug, :name, 'personal', false, 'free', '{}', '{}') "
            "RETURNING id"
        ),
        {
            "name": f"memory-job-{suffix}",
            "slug": f"memory-job-{suffix}",
        },
    )
    assert user_id is not None
    assert workspace_id is not None

    conversation = SessionRecord(
        title="Memory Job PostgreSQL test",
        status="idle",
        user_id=user_id,
        workspace_id=workspace_id,
        visibility="private",
        extra={},
        shared_with=[],
    )
    session.add(conversation)
    await session.flush()

    user_message = SessionMessage(
        session_id=conversation.id,
        role="user",
        content="Remember PostgreSQL job semantics.",
        token_estimate=1,
    )
    session.add(user_message)
    await session.flush()

    turn = SessionTurnRecord(
        session_id=conversation.id,
        user_id=user_id,
        workspace_id=workspace_id,
        status="running",
        started_message_id=user_message.id,
    )
    session.add(turn)
    await session.flush()
    user_message.turn_id = turn.id

    assistant_message = SessionMessage(
        session_id=conversation.id,
        turn_id=turn.id,
        role="assistant",
        content=[{"type": "text", "text": "Done."}],
        token_estimate=1,
    )
    session.add(assistant_message)
    await session.flush()
    turn.status = "completed"
    turn.completed_message_id = assistant_message.id
    turn.completed_at = datetime.now(UTC)

    space = MemorySpaceRecord(
        workspace_id=workspace_id,
        user_id=user_id,
        catalog_version=0,
        sessions_since_dream=0,
        settings={},
    )
    session.add(space)
    await session.flush()
    return _JobScope(
        user_id=user_id,
        workspace_id=workspace_id,
        session_id=conversation.id,
        turn_id=turn.id,
        space_id=space.id,
        started_message_id=user_message.id,
        completed_message_id=assistant_message.id,
    )


async def _enqueue(
    session: AsyncSession,
    scope: _JobScope,
    idempotency_key: str,
    *,
    max_attempts: int = 3,
) -> MemoryJobRecord:
    return await MemoryJobRepository(session).enqueue_extract(
        space_id=scope.space_id,
        session_id=scope.session_id,
        turn_id=scope.turn_id,
        idempotency_key=idempotency_key,
        max_attempts=max_attempts,
        model="test-model",
        prompt_version="phase3-test-v1",
        base_catalog_version=0,
        payload={"test": True},
        available_at=datetime.now(UTC) - timedelta(seconds=1),
    )


def _extraction_result(*candidates: dict) -> MemoryExtractionResult:
    """构造已经过严格 Schema 校验、无需调用 LLM 的提取结果。"""
    return MemoryExtractionResult.model_validate({"memories": list(candidates)})


def _candidate(
    *,
    memory_key: str,
    body: str,
    source_message_id: int,
    name: str = "PostgreSQL Job 语义",
    description: str = "持久任务使用 Lease 和幂等键保证可靠执行",
) -> dict:
    return {
        "memory_key": memory_key,
        "type": "project",
        "name": name,
        "description": description,
        "body": body,
        "source_message_ids": [source_message_id],
    }


async def _claim_job(
    session_factory,
    job_id: UUID,
    worker_id: str,
    *,
    lease_seconds: float = 120,
    now: datetime | None = None,
) -> None:
    async with session_factory() as session:
        claimed = await MemoryJobRepository(session).claim_available(
            worker_id=worker_id,
            lease_seconds=lease_seconds,
            limit=1,
            job_id=job_id,
            now=now,
        )
        assert [job.id for job in claimed] == [job_id]
        await session.commit()


async def _apply_and_succeed(
    session_factory,
    job_id: UUID,
    worker_id: str,
    result: MemoryExtractionResult,
):
    async with session_factory() as session:
        service = MemoryExtractionService(session)
        applied = await service.apply(job_id, worker_id, result)
        job = await service.job_repo.require_owned_lease(job_id, worker_id)
        await service.job_repo.mark_succeeded(job, worker_id)
        await session.commit()
        return applied


async def _memory_counts(session: AsyncSession, scope: _JobScope) -> tuple[int, int, int, int]:
    item_count = int(
        (
            await session.scalar(
                select(func.count(MemoryItemRecord.id)).where(
                    MemoryItemRecord.space_id == scope.space_id
                )
            )
        )
        or 0
    )
    revision_count = int(
        (
            await session.scalar(
                select(func.count(MemoryRevisionRecord.id))
                .join(
                    MemoryItemRecord,
                    MemoryRevisionRecord.memory_id == MemoryItemRecord.id,
                )
                .where(MemoryItemRecord.space_id == scope.space_id)
            )
        )
        or 0
    )
    source_count = int(
        (
            await session.scalar(
                select(func.count(MemorySourceRecord.id))
                .join(
                    MemoryItemRecord,
                    MemorySourceRecord.memory_id == MemoryItemRecord.id,
                )
                .where(MemoryItemRecord.space_id == scope.space_id)
            )
        )
        or 0
    )
    catalog_version = int(
        (
            await session.scalar(
                select(MemorySpaceRecord.catalog_version).where(
                    MemorySpaceRecord.id == scope.space_id
                )
            )
        )
        or 0
    )
    return item_count, revision_count, source_count, catalog_version


async def _check_concurrent_enqueue(session_factory, scope: _JobScope, suffix: str) -> None:
    idempotency_key = f"extract:{scope.session_id}:{scope.turn_id}:idempotent-{suffix}"

    async def enqueue_once() -> UUID:
        async with session_factory() as session:
            job = await _enqueue(session, scope, idempotency_key)
            await session.commit()
            return job.id

    job_ids = await asyncio.gather(*(enqueue_once() for _ in range(8)))
    assert len(set(job_ids)) == 1
    job_id = job_ids[0]

    async with session_factory() as session:
        count = await session.scalar(
            select(func.count(MemoryJobRecord.id)).where(
                MemoryJobRecord.idempotency_key == idempotency_key
            )
        )
        assert count == 1

        repo = MemoryJobRepository(session)
        claimed = await repo.claim_available(
            worker_id="idempotency-cleanup",
            lease_seconds=60,
            limit=1,
            job_id=job_id,
        )
        assert [job.id for job in claimed] == [job_id]
        await repo.mark_succeeded(claimed[0], "idempotency-cleanup")
        await session.commit()


async def _check_skip_locked_claims(session_factory, scope: _JobScope, suffix: str) -> None:
    async with session_factory() as session:
        expected_ids = {
            (
                await _enqueue(
                    session,
                    scope,
                    f"extract:{scope.session_id}:{scope.turn_id}:pool-{suffix}-{index}",
                )
            ).id
            for index in range(20)
        }
        await session.commit()

    barrier_lock = asyncio.Lock()
    release = asyncio.Event()
    ready_count = 0

    async def claim(worker_id: str) -> list[UUID]:
        nonlocal ready_count
        async with session_factory() as session:
            jobs = await MemoryJobRepository(session).claim_available(
                worker_id=worker_id,
                lease_seconds=120,
                limit=10,
                job_type="extract",
            )
            async with barrier_lock:
                ready_count += 1
                if ready_count == 2:
                    release.set()
            await asyncio.wait_for(release.wait(), timeout=5)
            claimed_ids = [job.id for job in jobs]
            assert all(job.worker_id == worker_id for job in jobs)
            assert all(job.attempts == 1 for job in jobs)
            await session.commit()
            return claimed_ids

    first_ids, second_ids = await asyncio.gather(claim("worker-a"), claim("worker-b"))
    assert set(first_ids).isdisjoint(second_ids)
    assert set(first_ids) | set(second_ids) == expected_ids
    assert len(first_ids) == len(second_ids) == 10


async def _check_expired_running_reclaim(
    session_factory,
    scope: _JobScope,
    suffix: str,
) -> None:
    async with session_factory() as session:
        job = await _enqueue(
            session,
            scope,
            f"extract:{scope.session_id}:{scope.turn_id}:expired-{suffix}",
        )
        await session.commit()
        job_id = job.id

    expired_at = datetime.now(UTC) - timedelta(minutes=5)
    async with session_factory() as session:
        first = await MemoryJobRepository(session).claim_available(
            worker_id="expired-owner",
            lease_seconds=1,
            limit=1,
            job_id=job_id,
            now=expired_at,
        )
        assert len(first) == 1
        first_started_at = first[0].started_at
        assert first[0].lease_until is not None
        assert first[0].lease_until < datetime.now(UTC)
        await session.commit()

    async with session_factory() as session:
        repo = MemoryJobRepository(session)
        assert await repo.get_owned_lease(job_id, "expired-owner") is None
        reclaimed = await repo.claim_available(
            worker_id="recovery-worker",
            lease_seconds=120,
            limit=1,
            job_id=job_id,
        )
        assert len(reclaimed) == 1
        assert reclaimed[0].worker_id == "recovery-worker"
        assert reclaimed[0].attempts == 2
        assert reclaimed[0].started_at == first_started_at
        await repo.mark_succeeded(reclaimed[0], "recovery-worker")
        await session.commit()


async def _check_lease_and_failure_states(
    session_factory,
    scope: _JobScope,
    suffix: str,
) -> None:
    async with session_factory() as session:
        retry_job = await _enqueue(
            session,
            scope,
            f"extract:{scope.session_id}:{scope.turn_id}:retry-{suffix}",
            max_attempts=2,
        )
        non_retryable_job = await _enqueue(
            session,
            scope,
            f"extract:{scope.session_id}:{scope.turn_id}:dead-{suffix}",
            max_attempts=3,
        )
        await session.commit()
        retry_job_id = retry_job.id
        non_retryable_job_id = non_retryable_job.id

    async with session_factory() as session:
        repo = MemoryJobRepository(session)
        claimed = await repo.claim_available(
            worker_id="lease-owner",
            lease_seconds=120,
            limit=1,
            job_id=retry_job_id,
        )
        assert len(claimed) == 1
        await session.commit()

    async with session_factory() as session:
        repo = MemoryJobRepository(session)
        assert await repo.get_owned_lease(retry_job_id, "other-worker") is None
        with pytest.raises(RuntimeError, match="Lease"):
            await repo.require_owned_lease(retry_job_id, "other-worker")

        owned = await repo.require_owned_lease(retry_job_id, "lease-owner")
        with pytest.raises(RuntimeError, match="Lease"):
            await repo.mark_succeeded(owned, "other-worker")

        retry_at = datetime.now(UTC) - timedelta(seconds=1)
        retried = await repo.mark_failed(
            owned,
            "lease-owner",
            retryable=True,
            available_at=retry_at,
            error_code="TEMPORARY",
            error_message="临时失败",
        )
        assert retried.status == "retry"
        assert retried.finished_at is None
        assert retried.worker_id is None
        assert retried.lease_until is None
        await session.commit()

    async with session_factory() as session:
        repo = MemoryJobRepository(session)
        claimed = await repo.claim_available(
            worker_id="retry-owner",
            lease_seconds=120,
            limit=1,
            job_id=retry_job_id,
        )
        assert len(claimed) == 1
        assert claimed[0].attempts == 2
        dead = await repo.mark_failed(
            claimed[0],
            "retry-owner",
            retryable=True,
            available_at=datetime.now(UTC),
            error_code="EXHAUSTED",
            error_message="达到最大尝试次数",
        )
        assert dead.status == "dead"
        assert dead.finished_at is not None
        assert dead.worker_id is None
        assert dead.lease_until is None
        await session.commit()

    async with session_factory() as session:
        repo = MemoryJobRepository(session)
        claimed = await repo.claim_available(
            worker_id="permanent-owner",
            lease_seconds=120,
            limit=1,
            job_id=non_retryable_job_id,
        )
        assert len(claimed) == 1
        dead = await repo.mark_failed(
            claimed[0],
            "permanent-owner",
            retryable=False,
            available_at=datetime.now(UTC),
            error_code="INVALID_OUTPUT",
            error_message="结构化输出无效",
        )
        assert dead.status == "dead"
        assert dead.attempts == 1
        assert dead.finished_at is not None
        await session.commit()


async def _check_apply_idempotency_and_batch_update(
    session_factory,
    scope: _JobScope,
    suffix: str,
) -> None:
    memory_key = f"postgres-job-semantics-{suffix}"
    base_body = "Memory Job 使用 Lease 和幂等键保证可靠执行。"
    base_result = _extraction_result(
        _candidate(
            memory_key=memory_key,
            body=base_body,
            source_message_id=scope.started_message_id,
        )
    )

    async with session_factory() as session:
        first_job = await _enqueue(
            session,
            scope,
            f"extract:{scope.session_id}:{scope.turn_id}:apply-create-{suffix}",
        )
        await session.commit()
        first_job_id = first_job.id
    await _claim_job(session_factory, first_job_id, "apply-create-worker")
    created = await _apply_and_succeed(
        session_factory,
        first_job_id,
        "apply-create-worker",
        base_result,
    )
    assert len(created.created_ids) == 1
    assert created.updated_ids == ()
    assert created.unchanged_ids == ()
    assert created.catalog_version == 1

    async with session_factory() as session:
        assert await _memory_counts(session, scope) == (1, 1, 1, 1)
        first_job_record = await MemoryJobRepository(session).get_by_id(first_job_id)
        assert first_job_record is not None
        assert first_job_record.status == "succeeded"
        assert first_job_record.worker_id is None
        assert first_job_record.lease_until is None

    async with session_factory() as session:
        replay_job = await _enqueue(
            session,
            scope,
            f"extract:{scope.session_id}:{scope.turn_id}:apply-replay-{suffix}",
        )
        await session.commit()
        replay_job_id = replay_job.id
    await _claim_job(session_factory, replay_job_id, "apply-replay-worker")
    replayed = await _apply_and_succeed(
        session_factory,
        replay_job_id,
        "apply-replay-worker",
        base_result,
    )
    assert replayed.created_ids == ()
    assert replayed.updated_ids == ()
    assert replayed.unchanged_ids == created.created_ids
    assert replayed.catalog_version == 1

    async with session_factory() as session:
        assert await _memory_counts(session, scope) == (1, 1, 1, 1)
        replay_job_record = await MemoryJobRepository(session).get_by_id(replay_job_id)
        assert replay_job_record is not None
        assert replay_job_record.status == "succeeded"

    enhanced_body = f"{base_body} Worker 崩溃后由过期 Lease 恢复。"
    batch_memory_key = f"postgres-job-batch-{suffix}"
    batch_result = _extraction_result(
        _candidate(
            memory_key=memory_key,
            body=enhanced_body,
            source_message_id=scope.completed_message_id,
        ),
        _candidate(
            memory_key=batch_memory_key,
            body="Claim 必须使用 FOR UPDATE SKIP LOCKED。",
            source_message_id=scope.completed_message_id,
            name="PostgreSQL Job Claim",
            description="并发 Worker 使用行锁跳过已经被其他 Worker 认领的任务",
        ),
    )
    async with session_factory() as session:
        batch_job = await _enqueue(
            session,
            scope,
            f"extract:{scope.session_id}:{scope.turn_id}:apply-batch-{suffix}",
        )
        await session.commit()
        batch_job_id = batch_job.id
    await _claim_job(session_factory, batch_job_id, "apply-batch-worker")
    batch_applied = await _apply_and_succeed(
        session_factory,
        batch_job_id,
        "apply-batch-worker",
        batch_result,
    )
    assert batch_applied.updated_ids == created.created_ids
    assert len(batch_applied.created_ids) == 1
    assert batch_applied.catalog_version == 2

    async with session_factory() as session:
        assert await _memory_counts(session, scope) == (2, 3, 3, 2)
        updated_item = (
            await session.scalars(
                select(MemoryItemRecord).where(
                    MemoryItemRecord.space_id == scope.space_id,
                    MemoryItemRecord.memory_key == memory_key,
                )
            )
        ).one()
        assert updated_item.version == 2
        assert updated_item.body == enhanced_body
        batch_job_record = await MemoryJobRepository(session).get_by_id(batch_job_id)
        assert batch_job_record is not None
        assert batch_job_record.status == "succeeded"


async def _check_apply_rejects_lost_or_expired_lease(
    session_factory,
    scope: _JobScope,
    suffix: str,
) -> None:
    async with session_factory() as session:
        baseline = await _memory_counts(session, scope)

    lost_result = _extraction_result(
        _candidate(
            memory_key=f"rejected-lost-lease-{suffix}",
            body="该候选不能由非 Lease 所有者写入。",
            source_message_id=scope.started_message_id,
        )
    )
    async with session_factory() as session:
        lost_job = await _enqueue(
            session,
            scope,
            f"extract:{scope.session_id}:{scope.turn_id}:apply-lost-{suffix}",
        )
        await session.commit()
        lost_job_id = lost_job.id
    await _claim_job(session_factory, lost_job_id, "actual-owner")

    async with session_factory() as session:
        with pytest.raises(RuntimeError, match="Lease"):
            await MemoryExtractionService(session).apply(
                lost_job_id,
                "stale-worker",
                lost_result,
            )
        await session.rollback()

    expired_result = _extraction_result(
        _candidate(
            memory_key=f"rejected-expired-lease-{suffix}",
            body="该候选不能在 Lease 过期后写入。",
            source_message_id=scope.started_message_id,
        )
    )
    async with session_factory() as session:
        expired_job = await _enqueue(
            session,
            scope,
            f"extract:{scope.session_id}:{scope.turn_id}:apply-expired-{suffix}",
        )
        await session.commit()
        expired_job_id = expired_job.id
    await _claim_job(
        session_factory,
        expired_job_id,
        "expired-apply-worker",
        lease_seconds=1,
        now=datetime.now(UTC) - timedelta(minutes=5),
    )

    async with session_factory() as session:
        with pytest.raises(RuntimeError, match="Lease"):
            await MemoryExtractionService(session).apply(
                expired_job_id,
                "expired-apply-worker",
                expired_result,
            )
        await session.rollback()

    async with session_factory() as session:
        assert await _memory_counts(session, scope) == baseline


async def _run_postgresql_checks() -> None:
    assert DATABASE_URL is not None
    engine = create_async_engine(_async_url(DATABASE_URL), pool_pre_ping=True)
    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    suffix = uuid4().hex[:12]
    scope: _JobScope | None = None

    try:
        async with session_factory() as session:
            scope = await _create_scope(session, suffix)
            await session.commit()

        await _check_concurrent_enqueue(session_factory, scope, suffix)
        await _check_skip_locked_claims(session_factory, scope, suffix)
        await _check_expired_running_reclaim(session_factory, scope, suffix)
        await _check_lease_and_failure_states(session_factory, scope, suffix)
        await _check_apply_idempotency_and_batch_update(session_factory, scope, suffix)
        await _check_apply_rejects_lost_or_expired_lease(session_factory, scope, suffix)
    finally:
        if scope is not None:
            async with session_factory() as session:
                await session.execute(
                    text("DELETE FROM users WHERE id = :user_id"),
                    {"user_id": scope.user_id},
                )
                await session.execute(
                    text("DELETE FROM workspaces WHERE id = :workspace_id"),
                    {"workspace_id": scope.workspace_id},
                )
                await session.commit()
        await engine.dispose()


def test_memory_jobs_on_postgresql() -> None:
    asyncio.run(_run_postgresql_checks())
