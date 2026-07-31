"""matcher JSONB 的真实 PostgreSQL 集成测试。

agent_type 维度是复用既有 matcher JSONB 列实现的,没有新增列也没有迁移。
这种做法的风险不在 Python 逻辑(已由 test_subagent_permission.py 覆盖),而在
JSONB 的往返:键序、类型保真、NULL 与 {} 的区分。这些只有真库能验,所以本文件
必须连 .env 里配置的 PostgreSQL,不用 SQLite 替代。

隔离方式:每个用例在外层事务里跑,结束回滚,不向库里留数据。
"""

from typing import Any

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

import database.registry  # noqa: F401
from database.engine import engine
from permission.repository import PermissionRepository, matcher_agent_type


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


@pytest.fixture
async def db() -> Any:
    """绑定到单条连接的会话,用例结束整体回滚。

    不 commit:测试不应在开发库里留残留规则,也避免与真实会话的权限判定串扰。
    """
    async with engine.connect() as conn:
        trans = await conn.begin()
        session = AsyncSession(bind=conn, expire_on_commit=False)
        try:
            yield session
        finally:
            await session.close()
            await trans.rollback()


# 用不存在的工具名,避免与开发库里已有的真实规则相互干扰。
TOOL = "__pg_it_probe_tool__"


@pytest.mark.anyio
class TestMatcherRoundTrip:
    """JSONB 往返:写进去的 matcher 必须原样读回来。"""

    async def test_定向规则的matcher可原样读回(self, db: AsyncSession):
        repo = PermissionRepository(db)
        created = await repo.upsert(
            "global", None, TOOL, "allow", matcher={"agent_type": "verification"}
        )
        db.expunge(created)
        reloaded = await repo.get(created.id)
        assert reloaded is not None
        assert reloaded.matcher == {"agent_type": "verification"}
        assert matcher_agent_type(reloaded.matcher) == "verification"

    async def test_通用规则的matcher存为NULL(self, db: AsyncSession):
        # None 与 {} 在 JSONB 里是不同的值,matcher_agent_type 必须都当"未限定"处理。
        repo = PermissionRepository(db)
        created = await repo.upsert("global", None, TOOL, "ask", matcher=None)
        db.expunge(created)
        reloaded = await repo.get(created.id)
        assert reloaded is not None
        assert reloaded.matcher is None
        assert matcher_agent_type(reloaded.matcher) is None

    async def test_空字典matcher也视为未限定(self, db: AsyncSession):
        repo = PermissionRepository(db)
        created = await repo.upsert("global", None, TOOL, "deny", matcher={})
        db.expunge(created)
        reloaded = await repo.get(created.id)
        assert reloaded is not None
        assert matcher_agent_type(reloaded.matcher) is None

    async def test_额外维度不丢失(self, db: AsyncSession):
        # matcher 是开放结构,未来加维度时旧数据不能被 agent_type 逻辑吃掉。
        repo = PermissionRepository(db)
        payload = {"agent_type": "Explore", "path_prefix": "/repo", "nested": {"k": [1, 2]}}
        created = await repo.upsert("global", None, TOOL, "allow", matcher=payload)
        db.expunge(created)
        reloaded = await repo.get(created.id)
        assert reloaded is not None
        assert reloaded.matcher == payload


@pytest.mark.anyio
class TestUpsertDedup:
    """去重键含 matcher.agent_type:定向规则与通用规则互不覆盖。"""

    async def test_定向与通用规则并存(self, db: AsyncSession):
        repo = PermissionRepository(db)
        generic = await repo.upsert("global", None, TOOL, "ask")
        scoped = await repo.upsert(
            "global", None, TOOL, "allow", matcher={"agent_type": "verification"}
        )
        assert generic.id != scoped.id
        assert len(await repo.find_candidates("global", None, TOOL)) == 2

    async def test_同一定向规则重复写入只更新(self, db: AsyncSession):
        repo = PermissionRepository(db)
        first = await repo.upsert(
            "global", None, TOOL, "allow", matcher={"agent_type": "verification"}
        )
        second = await repo.upsert(
            "global", None, TOOL, "deny", matcher={"agent_type": "verification"}
        )
        assert first.id == second.id
        assert second.behavior == "deny"

    async def test_不同agent_type各自独立(self, db: AsyncSession):
        repo = PermissionRepository(db)
        a = await repo.upsert("global", None, TOOL, "allow", matcher={"agent_type": "verification"})
        b = await repo.upsert("global", None, TOOL, "deny", matcher={"agent_type": "Explore"})
        assert a.id != b.id
        assert (await repo.get(a.id)).behavior == "allow"


@pytest.mark.anyio
class TestPrecedenceOnRealDB:
    """判定优先级在真库上复验一遍:Python 侧取舍依赖 find_candidates 的排序。"""

    async def test_定向规则优先于通用规则(self, db: AsyncSession):
        repo = PermissionRepository(db)
        await repo.upsert("global", None, TOOL, "ask")
        await repo.upsert("global", None, TOOL, "allow", matcher={"agent_type": "verification"})
        hit = await repo.find_match("global", None, TOOL, agent_type="verification")
        assert hit is not None and hit.behavior == "allow"

    async def test_主代理不命中定向规则(self, db: AsyncSession):
        # 核心隔离断言:给子代理的放行不能顺带放行主代理。
        repo = PermissionRepository(db)
        await repo.upsert("global", None, TOOL, "ask")
        await repo.upsert("global", None, TOOL, "allow", matcher={"agent_type": "verification"})
        hit = await repo.find_match("global", None, TOOL, agent_type=None)
        assert hit is not None and hit.behavior == "ask"

    async def test_无匹配定向规则时回落通用(self, db: AsyncSession):
        repo = PermissionRepository(db)
        await repo.upsert("global", None, TOOL, "deny")
        await repo.upsert("global", None, TOOL, "allow", matcher={"agent_type": "Explore"})
        hit = await repo.find_match("global", None, TOOL, agent_type="verification")
        assert hit is not None and hit.behavior == "deny"

    async def test_只有定向规则时主代理无匹配(self, db: AsyncSession):
        repo = PermissionRepository(db)
        await repo.upsert("global", None, TOOL, "allow", matcher={"agent_type": "verification"})
        assert await repo.find_match("global", None, TOOL, agent_type=None) is None

    async def test_候选按id倒序最新优先(self, db: AsyncSession):
        repo = PermissionRepository(db)
        await repo.upsert("global", None, TOOL, "ask", matcher={"agent_type": "a"})
        newer = await repo.upsert("global", None, TOOL, "deny", matcher={"agent_type": "b"})
        candidates = await repo.find_candidates("global", None, TOOL)
        assert candidates[0].id == newer.id

    async def test_软删除的规则不再参与判定(self, db: AsyncSession):
        repo = PermissionRepository(db)
        rule = await repo.upsert(
            "global", None, TOOL, "allow", matcher={"agent_type": "verification"}
        )
        assert await repo.delete(rule.id) == 1
        assert await repo.find_match("global", None, TOOL, agent_type="verification") is None
