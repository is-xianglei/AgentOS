"""Memory Phase 1 的真实 PostgreSQL 集成测试。

使用 AGENTOS_DATABASE_URL 指向的数据库，目标数据库必须已升级到当前 Alembic Head。
"""

import asyncio
from uuid import uuid4

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from core.config import settings
from core.errors import AgentException
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


async def _expect_constraint(session, statement, constraint_name: str) -> None:
    nested = await session.begin_nested()
    try:
        await session.execute(statement)
        await session.flush()
    except IntegrityError as exc:
        await nested.rollback()
        assert constraint_name in str(exc.orig)
    else:
        await nested.rollback()
        pytest.fail(f"数据库没有触发约束: {constraint_name}")


async def _run_postgresql_checks() -> None:
    assert DATABASE_URL is not None
    engine = create_async_engine(_async_url(DATABASE_URL), pool_pre_ping=True)
    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    suffix = uuid4().hex[:12]
    workspace_id = None
    user_id = None
    try:
        async with session_factory() as session:
            user_id = await session.scalar(
                text(
                    "INSERT INTO users (username, email, password) "
                    "VALUES (:username, :email, :password) RETURNING id"
                ),
                {
                    "username": f"memory-user-{suffix}",
                    "email": f"memory-{suffix}@example.com",
                    "password": "not-a-real-password-hash",
                },
            )
            workspace_id = await session.scalar(
                text(
                    "INSERT INTO workspaces "
                    "(name, slug, display_name, workspace_type, suspended, plan, quotas, settings) "
                    "VALUES (:name, :slug, :display_name, 'personal', false, 'free', '{}', '{}') "
                    "RETURNING id"
                ),
                {
                    "name": f"memory-workspace-{suffix}",
                    "slug": f"memory-{suffix}",
                    "display_name": f"Memory {suffix}",
                },
            )
            await session.commit()

        async def create_in_own_transaction(
            memory_key: str,
            memory_type: str,
            name: str,
            description: str,
            body: str,
        ):
            async with session_factory() as session:
                detail = await MemoryService(session).create_memory(
                    workspace_id,
                    user_id,
                    memory_key=memory_key,
                    memory_type=memory_type,
                    name=name,
                    description=description,
                    body=body,
                )
                await session.commit()
                return detail

        created, second = await asyncio.gather(
            create_in_own_transaction(
                "feedback_tabs",
                "feedback",
                "user-preference-tabs",
                "User prefers tabs for indentation",
                "Always use tabs.",
            ),
            create_in_own_transaction(
                "project_context",
                "project",
                "project-context",
                "Current project context",
                "PostgreSQL is the only Memory data source.",
            ),
        )
        memory_id = created.item.id
        space_id = created.item.space_id
        assert second.item.space_id == space_id
        assert sorted([created.catalog_version, second.catalog_version]) == [1, 2]

        async with session_factory() as session:
            duplicate = text(
                "INSERT INTO memory_items "
                "(space_id, memory_key, memory_type, name, description, body) "
                "VALUES (:space_id, 'feedback_tabs', 'feedback', 'duplicate', 'duplicate', 'x')"
            ).bindparams(space_id=space_id)
            await _expect_constraint(
                session,
                duplicate,
                "uq_memory_items_active_memory_key",
            )
            oversized = text(
                "INSERT INTO memory_items "
                "(space_id, memory_key, memory_type, name, description, body) "
                "VALUES (:space_id, 'oversized', 'reference', 'oversized', 'oversized', :body)"
            ).bindparams(space_id=space_id, body="中" * 6000)
            await _expect_constraint(session, oversized, "ck_memory_items_body_bytes")

        async def update_in_own_transaction(body: str) -> tuple[str, int]:
            async with session_factory() as session:
                try:
                    detail = await MemoryService(session).update_memory(
                        workspace_id,
                        user_id,
                        memory_id,
                        expected_version=1,
                        body=body,
                    )
                    await session.commit()
                    return "updated", detail.item.version
                except AgentException as exc:
                    await session.rollback()
                    return "conflict", exc.status_code

        outcomes = await asyncio.gather(
            update_in_own_transaction("Update from transaction A"),
            update_in_own_transaction("Update from transaction B"),
        )
        assert sorted(outcomes) == [("conflict", 409), ("updated", 2)]

        async with session_factory() as session:
            service = MemoryService(session)
            catalog = await service.get_rendered_catalog(workspace_id, user_id)
            assert catalog.snapshot.catalog_version == 3
            assert catalog.content.startswith("- [user-preference-tabs](memory:")
            assert " - User prefers tabs for indentation\n" in catalog.content

            deleted = await service.delete_memory(
                workspace_id,
                user_id,
                memory_id,
                expected_version=2,
            )
            assert deleted.catalog_version == 4
            await session.commit()

        async with session_factory() as session:
            recreated = await MemoryService(session).create_memory(
                workspace_id,
                user_id,
                memory_key="feedback_tabs",
                memory_type="feedback",
                name="replacement",
                description="Replacement after soft delete",
                body="Replacement body.",
            )
            assert recreated.item.id != memory_id
            assert recreated.catalog_version == 5
            revision_count = await session.scalar(
                text("SELECT count(*) FROM memory_revisions WHERE memory_id = :memory_id"),
                {"memory_id": memory_id},
            )
            source_count = await session.scalar(
                text("SELECT count(*) FROM memory_sources WHERE memory_id = :memory_id"),
                {"memory_id": memory_id},
            )
            assert revision_count == 3
            assert source_count == 3
            await session.commit()
    finally:
        if workspace_id is not None or user_id is not None:
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


def test_memory_storage_on_postgresql():
    asyncio.run(_run_postgresql_checks())
