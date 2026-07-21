"""事务边界回归测试。

验证重构后的核心契约:service 层不再各自 commit,提交统一交给请求边界(get_db)。
重点覆盖注册流程的原子性——历史 bug 是 register 中途 commit 了 user,导致后续
工作区创建失败时残留"孤儿用户"。

这些测试用轻量 fake 替身,不连真实数据库(项目模型依赖 PostgreSQL 专属类型),
聚焦于事务提交行为本身。
"""
import asyncio
import types

import pytest

from services.auth_service import AuthService


class FakeSession:
    """记录 commit / flush / rollback 调用次数的假 AsyncSession。"""

    def __init__(self):
        self.commit_count = 0
        self.flush_count = 0
        self.rollback_count = 0

    async def commit(self):
        self.commit_count += 1

    async def flush(self):
        self.flush_count += 1

    async def rollback(self):
        self.rollback_count += 1

    async def refresh(self, obj):
        return obj


class FakeUser:
    def __init__(self):
        self.id = 1
        self.email = "u@example.com"
        self.username = "u"
        self.is_deleted = False
        self.suspended = False


def _make_auth_service(session, monkeypatch, *, workspace_should_fail: bool):
    """构造一个 AuthService,其 repo 与工作区创建被替换为可控替身。"""
    svc = AuthService(session)

    # user_repo:邮箱/用户名唯一性检查返回 None(未占用),create 返回假 user
    async def _none(*a, **k):
        return None

    async def _create_user(*a, **k):
        return FakeUser()

    svc.repo.get_by_email = _none
    svc.repo.get_by_username = _none
    svc.repo.create = _create_user

    # 拦截 WorkspaceService.create_workspace:成功返回带 id 的假对象,失败则抛异常
    from services import workspace_service as ws_module

    async def _create_workspace(self, *a, **k):
        if workspace_should_fail:
            raise RuntimeError("工作区 slug 冲突（模拟失败）")
        return types.SimpleNamespace(id=42)

    monkeypatch.setattr(
        ws_module.WorkspaceService, "create_workspace", _create_workspace
    )
    return svc


def test_register_does_not_commit_midway(monkeypatch):
    """注册成功路径:service 内部不应有任何 commit(提交交给 get_db 边界)。"""
    session = FakeSession()
    svc = _make_auth_service(session, monkeypatch, workspace_should_fail=False)

    result = asyncio.run(
        svc.register(email="u@example.com", username="u", password="secret123")
    )

    assert result["workspace_id"] == 42
    # 关键断言:register 全程不自行 commit
    assert session.commit_count == 0, "register 不应在 service 层 commit"


def test_register_workspace_failure_propagates_without_commit(monkeypatch):
    """工作区创建失败时:异常向上抛,且此前绝不 commit user(否则产生孤儿用户)。"""
    session = FakeSession()
    svc = _make_auth_service(session, monkeypatch, workspace_should_fail=True)

    with pytest.raises(RuntimeError, match="工作区"):
        asyncio.run(
            svc.register(email="u@example.com", username="u", password="secret123")
        )

    # 核心回归点:失败路径下 user 从未被单独提交,交由外层 get_db 整体回滚
    assert session.commit_count == 0, "工作区失败时不应残留已提交的 user"


def test_get_db_commits_on_success():
    """get_db 依赖:正常路径 yield 后自动 commit 一次。"""
    from db import session as db_session

    fake = FakeSession()

    class _FakeSessionCtx:
        async def __aenter__(self):
            return fake

        async def __aexit__(self, *exc):
            return False

    async def _drive():
        orig = db_session.AsyncSessionLocal
        db_session.AsyncSessionLocal = lambda: _FakeSessionCtx()
        try:
            agen = db_session.get_db()
            db = await agen.__anext__()
            assert db is fake
            with pytest.raises(StopAsyncIteration):
                await agen.__anext__()
        finally:
            db_session.AsyncSessionLocal = orig

    asyncio.run(_drive())
    assert fake.commit_count == 1, "get_db 成功路径应 commit 恰好一次"
    assert fake.rollback_count == 0


def test_get_db_rolls_back_on_error():
    """get_db 依赖:处理过程中抛异常则 rollback,不 commit。"""
    from db import session as db_session

    fake = FakeSession()

    class _FakeSessionCtx:
        async def __aenter__(self):
            return fake

        async def __aexit__(self, *exc):
            return False

    async def _drive():
        orig = db_session.AsyncSessionLocal
        db_session.AsyncSessionLocal = lambda: _FakeSessionCtx()
        try:
            agen = db_session.get_db()
            await agen.__anext__()
            # 模拟请求处理中抛错
            with pytest.raises(ValueError):
                await agen.athrow(ValueError("boom"))
        finally:
            db_session.AsyncSessionLocal = orig

    asyncio.run(_drive())
    assert fake.commit_count == 0, "异常路径不应 commit"
    assert fake.rollback_count == 1, "异常路径应 rollback 一次"
