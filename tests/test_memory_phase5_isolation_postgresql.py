"""Memory 应用层租户隔离的真实 PostgreSQL 集成测试。"""

import asyncio
from dataclasses import dataclass
from uuid import UUID, uuid4

import jwt
import pytest
from fastapi.security import HTTPAuthorizationCredentials
from sqlalchemy import text
from sqlalchemy.engine import URL, make_url
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from api import deps as api_deps
from core.config import settings
from core.errors import AgentException
from repositories.memory_job_repo import MemoryJobRepository
from repositories.memory_repo import MemoryRepository
from services.memory_job_service import MemoryJobRunner
from services.memory_service import MemoryService

DATABASE_URL = settings.database_url

postgresql_test = pytest.mark.skipif(
    not DATABASE_URL,
    reason="未配置 AGENTOS_DATABASE_URL，跳过 PostgreSQL 集成测试",
)


@dataclass(frozen=True)
class _Scope:
    user_id: int
    workspace_id: int


def _async_url(url: str) -> URL:
    parsed = make_url(url)
    if parsed.drivername == "postgresql":
        return parsed.set(drivername="postgresql+psycopg")
    return parsed


async def _create_scope(session: AsyncSession, suffix: str) -> _Scope:
    user_id = await session.scalar(
        text(
            "INSERT INTO users (username, email, password) "
            "VALUES (:username, :email, 'test-hash') RETURNING id"
        ),
        {
            "username": f"memory-scope-user-{suffix}",
            "email": f"memory-scope-{suffix}@example.com",
        },
    )
    workspace_id = await session.scalar(
        text(
            "INSERT INTO workspaces "
            "(name, slug, display_name, workspace_type, suspended, plan, quotas, settings) "
            "VALUES (:name, :slug, :name, 'personal', false, 'free', '{}', '{}') "
            "RETURNING id"
        ),
        {"name": f"memory-scope-{suffix}", "slug": f"memory-scope-{suffix}"},
    )
    assert user_id is not None
    assert workspace_id is not None
    await session.execute(
        text(
            "INSERT INTO workspace_members (workspace_id, user_id, joined_at) "
            "VALUES (:workspace_id, :user_id, now())"
        ),
        {"workspace_id": workspace_id, "user_id": user_id},
    )
    return _Scope(user_id=user_id, workspace_id=workspace_id)


async def _enqueue_job(
    session: AsyncSession,
    scope: _Scope,
    *,
    suffix: str,
    mode: str,
) -> UUID:
    space = await MemoryRepository(session).get_space(scope.workspace_id, scope.user_id)
    assert space is not None
    job = await MemoryJobRepository(session).enqueue_dream(
        space_id=space.id,
        idempotency_key=f"scope-test:{suffix}:{uuid4().hex}",
        max_attempts=3,
        model="test-model",
        prompt_version="scope-test",
        base_catalog_version=space.catalog_version,
        payload={"mode": mode},
    )
    return job.id


async def _delete_scope(session: AsyncSession, scope: _Scope) -> None:
    await session.execute(
        text("DELETE FROM workspaces WHERE id = :workspace_id"),
        {"workspace_id": scope.workspace_id},
    )
    await session.execute(
        text("DELETE FROM users WHERE id = :user_id"),
        {"user_id": scope.user_id},
    )


async def _run_application_scope_checks() -> None:
    assert DATABASE_URL is not None
    engine = create_async_engine(_async_url(DATABASE_URL), pool_pre_ping=True)
    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    suffix = uuid4().hex[:10]
    scopes: list[_Scope] = []

    try:
        async with session_factory() as session:
            own = await _create_scope(session, f"{suffix}-own")
            other = await _create_scope(session, f"{suffix}-other")
            scopes.extend((own, other))

            own_memory = await MemoryService(session).create_memory(
                own.workspace_id,
                own.user_id,
                memory_key=f"own-{suffix}",
                memory_type="user",
                name="当前作用域记忆",
                description="仅当前用户和工作区可见",
                body="属于当前作用域的正文。",
            )
            other_memory = await MemoryService(session).create_memory(
                other.workspace_id,
                other.user_id,
                memory_key=f"other-{suffix}",
                memory_type="user",
                name="其他作用域记忆",
                description="不得被当前作用域访问",
                body="属于其他作用域的正文。",
            )
            await session.commit()

        async with session_factory() as session:
            service = MemoryService(session)
            page = await service.list_memories(
                own.workspace_id,
                own.user_id,
                status=None,
                memory_type=None,
                limit=20,
                offset=0,
            )
            assert [item.id for item in page.items] == [own_memory.item.id]
            assert page.total == 1

            with pytest.raises(AgentException) as get_error:
                await service.get_memory(
                    own.workspace_id,
                    own.user_id,
                    other_memory.item.id,
                )
            assert get_error.value.status_code == 404

            with pytest.raises(AgentException) as update_error:
                await service.update_memory(
                    own.workspace_id,
                    own.user_id,
                    other_memory.item.id,
                    expected_version=other_memory.item.version,
                    body="越权修改",
                )
            assert update_error.value.status_code == 404

            with pytest.raises(AgentException) as delete_error:
                await service.delete_memory(
                    own.workspace_id,
                    own.user_id,
                    other_memory.item.id,
                    expected_version=other_memory.item.version,
                )
            assert delete_error.value.status_code == 404

            repo = MemoryRepository(session)
            assert await repo.get_space(own.workspace_id, other.user_id) is None
            assert (
                await repo.get_item(
                    own.workspace_id,
                    own.user_id,
                    other_memory.item.id,
                )
                is None
            )
            await repo.touch_used_items(
                own.workspace_id,
                own.user_id,
                [other_memory.item.id],
            )
            await session.commit()

        async with session_factory() as session:
            other_after = await MemoryService(session).get_memory(
                other.workspace_id,
                other.user_id,
                other_memory.item.id,
            )
            assert other_after.item.body == "属于其他作用域的正文。"
            assert other_after.item.use_count == 0

            global_mode = f"global-{suffix}"
            global_own = await _enqueue_job(
                session,
                own,
                suffix=f"{suffix}-global-own",
                mode=global_mode,
            )
            global_other = await _enqueue_job(
                session,
                other,
                suffix=f"{suffix}-global-other",
                mode=global_mode,
            )
            scoped_own = await _enqueue_job(
                session,
                own,
                suffix=f"{suffix}-scoped-own",
                mode=f"scoped-own-{suffix}",
            )
            scoped_other = await _enqueue_job(
                session,
                other,
                suffix=f"{suffix}-scoped-other",
                mode=f"scoped-other-{suffix}",
            )
            await session.commit()

        global_runner = MemoryJobRunner(
            session_factory=session_factory,
            llm=object(),
            worker_id=f"global-worker-{suffix}",
        )
        globally_claimed = await global_runner._claim_type(
            job_type="dream",
            lease_seconds=60,
            limit=10,
            job_id=None,
            payload_mode=global_mode,
        )
        assert {job_id for job_id, _ in globally_claimed} == {global_own, global_other}

        inline_runner = MemoryJobRunner(
            session_factory=session_factory,
            llm=object(),
            worker_id=f"inline-worker-{suffix}",
            claim_scope=(own.workspace_id, own.user_id),
        )
        assert (
            await inline_runner._claim_type(
                job_type="dream",
                lease_seconds=60,
                limit=1,
                job_id=scoped_other,
            )
            == ()
        )
        assert await inline_runner._claim_type(
            job_type="dream",
            lease_seconds=60,
            limit=1,
            job_id=scoped_own,
        ) == ((scoped_own, "dream"),)
    finally:
        async with session_factory() as session:
            for scope in scopes:
                await _delete_scope(session, scope)
            await session.commit()
        await engine.dispose()


@postgresql_test
def test_memory_application_scope_and_worker_claim_on_postgresql() -> None:
    asyncio.run(_run_application_scope_checks())


def test_workspace_scope_rejects_invalid_or_missing_workspace_token() -> None:
    async def run() -> None:
        current_user = type("CurrentUser", (), {"id": 7})()
        invalid = HTTPAuthorizationCredentials(scheme="Bearer", credentials="not-a-jwt")
        with pytest.raises(AgentException) as invalid_error:
            await api_deps.get_current_workspace_id(invalid, current_user, object())
        assert invalid_error.value.status_code == 401

        token = jwt.encode(
            {"sub": "7"},
            settings.jwt_secret_key,
            algorithm=settings.jwt_algorithm,
        )
        missing = HTTPAuthorizationCredentials(scheme="Bearer", credentials=token)
        with pytest.raises(AgentException) as missing_error:
            await api_deps.get_current_workspace_id(missing, current_user, object())
        assert missing_error.value.status_code == 401

    asyncio.run(run())


def test_workspace_scope_rejects_inactive_member_as_forbidden(monkeypatch) -> None:
    async def reject_member(self, workspace_id: int, user_id: int) -> None:
        raise AgentException.message("当前用户不是该工作区的有效成员")

    monkeypatch.setattr(
        api_deps.WorkspaceService,
        "require_active_member",
        reject_member,
    )

    async def run() -> None:
        token = jwt.encode(
            {"sub": "7", "workspace_id": 9},
            settings.jwt_secret_key,
            algorithm=settings.jwt_algorithm,
        )
        credentials = HTTPAuthorizationCredentials(scheme="Bearer", credentials=token)
        with pytest.raises(AgentException) as denied:
            await api_deps.get_current_workspace_id(
                credentials,
                type("CurrentUser", (), {"id": 7})(),
                object(),
            )
        assert denied.value.status_code == 403

    asyncio.run(run())
