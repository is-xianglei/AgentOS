"""Memory Phase 5 数据生命周期的真实 PostgreSQL 集成测试。"""

import asyncio
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from api.memories import _to_export_response
from core.config import settings
from models.memory import MemoryJobRecord, TurnMemoryContextRecord
from models.session import SessionMessage, SessionRecord, SessionTurnRecord
from repositories.memory_job_repo import MemoryJobRepository
from repositories.memory_repo import MemoryPhysicalDeleteCounts, MemoryRepository
from services.memory_retention_service import MemoryRetentionService
from services.memory_service import MemoryService

DATABASE_URL = settings.database_url
pytestmark = pytest.mark.skipif(
    not DATABASE_URL,
    reason="未配置 AGENTOS_DATABASE_URL，跳过 PostgreSQL 集成测试",
)


def _async_url(url: str) -> str:
    if url.startswith("postgresql://"):
        return url.replace("postgresql://", "postgresql+psycopg://", 1)
    return url


async def _create_turn(session, user_id: int, workspace_id: int, suffix: str):
    conversation = SessionRecord(
        title=f"Memory Phase 5 {suffix}",
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
        content="remember phase 5",
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
        content=[{"type": "text", "text": "done"}],
        token_estimate=1,
    )
    session.add(assistant_message)
    await session.flush()
    turn.status = "completed"
    turn.completed_message_id = assistant_message.id
    turn.completed_at = datetime.now(UTC)
    await session.flush()
    return conversation, turn


async def _seed_jobs(session, space_id: int, turn, conversation, old_at: datetime) -> UUID:
    for status in ("pending", "running", "retry", "succeeded", "dead"):
        terminal = status in {"succeeded", "dead"}
        session.add(
            MemoryJobRecord(
                job_type="extract",
                space_id=space_id,
                session_id=conversation.id,
                turn_id=turn.id,
                idempotency_key=f"phase5:{space_id}:{status}:{uuid4().hex}",
                status=status,
                attempts=1 if status != "pending" else 0,
                max_attempts=5,
                available_at=old_at,
                lease_until=None,
                worker_id=None,
                model="test-model",
                prompt_version="phase5",
                base_catalog_version=0,
                started_at=old_at if status != "pending" else None,
                finished_at=old_at if terminal else None,
                payload={},
            )
        )
    shadow = MemoryJobRecord(
        job_type="extract",
        space_id=space_id,
        session_id=conversation.id,
        turn_id=turn.id,
        idempotency_key=f"phase5:{space_id}:shadow:{uuid4().hex}",
        status="pending",
        attempts=0,
        max_attempts=5,
        available_at=old_at,
        model="test-model",
        prompt_version="phase5",
        base_catalog_version=0,
        payload={"mode": "shadow"},
    )
    session.add(shadow)
    await session.flush()
    return shadow.id


async def _run_checks() -> None:
    assert DATABASE_URL is not None
    engine = create_async_engine(_async_url(DATABASE_URL), pool_pre_ping=True)
    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    suffix = uuid4().hex[:12]
    user_id = None
    workspace_id = None
    try:
        async with session_factory() as session:
            user_id = await session.scalar(
                text(
                    "INSERT INTO users (username, email, password) "
                    "VALUES (:username, :email, 'test-hash') RETURNING id"
                ),
                {
                    "username": f"memory-phase5-{suffix}",
                    "email": f"memory-phase5-{suffix}@example.com",
                },
            )
            workspace_id = await session.scalar(
                text(
                    "INSERT INTO workspaces "
                    "(name, slug, display_name, workspace_type, suspended, plan, quotas, settings) "
                    "VALUES (:name, :slug, :name, 'personal', false, 'free', '{}', '{}') "
                    "RETURNING id"
                ),
                {"name": f"memory-phase5-{suffix}", "slug": f"memory-phase5-{suffix}"},
            )
            assert user_id is not None
            assert workspace_id is not None

            service = MemoryService(session)
            archived = await service.create_memory(
                workspace_id,
                user_id,
                memory_key="phase5-archived",
                memory_type="reference",
                name="待清理记忆",
                description="验证软删除记录也可执行保留策略",
                body="这段正文必须先导出，随后由保留策略物理删除。",
            )
            active = await service.create_memory(
                workspace_id,
                user_id,
                memory_key="phase5-active",
                memory_type="project",
                name="保留记忆",
                description="验证 active 记录不会被保留策略删除",
                body="这段正文应保留到完整 Space 被彻底删除。",
            )
            await service.delete_memory(
                workspace_id,
                user_id,
                archived.item.id,
                expected_version=archived.item.version,
            )
            space = await MemoryRepository(session).get_space(
                workspace_id,
                user_id,
                for_update=True,
            )
            assert space is not None
            space.settings = {
                "archived_item_retention_days": 1,
                "terminal_job_retention_days": 1,
            }
            old_at = datetime.now(UTC) - timedelta(days=10)
            await session.execute(
                text("UPDATE memory_items SET updated_at = :old_at WHERE id = :item_id"),
                {"old_at": old_at, "item_id": archived.item.id},
            )
            conversation, turn = await _create_turn(session, user_id, workspace_id, suffix)
            context = TurnMemoryContextRecord(
                turn_id=turn.id,
                space_id=space.id,
                catalog_version=space.catalog_version,
                selected_revision_ids=[active.revisions[0].id],
                rendered_catalog="- [保留记忆](memory:1@1) - 验证导出",
                rendered_memories="需要被彻底删除的冻结 Memory 正文",
                selector_status="selected",
                byte_count=64,
            )
            session.add(context)
            _, orphan_turn = await _create_turn(
                session,
                user_id,
                workspace_id,
                f"{suffix}-orphan",
            )
            orphan_context = TurnMemoryContextRecord(
                turn_id=orphan_turn.id,
                space_id=None,
                catalog_version=space.catalog_version,
                selected_revision_ids=[],
                rendered_catalog="孤立上下文目录",
                rendered_memories="space_id为空但仍属于当前租户的冻结正文",
                selector_status="empty",
                byte_count=32,
            )
            session.add(orphan_context)
            shadow_job_id = await _seed_jobs(session, space.id, turn, conversation, old_at)
            await session.commit()
            space_id = space.id
            context_id = context.id
            orphan_context_id = orphan_context.id

        async with session_factory() as session:
            claimed = await MemoryJobRepository(session).claim_available(
                worker_id="phase5-shadow-worker",
                lease_seconds=300,
                limit=10,
                job_type="extract",
                payload_mode="shadow",
            )
            assert [job.id for job in claimed] == [shadow_job_id]
            await session.commit()

            service = MemoryService(session)
            export_result = await service.export_memories(workspace_id, user_id)
            response = _to_export_response(
                export_result,
                workspace_id=workspace_id,
                user_id=user_id,
            )
            assert response.space is not None
            assert response.space.is_deleted is False
            assert response.space.deleted_at is None
            assert len(response.items) == 2
            assert len(response.jobs) == 6
            assert len(response.contexts) == 2
            assert {context.rendered_memories for context in response.contexts} == {
                "需要被彻底删除的冻结 Memory 正文",
                "space_id为空但仍属于当前租户的冻结正文",
            }
            archived_export = next(item for item in response.items if item.id == archived.item.id)
            assert archived_export.is_deleted is True
            assert len(archived_export.revisions) == 2
            assert len(archived_export.sources) == 2

            other_scope = await service.export_memories(workspace_id, user_id + 1_000_000)
            assert other_scope.bundle.space is None
            assert other_scope.bundle.items == ()

            retention = await MemoryRetentionService(session).cleanup_batch(
                now=datetime.now(UTC),
                batch_size=100,
                space_id=space_id,
            )
            assert retention.counts == MemoryPhysicalDeleteCounts(
                items=1,
                revisions=2,
                sources=2,
                jobs=2,
            )
            statuses = await session.execute(
                text(
                    "SELECT status, count(*) FROM memory_jobs WHERE space_id = :space_id "
                    "GROUP BY status ORDER BY status"
                ),
                {"space_id": space_id},
            )
            assert dict(statuses.all()) == {"pending": 1, "retry": 1, "running": 2}

            confirmation = await service.issue_purge_confirmation(workspace_id, user_id)
            purge_counts = await service.purge_memories(
                workspace_id,
                user_id,
                confirmation_token=confirmation.confirmation_token,
                expected_catalog_version=confirmation.expected_catalog_version,
            )
            assert purge_counts == MemoryPhysicalDeleteCounts(
                spaces=1,
                items=1,
                revisions=1,
                sources=1,
                jobs=4,
                contexts=2,
            )
            remaining = await session.scalar(
                text(
                    "SELECT count(*) FROM turn_memory_contexts "
                    "WHERE id IN (:context_id, :orphan_context_id) OR space_id = :space_id"
                ),
                {
                    "context_id": context_id,
                    "orphan_context_id": orphan_context_id,
                    "space_id": space_id,
                },
            )
            assert remaining == 0
            await session.commit()
    finally:
        if user_id is not None or workspace_id is not None:
            async with session_factory() as session:
                if user_id is not None:
                    await session.execute(
                        text("DELETE FROM users WHERE id = :user_id"),
                        {"user_id": user_id},
                    )
                if workspace_id is not None:
                    await session.execute(
                        text("DELETE FROM workspaces WHERE id = :workspace_id"),
                        {"workspace_id": workspace_id},
                    )
                await session.commit()
        await engine.dispose()


def test_memory_phase5_lifecycle_on_postgresql():
    asyncio.run(_run_checks())
