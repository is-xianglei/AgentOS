"""Memory Phase 4 Dream 的真实 PostgreSQL 集成测试。

使用 AGENTOS_DATABASE_URL 指向的数据库，目标数据库必须已升级到当前 Alembic Head。
"""

import asyncio
from collections.abc import Callable
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any
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
from repositories.memory_repo import MemoryRepository
from services.memory_dream_service import (
    MemoryDreamFailure,
    MemoryDreamResult,
    MemoryDreamService,
    PreparedDream,
)
from services.memory_job_service import MemoryJobRunner, MemoryJobService

DATABASE_URL = settings.database_url
pytestmark = pytest.mark.skipif(
    not DATABASE_URL,
    reason="未配置 AGENTOS_DATABASE_URL，跳过 PostgreSQL 集成测试",
)


@dataclass(frozen=True)
class _ConversationScope:
    session_id: int
    turn_id: UUID
    started_message_id: int
    completed_message_id: int


@dataclass(frozen=True)
class _DreamScope:
    user_id: int
    workspace_id: int
    space_id: int
    conversation: _ConversationScope


def _async_url(url: str) -> str:
    if url.startswith("postgresql://"):
        return url.replace("postgresql://", "postgresql+psycopg://", 1)
    return url


@contextmanager
def _dream_settings(**overrides: Any):
    original = {name: getattr(settings, name) for name in overrides}
    try:
        for name, value in overrides.items():
            setattr(settings, name, value)
        yield
    finally:
        for name, value in original.items():
            setattr(settings, name, value)


async def _create_conversation(
    session: AsyncSession,
    *,
    user_id: int,
    workspace_id: int,
    suffix: str,
) -> _ConversationScope:
    conversation = SessionRecord(
        title=f"Memory Dream PostgreSQL {suffix}",
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
        content=f"Dream PostgreSQL test {suffix}",
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
    await session.flush()
    return _ConversationScope(
        session_id=conversation.id,
        turn_id=turn.id,
        started_message_id=user_message.id,
        completed_message_id=assistant_message.id,
    )


async def _create_scope(session: AsyncSession, suffix: str) -> _DreamScope:
    user_id = await session.scalar(
        text(
            "INSERT INTO users (username, email, password) "
            "VALUES (:username, :email, 'test-hash') RETURNING id"
        ),
        {
            "username": f"memory-dream-user-{suffix}",
            "email": f"memory-dream-{suffix}@example.com",
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
            "name": f"memory-dream-{suffix}",
            "slug": f"memory-dream-{suffix}",
        },
    )
    assert user_id is not None
    assert workspace_id is not None
    conversation = await _create_conversation(
        session,
        user_id=user_id,
        workspace_id=workspace_id,
        suffix=suffix,
    )
    space = MemorySpaceRecord(
        workspace_id=workspace_id,
        user_id=user_id,
        catalog_version=0,
        sessions_since_dream=0,
        settings={},
    )
    session.add(space)
    await session.flush()
    return _DreamScope(
        user_id=user_id,
        workspace_id=workspace_id,
        space_id=space.id,
        conversation=conversation,
    )


async def _seed_memory(
    session: AsyncSession,
    scope: _DreamScope,
    *,
    memory_key: str,
    memory_type: str,
    body: str | None = None,
) -> MemoryItemRecord:
    repo = MemoryRepository(session)
    space = await repo.get_space_for_worker(scope.space_id, for_update=True)
    assert space is not None
    item = await repo.create_item(
        scope.workspace_id,
        scope.user_id,
        space=space,
        memory_key=memory_key,
        memory_type=memory_type,
        name=f"标题 {memory_key}",
        description=f"摘要 {memory_key}",
        body=body or f"正文 {memory_key}",
        source_kind="explicit",
    )
    revision = await repo.create_revision(
        scope.workspace_id,
        scope.user_id,
        space,
        item,
        actor_type="user",
        actor_id=str(scope.user_id),
    )
    await repo.create_source(
        scope.workspace_id,
        scope.user_id,
        space=space,
        item=item,
        revision=revision,
        source_kind="explicit",
    )
    await repo.increment_catalog_version(scope.workspace_id, scope.user_id, space)
    return item


async def _seed_operation_memories(
    session: AsyncSession,
    scope: _DreamScope,
) -> dict[str, MemoryItemRecord]:
    definitions = (
        ("user-tabs", "user"),
        ("duplicate-tabs", "feedback"),
        ("project-format", "project"),
        ("obsolete-reference", "reference"),
        ("current-reference", "reference"),
        ("unused-reference", "reference"),
    )
    return {
        memory_key: await _seed_memory(
            session,
            scope,
            memory_key=memory_key,
            memory_type=memory_type,
        )
        for memory_key, memory_type in definitions
    }


async def _enqueue_extract(
    session: AsyncSession,
    scope: _DreamScope,
    conversation: _ConversationScope,
    suffix: str,
) -> MemoryJobRecord:
    space = await MemoryRepository(session).get_space_for_worker(scope.space_id)
    assert space is not None
    return await MemoryJobRepository(session).enqueue_extract(
        space_id=scope.space_id,
        session_id=conversation.session_id,
        turn_id=conversation.turn_id,
        idempotency_key=f"extract:{conversation.session_id}:{conversation.turn_id}:{suffix}",
        max_attempts=3,
        model="test-model",
        prompt_version="phase4-postgresql",
        base_catalog_version=space.catalog_version,
        payload={},
    )


async def _enqueue_dream(
    session: AsyncSession,
    scope: _DreamScope,
    suffix: str,
) -> MemoryJobRecord:
    space = await MemoryRepository(session).get_space_for_worker(scope.space_id)
    assert space is not None
    return await MemoryJobRepository(session).enqueue_dream(
        space_id=scope.space_id,
        idempotency_key=f"dream:{scope.space_id}:{space.catalog_version}:{suffix}",
        max_attempts=3,
        model="test-dream-model",
        prompt_version="phase4-postgresql",
        base_catalog_version=space.catalog_version,
        payload={},
    )


async def _claim_and_prepare(
    session_factory: async_sessionmaker[AsyncSession],
    job_id: UUID,
    worker_id: str,
) -> PreparedDream:
    async with session_factory() as session:
        claimed = await MemoryJobRepository(session).claim_available(
            worker_id=worker_id,
            lease_seconds=300,
            limit=1,
            job_id=job_id,
            job_type="dream",
        )
        assert [job.id for job in claimed] == [job_id]
        await session.commit()
    async with session_factory() as session:
        prepared = await MemoryDreamService(session).prepare(job_id, worker_id)
        await session.commit()
        return prepared


async def _counts(
    session: AsyncSession,
    scope: _DreamScope,
) -> tuple[int, int, int, int]:
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
                .join(MemoryItemRecord)
                .where(MemoryItemRecord.space_id == scope.space_id)
            )
        )
        or 0
    )
    source_count = int(
        (
            await session.scalar(
                select(func.count(MemorySourceRecord.id))
                .join(MemoryItemRecord)
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


async def _run_isolated(
    check: Callable[[async_sessionmaker[AsyncSession], _DreamScope, str], Any],
) -> None:
    assert DATABASE_URL is not None
    engine = create_async_engine(_async_url(DATABASE_URL), pool_pre_ping=True)
    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    suffix = uuid4().hex[:12]
    scope: _DreamScope | None = None
    try:
        async with session_factory() as session:
            scope = await _create_scope(session, suffix)
            await session.commit()
        await check(session_factory, scope, suffix)
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


async def _check_gate_and_concurrent_scan(
    session_factory: async_sessionmaker[AsyncSession],
    scope: _DreamScope,
    suffix: str,
) -> None:
    async with session_factory() as session:
        items = [
            await _seed_memory(
                session,
                scope,
                memory_key=f"gate-{suffix}-{index}",
                memory_type="project",
            )
            for index in range(10)
        ]
        gate_job = await _enqueue_extract(
            session,
            scope,
            scope.conversation,
            f"gate-{suffix}",
        )
        await session.commit()
        gate_job_id = gate_job.id
        low_count_item_id = items[-1].id

    completed_at = datetime.now(UTC).replace(microsecond=0)
    settings_values = {
        "memory_dream_enabled": True,
        "memory_dream_min_items": 10,
        "memory_dream_min_sessions": 5,
        "memory_dream_interval_seconds": 24 * 60 * 60,
        "memory_dream_scan_interval_seconds": 60 * 60,
        "memory_dream_prompt_version": "phase4-postgresql",
        "memory_dream_model": "test-dream-model",
        "memory_job_max_attempts": 3,
    }

    async def run_gate_case(case: str) -> bool:
        async with session_factory() as session:
            service = MemoryJobService(session)
            job = await MemoryJobRepository(session).get_by_id(gate_job_id)
            space = await MemoryRepository(session).get_space_for_worker(
                scope.space_id,
                for_update=True,
            )
            assert job is not None
            assert space is not None
            space.sessions_since_dream = 4
            space.last_dream_at = completed_at - timedelta(hours=25)
            space.last_scan_at = completed_at - timedelta(hours=2)
            if case == "item_count":
                item = await MemoryRepository(session).get_item(
                    scope.workspace_id,
                    scope.user_id,
                    low_count_item_id,
                    for_update=True,
                )
                assert item is not None
                item.status = "archived"
            elif case == "session_count":
                space.sessions_since_dream = 3
            elif case == "dream_interval":
                space.last_dream_at = completed_at - timedelta(hours=23)
            elif case == "scan_interval":
                space.last_scan_at = completed_at - timedelta(minutes=30)
            elif case == "live_dream":
                await _enqueue_dream(session, scope, f"live-{suffix}")

            dream = await service.record_extract_success_and_maybe_enqueue_dream(
                job,
                completed_at=completed_at,
            )
            await session.rollback()
            return dream is not None

    with _dream_settings(**settings_values):
        for case in (
            "item_count",
            "session_count",
            "dream_interval",
            "scan_interval",
            "live_dream",
        ):
            assert await run_gate_case(case) is False, case
        assert await run_gate_case("eligible") is True

        async with session_factory() as session:
            first = await _enqueue_extract(
                session,
                scope,
                scope.conversation,
                f"same-session-first-{suffix}",
            )
            second = await _enqueue_extract(
                session,
                scope,
                scope.conversation,
                f"same-session-second-{suffix}",
            )
            space = await MemoryRepository(session).get_space_for_worker(
                scope.space_id,
                for_update=True,
            )
            assert space is not None
            space.sessions_since_dream = 0
            space.last_dream_at = None
            space.last_scan_at = None
            await session.commit()
            first_id = first.id
            second_id = second.id

        with _dream_settings(memory_dream_enabled=False):
            async with session_factory() as session:
                first = await MemoryJobRepository(session).get_by_id(first_id)
                assert first is not None
                await MemoryJobService(session).record_extract_success_and_maybe_enqueue_dream(
                    first,
                    completed_at=completed_at,
                )
                first.status = "succeeded"
                first.finished_at = completed_at
                await session.commit()

            async with session_factory() as session:
                second = await MemoryJobRepository(session).get_by_id(second_id)
                assert second is not None
                await MemoryJobService(session).record_extract_success_and_maybe_enqueue_dream(
                    second,
                    completed_at=completed_at + timedelta(seconds=1),
                )
                second.status = "succeeded"
                second.finished_at = completed_at + timedelta(seconds=1)
                await session.commit()

        async with session_factory() as session:
            sessions_since_dream = await session.scalar(
                select(MemorySpaceRecord.sessions_since_dream).where(
                    MemorySpaceRecord.id == scope.space_id
                )
            )
            assert sessions_since_dream == 1

        async with session_factory() as session:
            second_conversation = await _create_conversation(
                session,
                user_id=scope.user_id,
                workspace_id=scope.workspace_id,
                suffix=f"parallel-{suffix}",
            )
            first_scan = await _enqueue_extract(
                session,
                scope,
                scope.conversation,
                f"parallel-a-{suffix}",
            )
            second_scan = await _enqueue_extract(
                session,
                scope,
                second_conversation,
                f"parallel-b-{suffix}",
            )
            space = await MemoryRepository(session).get_space_for_worker(
                scope.space_id,
                for_update=True,
            )
            assert space is not None
            space.sessions_since_dream = 4
            space.last_dream_at = completed_at - timedelta(hours=25)
            space.last_scan_at = completed_at - timedelta(hours=2)
            await session.commit()
            scan_job_ids = (first_scan.id, second_scan.id)

        async def scan(job_id: UUID, finished_at: datetime) -> UUID | None:
            async with session_factory() as session:
                job = await MemoryJobRepository(session).get_by_id(job_id)
                assert job is not None
                dream = await MemoryJobService(
                    session
                ).record_extract_success_and_maybe_enqueue_dream(
                    job,
                    completed_at=finished_at,
                )
                job.status = "succeeded"
                job.finished_at = finished_at
                await session.commit()
                return dream.id if dream is not None else None

        scan_results = await asyncio.gather(
            scan(scan_job_ids[0], completed_at + timedelta(hours=3)),
            scan(scan_job_ids[1], completed_at + timedelta(hours=3)),
        )
        assert sum(result is not None for result in scan_results) == 1
        async with session_factory() as session:
            dream_count = await session.scalar(
                select(func.count(MemoryJobRecord.id)).where(
                    MemoryJobRecord.space_id == scope.space_id,
                    MemoryJobRecord.job_type == "dream",
                    MemoryJobRecord.status.in_(("pending", "retry", "running")),
                )
            )
            assert dream_count == 1


class _TrackedSessionFactory:
    def __init__(self, raw_factory: async_sessionmaker[AsyncSession]) -> None:
        self.raw_factory = raw_factory
        self.active_contexts = 0

    def __call__(self):
        owner = self

        class _Context:
            async def __aenter__(self) -> AsyncSession:
                self.session_context = owner.raw_factory()
                session = await self.session_context.__aenter__()
                owner.active_contexts += 1
                return session

            async def __aexit__(self, exc_type, exc, traceback) -> None:
                try:
                    await self.session_context.__aexit__(exc_type, exc, traceback)
                finally:
                    owner.active_contexts -= 1

        return _Context()


class _ConcurrentCatalogLLM:
    def __init__(
        self,
        raw_factory: async_sessionmaker[AsyncSession],
        tracker: _TrackedSessionFactory,
        scope: _DreamScope,
        target_id: int,
        target_key: str,
    ) -> None:
        self.raw_factory = raw_factory
        self.tracker = tracker
        self.scope = scope
        self.target_id = target_id
        self.target_key = target_key
        self.called = False

    async def complete_structured(self, *args: Any, **kwargs: Any) -> MemoryDreamResult:
        assert self.tracker.active_contexts == 0
        self.called = True
        async with self.raw_factory() as session:
            await _seed_memory(
                session,
                self.scope,
                memory_key=f"concurrent-{uuid4().hex[:10]}",
                memory_type="reference",
            )
            await session.commit()
        payload = {
            "operations": [
                {
                    "action": "update",
                    "item_id": self.target_id,
                    "expected_version": 1,
                    "target": {
                        "memory_key": self.target_key,
                        "type": "project",
                        "name": "并发前快照",
                        "description": "该更新必须因 Catalog CAS 失败而丢弃",
                        "body": "不能写入。",
                    },
                }
            ]
        }
        return MemoryDreamResult.model_validate(payload)


async def _check_runner_has_no_llm_transaction_and_cas(
    session_factory: async_sessionmaker[AsyncSession],
    scope: _DreamScope,
    suffix: str,
) -> None:
    async with session_factory() as session:
        item = await _seed_memory(
            session,
            scope,
            memory_key=f"runner-target-{suffix}",
            memory_type="project",
        )
        job = await _enqueue_dream(session, scope, f"runner-cas-{suffix}")
        await session.commit()
        item_id = item.id
        item_key = item.memory_key
        job_id = job.id

    tracker = _TrackedSessionFactory(session_factory)
    llm = _ConcurrentCatalogLLM(session_factory, tracker, scope, item_id, item_key)
    runner = MemoryJobRunner(
        session_factory=tracker,
        llm=llm,
        worker_id=f"runner-{suffix}",
    )
    claimed = await runner._claim_type(
        job_type="dream",
        lease_seconds=300,
        limit=1,
        job_id=job_id,
    )
    assert claimed == ((job_id, "dream"),)
    assert await runner._execute_dream_claimed(job_id) is False
    assert llm.called is True
    assert tracker.active_contexts == 0

    async with session_factory() as session:
        job = await MemoryJobRepository(session).get_by_id(job_id)
        target = await MemoryRepository(session).get_item(
            scope.workspace_id,
            scope.user_id,
            item_id,
        )
        dream_revision_count = await session.scalar(
            select(func.count(MemoryRevisionRecord.id)).where(MemoryRevisionRecord.run_id == job_id)
        )
        assert job is not None
        assert target is not None
        assert job.status == "retry"
        assert job.last_error_code == "catalog_changed"
        assert "dream_audit" not in job.payload
        assert target.version == 1
        assert target.body == f"正文 {item_key}"
        assert dream_revision_count == 0
        assert (await _counts(session, scope))[0] == 2


def _target(
    memory_key: str,
    memory_type: str,
    *,
    body: str,
) -> dict[str, str]:
    return {
        "memory_key": memory_key,
        "type": memory_type,
        "name": f"Dream {memory_key}",
        "description": f"Dream 摘要 {memory_key}",
        "body": body,
    }


def _complete_operations(items: dict[str, MemoryItemRecord]) -> MemoryDreamResult:
    return MemoryDreamResult.model_validate(
        {
            "operations": [
                {
                    "action": "merge",
                    "source_ids": [items["user-tabs"].id, items["duplicate-tabs"].id],
                    "target": _target("user-tabs", "user", body="合并后的用户偏好"),
                },
                {
                    "action": "update",
                    "item_id": items["project-format"].id,
                    "expected_version": 1,
                    "target": _target(
                        "project-format",
                        "project",
                        body="项目统一使用 Ruff 格式化。",
                    ),
                },
                {
                    "action": "supersede",
                    "item_id": items["obsolete-reference"].id,
                    "by_item_id": items["current-reference"].id,
                },
                {
                    "action": "archive",
                    "item_id": items["unused-reference"].id,
                },
            ]
        }
    )


async def _check_apply_replay_and_rollback(
    session_factory: async_sessionmaker[AsyncSession],
    scope: _DreamScope,
    suffix: str,
) -> None:
    async with session_factory() as session:
        items = await _seed_operation_memories(session, scope)
        await session.commit()
        item_ids = {key: item.id for key, item in items.items()}
        original_bodies = {key: item.body for key, item in items.items()}

    async with session_factory() as session:
        bad_job = await _enqueue_dream(session, scope, f"protected-{suffix}")
        await session.commit()
        bad_job_id = bad_job.id
    bad_prepared = await _claim_and_prepare(
        session_factory,
        bad_job_id,
        f"protected-worker-{suffix}",
    )
    bad_result = MemoryDreamResult.model_validate(
        {
            "operations": [
                {
                    "action": "update",
                    "item_id": item_ids["project-format"],
                    "expected_version": 1,
                    "target": _target(
                        "project-format",
                        "project",
                        body="此更新也必须整体回滚。",
                    ),
                },
                {"action": "archive", "item_id": item_ids["user-tabs"]},
            ]
        }
    )
    async with session_factory() as session:
        baseline = await _counts(session, scope)
        with pytest.raises(MemoryDreamFailure) as exc_info:
            await MemoryDreamService(session).apply(
                bad_job_id,
                f"protected-worker-{suffix}",
                bad_prepared,
                bad_result,
            )
        assert exc_info.value.code == "user_memory_protected"
        await session.rollback()
    async with session_factory() as session:
        assert await _counts(session, scope) == baseline
        user_item = await MemoryRepository(session).get_item(
            scope.workspace_id,
            scope.user_id,
            item_ids["user-tabs"],
        )
        project_item = await MemoryRepository(session).get_item(
            scope.workspace_id,
            scope.user_id,
            item_ids["project-format"],
        )
        assert user_item is not None and user_item.status == "active"
        assert project_item is not None and project_item.version == 1

    async with session_factory() as session:
        job = await _enqueue_dream(session, scope, f"apply-{suffix}")
        await session.commit()
        job_id = job.id
    worker_id = f"apply-worker-{suffix}"
    prepared = await _claim_and_prepare(session_factory, job_id, worker_id)
    result = _complete_operations(items)
    async with session_factory() as session:
        applied = await MemoryDreamService(session).apply(job_id, worker_id, prepared, result)
        await session.commit()
    assert applied.operation_count == 4
    assert set(applied.changed_item_ids) == {
        item_ids["user-tabs"],
        item_ids["duplicate-tabs"],
        item_ids["project-format"],
        item_ids["obsolete-reference"],
        item_ids["unused-reference"],
    }
    assert applied.catalog_version == prepared.base_catalog_version + 1

    async with session_factory() as session:
        after_apply_counts = await _counts(session, scope)
        replay_payload = dict((await MemoryJobRepository(session).get_by_id(job_id)).payload)
        with pytest.raises(MemoryDreamFailure) as exc_info:
            await MemoryDreamService(session).apply(job_id, worker_id, prepared, result)
        assert exc_info.value.code == "catalog_changed"
        await session.rollback()
    async with session_factory() as session:
        assert await _counts(session, scope) == after_apply_counts
        replayed_job = await MemoryJobRepository(session).get_by_id(job_id)
        assert replayed_job is not None
        assert replayed_job.payload == replay_payload
        assert "body" not in str(replayed_job.payload["dream_audit"])

        user_item = await MemoryRepository(session).get_item(
            scope.workspace_id, scope.user_id, item_ids["user-tabs"]
        )
        duplicate_item = await MemoryRepository(session).get_item(
            scope.workspace_id, scope.user_id, item_ids["duplicate-tabs"]
        )
        project_item = await MemoryRepository(session).get_item(
            scope.workspace_id, scope.user_id, item_ids["project-format"]
        )
        obsolete_item = await MemoryRepository(session).get_item(
            scope.workspace_id, scope.user_id, item_ids["obsolete-reference"]
        )
        unused_item = await MemoryRepository(session).get_item(
            scope.workspace_id, scope.user_id, item_ids["unused-reference"]
        )
        assert user_item is not None and user_item.body == "合并后的用户偏好"
        assert duplicate_item is not None and duplicate_item.status == "superseded"
        assert duplicate_item.superseded_by_id == item_ids["user-tabs"]
        assert project_item is not None and project_item.body == "项目统一使用 Ruff 格式化。"
        assert obsolete_item is not None and obsolete_item.status == "superseded"
        assert obsolete_item.superseded_by_id == item_ids["current-reference"]
        assert unused_item is not None and unused_item.status == "archived"

        owned_job = await MemoryJobRepository(session).require_owned_lease(job_id, worker_id)
        await MemoryJobRepository(session).mark_succeeded(owned_job, worker_id)
        await session.commit()

    async with session_factory() as session:
        rolled_back = await MemoryDreamService(session).rollback_dream(
            scope.workspace_id,
            scope.user_id,
            job_id,
            expected_after_catalog_version=applied.catalog_version,
            actor_id="postgresql-test",
        )
        await session.commit()
    assert rolled_back.catalog_version == applied.catalog_version + 1
    assert set(rolled_back.restored_item_ids) == set(applied.changed_item_ids)

    async with session_factory() as session:
        for key, item_id in item_ids.items():
            item = await MemoryRepository(session).get_item(
                scope.workspace_id,
                scope.user_id,
                item_id,
            )
            assert item is not None
            assert item.status == "active"
            assert item.superseded_by_id is None
            assert item.body == original_bodies[key]
            expected_version = 1 if key == "current-reference" else 3
            assert item.version == expected_version
        rolled_back_job = await MemoryJobRepository(session).get_by_id(job_id)
        assert rolled_back_job is not None
        assert rolled_back_job.payload["dream_rollback"]["catalog_version"] == (
            rolled_back.catalog_version
        )


async def _check_rollback_rejects_concurrent_change(
    session_factory: async_sessionmaker[AsyncSession],
    scope: _DreamScope,
    suffix: str,
) -> None:
    async with session_factory() as session:
        items = await _seed_operation_memories(session, scope)
        await session.commit()
    async with session_factory() as session:
        job = await _enqueue_dream(session, scope, f"rollback-cas-{suffix}")
        await session.commit()
        job_id = job.id
    worker_id = f"rollback-worker-{suffix}"
    prepared = await _claim_and_prepare(session_factory, job_id, worker_id)
    async with session_factory() as session:
        service = MemoryDreamService(session)
        applied = await service.apply(job_id, worker_id, prepared, _complete_operations(items))
        owned_job = await service.job_repo.require_owned_lease(job_id, worker_id)
        await service.job_repo.mark_succeeded(owned_job, worker_id)
        await session.commit()

    async with session_factory() as session:
        repo = MemoryRepository(session)
        space = await repo.get_space_for_worker(scope.space_id, for_update=True)
        current = await repo.get_item(
            scope.workspace_id,
            scope.user_id,
            items["current-reference"].id,
            for_update=True,
        )
        assert space is not None
        assert current is not None
        await repo.update_item(
            scope.workspace_id,
            scope.user_id,
            space,
            current,
            memory_type=current.memory_type,
            name=current.name,
            description=current.description,
            body="Dream 完成后的并发修改。",
        )
        revision = await repo.create_revision(
            scope.workspace_id,
            scope.user_id,
            space,
            current,
            actor_type="user",
            actor_id=str(scope.user_id),
        )
        await repo.create_source(
            scope.workspace_id,
            scope.user_id,
            space=space,
            item=current,
            revision=revision,
            source_kind="explicit",
        )
        concurrent_catalog_version = await repo.increment_catalog_version(
            scope.workspace_id,
            scope.user_id,
            space,
        )
        await session.commit()

    async with session_factory() as session:
        before = await _counts(session, scope)
        with pytest.raises(MemoryDreamFailure) as exc_info:
            await MemoryDreamService(session).rollback_dream(
                scope.workspace_id,
                scope.user_id,
                job_id,
                expected_after_catalog_version=applied.catalog_version,
            )
        assert exc_info.value.code == "catalog_changed"
        await session.rollback()
    async with session_factory() as session:
        assert await _counts(session, scope) == before
        assert before[3] == concurrent_catalog_version
        job = await MemoryJobRepository(session).get_by_id(job_id)
        assert job is not None
        assert "dream_rollback" not in job.payload


def test_dream_gate_and_concurrent_scan_on_postgresql() -> None:
    asyncio.run(_run_isolated(_check_gate_and_concurrent_scan))


def test_dream_runner_transaction_boundary_and_cas_on_postgresql() -> None:
    asyncio.run(_run_isolated(_check_runner_has_no_llm_transaction_and_cas))


def test_dream_apply_replay_and_rollback_on_postgresql() -> None:
    asyncio.run(_run_isolated(_check_apply_replay_and_rollback))


def test_dream_rollback_rejects_concurrent_change_on_postgresql() -> None:
    asyncio.run(_run_isolated(_check_rollback_rejects_concurrent_change))
