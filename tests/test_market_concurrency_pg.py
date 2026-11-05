"""market 域并发场景的真实 PostgreSQL 集成测试：两个独立会话同时审核同一条目，
以及同一个源对象被并发首次提交。

READ COMMITTED 下双方都看不到对方未提交的改动：审核如果不先锁条目、拿到锁后不重读版本，
两边都会读到 pending 并各自生效。用例让先到者持锁不提交，确认后到者确实在等锁，
再提交先到者，断言后到者拿到 409、最终只有一次审核生效。
并发用例需要真实提交，测试数据在 fixture 收尾时物理删除。库不可达时跳过。
"""

import asyncio
import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest
from sqlalchemy import delete, text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, create_async_engine
from sqlalchemy.pool import NullPool

import database.engine  # noqa: F401  副作用注册全部映射与全局软删过滤
from core.config import settings
from core.errors import AgentException
from market.models import MarketListingRecord
from market.repository import MarketRepository
from market.service import MarketService
from skill.parser import SkillBundle
from skill.repository import SkillRepository
from user.models import UserRecord
from workspace.models import WorkspaceMemberRecord, WorkspaceRecord

pytestmark = pytest.mark.anyio

# 后到者最多等这么久进入锁等待；超时说明它没有被行锁或唯一索引挡住。
_LOCK_WAIT_TIMEOUT_SECONDS = 5.0

_IS_WAITING_FOR_LOCK_SQL = text(
    "SELECT wait_event_type = 'Lock' FROM pg_stat_activity WHERE pid = :pid"
)


async def _wait_until_blocked(engine: AsyncEngine, pid: int) -> None:
    """轮询 pg_stat_activity，直到指定连接进入锁等待。"""
    deadline = asyncio.get_running_loop().time() + _LOCK_WAIT_TIMEOUT_SECONDS
    async with engine.connect() as probe:
        while asyncio.get_running_loop().time() < deadline:
            waiting = await probe.scalar(_IS_WAITING_FOR_LOCK_SQL, {"pid": pid})
            if waiting:
                return
            await asyncio.sleep(0.05)
    raise AssertionError("后到的请求没有进入锁等待")


async def _backend_pid(session: AsyncSession) -> int:
    return await session.scalar(text("SELECT pg_backend_pid()"))


@pytest.fixture
async def env() -> AsyncIterator[SimpleNamespace]:
    """已提交的发布工作区、owner、审核人和一个 Skill；用例结束后物理删除全部测试数据。"""
    engine = create_async_engine(settings.database_url, poolclass=NullPool)
    try:
        async with engine.connect() as probe:
            await probe.execute(text("SELECT 1"))
    except Exception as exc:  # noqa: BLE001  任何连接错误都只用于决定跳过
        await engine.dispose()
        pytest.skip(f"PostgreSQL 不可达(AGENTOS_DATABASE_URL): {type(exc).__name__}")

    suffix = uuid.uuid4().hex[:10]
    user_ids: list[int] = []
    workspace_ids: list[int] = []
    try:
        async with AsyncSession(engine, expire_on_commit=False) as db:
            author = UserRecord(
                email=f"mkc-a-{suffix}@test.local", username=f"mkc-a-{suffix}", password="x"
            )
            reviewer = UserRecord(
                email=f"mkc-r-{suffix}@test.local", username=f"mkc-r-{suffix}", password="x"
            )
            workspace = WorkspaceRecord(
                name=f"mkc-{suffix}", slug=f"mkc-{suffix}", display_name="市场并发测试"
            )
            db.add_all([author, reviewer, workspace])
            await db.flush()
            user_ids.extend([author.id, reviewer.id])
            workspace_ids.append(workspace.id)
            db.add(
                WorkspaceMemberRecord(
                    workspace_id=workspace.id,
                    user_id=author.id,
                    role="owner",
                    joined_at=datetime.now(UTC),
                )
            )
            content = f"---\nname: race-{suffix}\ndescription: 并发测试\n---\n\n正文\n"
            bundle = SkillBundle.from_content(content, {})
            skill = await SkillRepository(db).save(
                None,
                workspace_id=workspace.id,
                name=bundle.name,
                description=bundle.description,
                frontmatter=bundle.parsed.frontmatter,
                version=bundle.version,
                skill_hash=bundle.skill_hash,
                content=bundle.content,
                extra_files=bundle.extra_files,
                actor_user_id=author.id,
            )
            await db.commit()
        yield SimpleNamespace(
            engine=engine,
            workspace_id=workspace.id,
            author_id=author.id,
            reviewer_id=reviewer.id,
            skill_id=skill.id,
        )
    finally:
        # 条目删除时级联删掉版本；工作区删除时级联删掉成员和 Skill，最后才能删用户。
        async with engine.begin() as conn:
            await conn.execute(
                delete(MarketListingRecord).where(
                    MarketListingRecord.publisher_workspace_id.in_(workspace_ids)
                )
            )
            await conn.execute(delete(WorkspaceRecord).where(WorkspaceRecord.id.in_(workspace_ids)))
            await conn.execute(delete(UserRecord).where(UserRecord.id.in_(user_ids)))
        await engine.dispose()


async def _submit(session: AsyncSession, env: SimpleNamespace) -> int:
    version = await MarketService(session).submit(
        env.workspace_id,
        env.author_id,
        kind="skill",
        source_id=env.skill_id,
        category="data",
        release_note=None,
    )
    return version.id


@pytest.mark.parametrize("second_action", ["approve", "reject"])
async def test_concurrent_reviews_only_one_takes_effect(env, second_action: str) -> None:
    async with AsyncSession(env.engine, expire_on_commit=False) as setup:
        version_id = await _submit(setup, env)
        await setup.commit()

    first = AsyncSession(env.engine, expire_on_commit=False)
    second = AsyncSession(env.engine, expire_on_commit=False)
    try:
        # 先到者通过审核，持有条目行锁但还不提交。
        await MarketService(first).approve_version(env.reviewer_id, version_id, None)
        second_pid = await _backend_pid(second)

        second_service = MarketService(second)
        if second_action == "approve":
            second_call = second_service.approve_version(env.reviewer_id, version_id, None)
        else:
            second_call = second_service.reject_version(env.reviewer_id, version_id, "并发驳回")
        second_task = asyncio.create_task(second_call)
        await _wait_until_blocked(env.engine, second_pid)
        await first.commit()

        # 后到者拿到锁后重读版本，发现已不是待审状态。
        with pytest.raises(AgentException) as exc_info:
            await second_task
        assert exc_info.value.status_code == 409
        await second.rollback()
    finally:
        await first.close()
        await second.close()

    async with AsyncSession(env.engine, expire_on_commit=False) as check:
        repo = MarketRepository(check)
        version = await repo.get_version(version_id)
        assert version is not None
        listing = await repo.get_listing(version.listing_id)
        assert listing is not None
        assert (version.status, version.review_comment) == ("approved", None)
        assert (listing.status, listing.current_version_number) == ("listed", 1)


async def test_concurrent_first_submissions_one_gets_409(env) -> None:
    first = AsyncSession(env.engine, expire_on_commit=False)
    second = AsyncSession(env.engine, expire_on_commit=False)
    try:
        # 先到者新建了条目但还不提交；后到者看不到它，插入条目时被源对象的唯一索引挡住。
        first_version_id = await _submit(first, env)
        second_pid = await _backend_pid(second)
        second_task = asyncio.create_task(_submit(second, env))
        await _wait_until_blocked(env.engine, second_pid)
        await first.commit()

        # 唯一索引冲突转成 409 业务错误，不会变成 500。
        with pytest.raises(AgentException) as exc_info:
            await second_task
        assert exc_info.value.status_code == 409
        assert exc_info.value.__cause__ is not None
        await second.rollback()
    finally:
        await first.close()
        await second.close()

    async with AsyncSession(env.engine, expire_on_commit=False) as check:
        repo = MarketRepository(check)
        version = await repo.get_version(first_version_id)
        assert version is not None
        listing = await repo.get_listing_by_source_for_update("skill", env.skill_id)
        assert listing is not None and listing.id == version.listing_id
        assert await repo.get_max_version_number(listing.id) == 1
