"""switch_workspace 的成员校验测试：Token 作用域不得越权签发。"""

from types import SimpleNamespace

import pytest

# 跨包 relationship 依赖全部实体已登记，实例化 UserRecord 前必须先导入 registry。
import database.registry  # noqa: F401
from auth import service as auth_service_module
from auth.service import AuthService
from core.errors import AgentException
from user.models import UserRecord


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


class _FakeSession:
    """Repository 仅持有 session 引用，本用例不触发真实 SQL。"""


def _user(user_id: int = 7) -> UserRecord:
    user = UserRecord()
    user.id = user_id
    user.email = "member@example.com"
    user.username = "member"
    user.is_deleted = False
    user.suspended = False
    return user


def _build_service(
    monkeypatch: pytest.MonkeyPatch,
    *,
    user: UserRecord | None,
    member_error: str | None,
) -> tuple[AuthService, list[tuple[int, int]]]:
    """构造 AuthService，并把成员校验替换为可断言调用参数的假实现。"""
    calls: list[tuple[int, int]] = []

    class _FakeWorkspaceService:
        def __init__(self, db) -> None:
            self.db = db

        async def require_active_member(self, workspace_id: int, user_id: int):
            calls.append((workspace_id, user_id))
            if member_error is not None:
                raise AgentException.message(member_error)
            return object()

    monkeypatch.setattr(auth_service_module, "WorkspaceService", _FakeWorkspaceService)

    service = AuthService(_FakeSession())

    async def _get_by_id(_: int) -> UserRecord | None:
        return user

    monkeypatch.setattr(service.repo, "get_by_id", _get_by_id)
    return service, calls


@pytest.mark.anyio
async def test_非成员切换工作区被拒绝(monkeypatch: pytest.MonkeyPatch):
    """回归：曾因缺少成员校验，任意登录用户可凭任意 workspace_id 换到该工作区 Token。"""
    service, calls = _build_service(
        monkeypatch,
        user=_user(),
        member_error="当前用户不是该工作区的有效成员",
    )

    with pytest.raises(AgentException):
        await service.switch_workspace(user_id=7, workspace_id=99)

    assert calls == [(99, 7)]


@pytest.mark.anyio
async def test_有效成员切换工作区签发带作用域的_token(monkeypatch: pytest.MonkeyPatch):
    """成员校验通过后才签发 Token，且 workspace_id 必须落进 payload。"""
    import jwt

    from core.config import settings

    service, calls = _build_service(monkeypatch, user=_user(), member_error=None)

    result = await service.switch_workspace(user_id=7, workspace_id=42)

    assert calls == [(42, 7)]
    payload = jwt.decode(
        result["access_token"],
        settings.jwt_secret_key,
        algorithms=[settings.jwt_algorithm],
    )
    assert payload["workspace_id"] == 42
    assert payload["sub"] == "7"
    refresh_payload = jwt.decode(
        result["refresh_token"],
        settings.jwt_secret_key,
        algorithms=[settings.jwt_algorithm],
    )
    assert refresh_payload["workspace_id"] == 42


@pytest.mark.anyio
async def test_刷新令牌保留工作区并实时复核成员关系(monkeypatch: pytest.MonkeyPatch):
    import jwt

    from core.config import settings

    service, calls = _build_service(monkeypatch, user=_user(), member_error=None)
    refresh_token = service._generate_tokens_with_workspace(_user(), 42)["refresh_token"]

    result = await service.refresh_token(refresh_token)

    assert calls == [(42, 7)]
    payload = jwt.decode(
        result["access_token"],
        settings.jwt_secret_key,
        algorithms=[settings.jwt_algorithm],
    )
    assert payload["workspace_id"] == 42


@pytest.mark.anyio
async def test_成员失效后原工作区刷新令牌被拒绝(monkeypatch: pytest.MonkeyPatch):
    service, calls = _build_service(
        monkeypatch,
        user=_user(),
        member_error="当前用户不是该工作区的有效成员",
    )
    refresh_token = service._generate_tokens_with_workspace(_user(), 42)["refresh_token"]

    with pytest.raises(AgentException, match="有效成员"):
        await service.refresh_token(refresh_token)

    assert calls == [(42, 7)]


@pytest.mark.anyio
async def test_旧刷新令牌从实时成员关系选择个人工作区(monkeypatch: pytest.MonkeyPatch):
    import jwt

    from core.config import settings

    class _LegacyWorkspaceService:
        def __init__(self, db) -> None:
            self.db = db

        async def list_user_workspaces(self, user_id: int):
            assert user_id == 7
            return [SimpleNamespace(id=51, workspace_type="personal")]

    monkeypatch.setattr(auth_service_module, "WorkspaceService", _LegacyWorkspaceService)
    service = AuthService(_FakeSession())

    async def _get_by_id(_: int) -> UserRecord:
        return _user()

    monkeypatch.setattr(service.repo, "get_by_id", _get_by_id)
    legacy_refresh_token = service._generate_tokens(_user())["refresh_token"]

    result = await service.refresh_token(legacy_refresh_token)

    payload = jwt.decode(
        result["access_token"],
        settings.jwt_secret_key,
        algorithms=[settings.jwt_algorithm],
    )
    assert payload["workspace_id"] == 51


@pytest.mark.anyio
async def test_用户禁用时不再查询成员关系(monkeypatch: pytest.MonkeyPatch):
    """用户态校验先于成员校验，避免为已禁用账号多查一次工作区。"""
    disabled = _user()
    disabled.suspended = True
    service, calls = _build_service(monkeypatch, user=disabled, member_error=None)

    with pytest.raises(AgentException):
        await service.switch_workspace(user_id=7, workspace_id=42)

    assert calls == []
