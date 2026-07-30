"""Memory Phase 2 的真实 PostgreSQL 集成测试。"""

import asyncio
from uuid import uuid4

import pytest
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from core.config import settings
from models.session import SessionMessage, SessionRecord, SessionTurnRecord
from repositories.memory_recall_repo import MemoryRecallRepository
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


async def _create_turn(session, session_id: int, user_id: int, workspace_id: int, content: str):
    message = SessionMessage(
        session_id=session_id,
        role="user",
        content=content,
        token_estimate=1,
    )
    session.add(message)
    await session.flush()
    turn = SessionTurnRecord(
        session_id=session_id,
        user_id=user_id,
        workspace_id=workspace_id,
        status="running",
        started_message_id=message.id,
    )
    session.add(turn)
    await session.flush()
    message.turn_id = turn.id
    await session.flush()
    return turn, message


async def _run_postgresql_checks() -> None:
    assert DATABASE_URL is not None
    engine = create_async_engine(_async_url(DATABASE_URL), pool_pre_ping=True)
    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    suffix = uuid4().hex[:12]
    user_id = None
    workspace_id = None
    try:
        async with session_factory() as session:
            extension = await session.scalar(
                text("SELECT 1 FROM pg_extension WHERE extname = 'pg_trgm'")
            )
            assert extension == 1
            user_id = await session.scalar(
                text(
                    "INSERT INTO users (username, email, password) "
                    "VALUES (:username, :email, 'test-hash') RETURNING id"
                ),
                {
                    "username": f"recall-user-{suffix}",
                    "email": f"recall-{suffix}@example.com",
                },
            )
            workspace_id = await session.scalar(
                text(
                    "INSERT INTO workspaces "
                    "(name, slug, display_name, workspace_type, suspended, plan, quotas, settings) "
                    "VALUES (:name, :slug, :name, 'personal', false, 'free', '{}', '{}') "
                    "RETURNING id"
                ),
                {"name": f"recall-{suffix}", "slug": f"recall-{suffix}"},
            )
            conversation = SessionRecord(
                title="Recall test",
                status="running",
                user_id=user_id,
                workspace_id=workspace_id,
                visibility="private",
                extra={},
                shared_with=[],
            )
            session.add(conversation)
            await session.flush()
            turn, raw_message = await _create_turn(
                session,
                conversation.id,
                user_id,
                workspace_id,
                "Please preserve Python tab indentation",
            )
            continuation = SessionMessage(
                session_id=conversation.id,
                turn_id=turn.id,
                role="user",
                content="Stop Hook continuation",
                token_estimate=1,
            )
            session.add(continuation)
            await session.flush()
            concurrent_turn, _ = await _create_turn(
                session,
                conversation.id,
                user_id,
                workspace_id,
                "Concurrent freeze",
            )

            memory_service = MemoryService(session)
            tabs = await memory_service.create_memory(
                workspace_id,
                user_id,
                memory_key="python_tabs",
                memory_type="feedback",
                name="Python tab indentation",
                description="Preserve tabs when editing Python",
                body="Use tabs for this user's Python files.",
            )
            colors = await memory_service.create_memory(
                workspace_id,
                user_id,
                memory_key="color_theme",
                memory_type="user",
                name="Editor color theme",
                description="User prefers a high contrast theme",
                body="Use a high contrast editor theme.",
            )
            catalog = await memory_service.get_rendered_catalog(workspace_id, user_id)
            await session.commit()

        async with session_factory() as session:
            repo = MemoryRecallRepository(session)
            recent = await repo.list_recent_user_texts(
                workspace_id,
                user_id,
                conversation.id,
                raw_message.id,
                limit=3,
            )
            assert recent == ["Please preserve Python tab indentation"]

            candidates = await repo.search_lexical_candidates(
                workspace_id,
                user_id,
                "Python tab indent",
                catalog.snapshot.entries,
                limit=10,
            )
            assert candidates
            assert {item.entry.id for item in candidates} == {
                tabs.item.id,
                colors.item.id,
            }
            old_tabs_entry = next(
                entry for entry in catalog.snapshot.entries if entry.id == tabs.item.id
            )
            old_revision = await repo.get_catalog_revisions(
                workspace_id,
                user_id,
                [old_tabs_entry],
            )
            assert len(old_revision) == 1
            assert old_revision[0].revision == 1

            updated = await MemoryService(session).update_memory(
                workspace_id,
                user_id,
                tabs.item.id,
                expected_version=1,
                body="Updated body must not leak into the old Catalog.",
            )
            assert updated.item.version == 2
            await session.commit()

        async with session_factory() as session:
            repo = MemoryRecallRepository(session)
            stale_candidates = await repo.search_lexical_candidates(
                workspace_id,
                user_id,
                "Python tab indent",
                (old_tabs_entry,),
                limit=10,
            )
            assert stale_candidates == []
            old_revision = await repo.get_catalog_revisions(
                workspace_id,
                user_id,
                [old_tabs_entry],
            )
            assert old_revision[0].body == "Use tabs for this user's Python files."

        async def freeze(value: str):
            async with session_factory() as session:
                context = await MemoryRecallRepository(session).freeze_context(
                    workspace_id,
                    user_id,
                    concurrent_turn.id,
                    space_id=catalog.snapshot.space_id,
                    catalog_version=catalog.snapshot.catalog_version,
                    selected_revision_ids=[colors.revisions[0].id, tabs.revisions[0].id],
                    rendered_catalog=catalog.content,
                    rendered_memories=value,
                    selector_status="selected",
                    degraded_reason=None,
                    byte_count=len(value.encode("utf-8")),
                )
                await session.commit()
                return context.id, context.rendered_memories

        first, second = await asyncio.gather(freeze("first"), freeze("second"))
        assert first[0] == second[0]
        assert first[1] == second[1]

        async with session_factory() as session:
            repo = MemoryRecallRepository(session)
            context = await repo.get_context(
                workspace_id,
                user_id,
                concurrent_turn.id,
                session_id=conversation.id,
            )
            assert context is not None
            assert context.selected_revision_ids == [colors.revisions[0].id, tabs.revisions[0].id]
            assert (
                await repo.get_context(
                    workspace_id,
                    user_id,
                    concurrent_turn.id,
                    session_id=conversation.id + 1,
                )
                is None
            )
            count = await session.scalar(
                text("SELECT count(*) FROM turn_memory_contexts WHERE turn_id = :turn_id"),
                {"turn_id": concurrent_turn.id},
            )
            assert count == 1
            stored_messages = list(
                await session.scalars(
                    select(SessionMessage.content).where(
                        SessionMessage.session_id == conversation.id
                    )
                )
            )
            assert "first" not in stored_messages
            assert "second" not in stored_messages
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


def test_memory_recall_on_postgresql() -> None:
    asyncio.run(_run_postgresql_checks())
