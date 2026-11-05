"""market 域三张表的真实 PostgreSQL 约束测试：CHECK 约束、部分唯一索引和 JSONB 列表默认值。

表结构由迁移 0028 建立，这里验证 ORM 声明与库里的约束一致、能按设计拦住非法数据。
连接 .env 中 AGENTOS_DATABASE_URL 指向的库，整个用例包在外层事务里，结束时回滚，不留数据。
每次预期失败的写入放在 SAVEPOINT 里，失败后外层事务可以继续使用。库不可达时跳过并给出原因。
"""

import uuid
from types import SimpleNamespace

import pytest
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.pool import NullPool

import database.engine  # noqa: F401  副作用注册全部映射与全局软删过滤
from agent.repository import AgentRepository
from core.config import settings
from database.base import Base
from market.models import MarketInstallRecord, MarketListingRecord, MarketVersionRecord
from user.models import UserRecord
from workspace.models import WorkspaceRecord

pytestmark = pytest.mark.anyio


@pytest.fixture
async def pg():
    engine = create_async_engine(settings.database_url, poolclass=NullPool)
    try:
        conn = await engine.connect()
    except Exception as exc:  # noqa: BLE001  任何连接错误都只用于决定跳过
        await engine.dispose()
        pytest.skip(f"PostgreSQL 不可达(AGENTOS_DATABASE_URL): {type(exc).__name__}")
    transaction = await conn.begin()
    session = AsyncSession(bind=conn, expire_on_commit=False)
    suffix = uuid.uuid4().hex[:12]
    user = UserRecord(
        email=f"market-model-{suffix}@test.local", username=f"market-model-{suffix}", password="x"
    )
    workspace = WorkspaceRecord(
        name=f"market-model-{suffix}", slug=f"market-model-{suffix}", display_name="市场模型测试"
    )
    session.add_all([user, workspace])
    await session.flush()
    agent = await AgentRepository(session).create(
        workspace_id=workspace.id,
        name="源Agent",
        description=None,
        system_prompt="你是源Agent",
        model_name=None,
        is_enabled=True,
        created_by_user_id=user.id,
    )
    try:
        yield SimpleNamespace(
            db=session,
            user_id=user.id,
            workspace_id=workspace.id,
            agent_id=agent.id,
        )
    finally:
        await session.close()
        await transaction.rollback()
        await conn.close()
        await engine.dispose()


async def _add(db: AsyncSession, record: Base) -> None:
    db.add(record)
    await db.flush()


async def _expect_integrity_error(db: AsyncSession, record: Base) -> None:
    """在 SAVEPOINT 里写入，断言被约束拦下；回滚到保存点后外层事务继续可用。"""
    with pytest.raises(IntegrityError):
        async with db.begin_nested():
            db.add(record)
            await db.flush()


def _listing(env: SimpleNamespace, **fields) -> MarketListingRecord:
    values = {
        "kind": "agent",
        "status": "unlisted",
        "publisher_workspace_id": env.workspace_id,
        "created_by_user_id": env.user_id,
    }
    values.update(fields)
    return MarketListingRecord(**values)


def _version(listing_id: int, version_number: int, status: str) -> MarketVersionRecord:
    return MarketVersionRecord(
        listing_id=listing_id,
        version_number=version_number,
        status=status,
        category="general",
        snapshot={"agent": {"name": "源Agent"}, "skills": []},
        snapshot_hash="0" * 64,
    )


async def test_listing_rejects_unknown_kind_and_status(pg) -> None:
    await _expect_integrity_error(pg.db, _listing(pg, kind="plugin"))
    await _expect_integrity_error(pg.db, _listing(pg, status="hidden"))


async def test_listing_source_and_official_key_are_unique(pg) -> None:
    await _add(pg.db, _listing(pg, source_agent_id=pg.agent_id))
    await _expect_integrity_error(pg.db, _listing(pg, source_agent_id=pg.agent_id))

    official = {"is_official": True, "publisher_workspace_id": None, "created_by_user_id": None}
    await _add(pg.db, _listing(pg, kind="skill", official_key="demo", **official))
    await _expect_integrity_error(
        pg.db, _listing(pg, kind="skill", official_key="demo", **official)
    )
    # 唯一性按 (kind, official_key) 判断，不同类型可以同名。
    await _add(pg.db, _listing(pg, kind="agent", official_key="demo", **official))


async def test_only_one_pending_version_per_listing(pg) -> None:
    listing = _listing(pg)
    await _add(pg.db, listing)
    first = _version(listing.id, 1, "pending")
    await _add(pg.db, first)

    await _expect_integrity_error(pg.db, _version(listing.id, 2, "pending"))
    await _expect_integrity_error(pg.db, _version(listing.id, 1, "approved"))
    await _expect_integrity_error(pg.db, _version(listing.id, 2, "unknown"))

    # 原待审版本撤回后，可以再提交新的待审版本。
    first.status = "withdrawn"
    await pg.db.flush()
    await _add(pg.db, _version(listing.id, 2, "pending"))


async def test_only_one_active_install_per_listing_and_workspace(pg) -> None:
    listing = _listing(pg)
    await _add(pg.db, listing)
    first = MarketInstallRecord(
        listing_id=listing.id,
        workspace_id=pg.workspace_id,
        installed_version_number=1,
        target_agent_id=pg.agent_id,
        installed_by_user_id=pg.user_id,
    )
    await _add(pg.db, first)
    assert first.managed_skill_ids == []

    duplicate = MarketInstallRecord(
        listing_id=listing.id,
        workspace_id=pg.workspace_id,
        installed_version_number=1,
        installed_by_user_id=pg.user_id,
    )
    await _expect_integrity_error(pg.db, duplicate)

    # 唯一索引只约束未软删除的记录。
    first.is_deleted = True
    await pg.db.flush()
    await _add(
        pg.db,
        MarketInstallRecord(
            listing_id=listing.id,
            workspace_id=pg.workspace_id,
            installed_version_number=2,
            installed_by_user_id=pg.user_id,
            managed_skill_ids=[3, 5],
        ),
    )
