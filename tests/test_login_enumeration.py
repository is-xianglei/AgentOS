"""登录防账号枚举回归测试：密码校验通过前，任何账号状态都不能从错误文案中区分出来。"""

from collections.abc import Callable
from datetime import datetime
from types import SimpleNamespace
from typing import Any, ClassVar

import jwt
import pytest

from auth import service as auth_service_module
from auth.service import AuthService
from core.config import settings
from core.errors import AgentException
from user.service import UserService

pytestmark = pytest.mark.anyio

# 测试专用假值，不对应任何真实账号或密钥。
FAKE_JWT_SECRET = "test-only-fake-jwt-secret-for-login-enumeration"
RIGHT_PASSWORD = "fake-right-password"
WRONG_PASSWORD = "fake-wrong-password"
ACCOUNT_STATES = ("missing", "active", "suspended", "deleted")
WORKSPACE_ID = 42


def _build_users(hash_password: Callable[[str], str]) -> dict[str, SimpleNamespace]:
    """构造三种状态的用户，密码均为 RIGHT_PASSWORD 的哈希；missing 状态不在其中。"""
    users: dict[str, SimpleNamespace] = {}
    for index, state in enumerate(("active", "suspended", "deleted"), start=1):
        users[state] = SimpleNamespace(
            id=index,
            email=f"{state}@example.test",
            username=f"{state}_user",
            password=hash_password(RIGHT_PASSWORD),
            is_deleted=state == "deleted",
            suspended=state == "suspended",
            last_login_at=None,
        )
    return users


class FakeUserRepository:
    """内存版用户仓储；不做软删除过滤，模拟全局过滤失效时的最坏情况。"""

    def __init__(self, users: list[SimpleNamespace]) -> None:
        self.users = users

    async def get_by_email(self, email: str) -> SimpleNamespace | None:
        return next((user for user in self.users if user.email == email), None)

    async def get_by_username(self, username: str) -> SimpleNamespace | None:
        return next((user for user in self.users if user.username == username), None)


class FakeWorkspaceService:
    """只提供登录成功后选择默认工作区所需的方法，并记录调用。"""

    calls: ClassVar[list[int]] = []

    def __init__(self, db: Any) -> None:
        self.db = db

    async def list_user_workspaces(self, user_id: int) -> list[SimpleNamespace]:
        FakeWorkspaceService.calls.append(user_id)
        return [SimpleNamespace(id=WORKSPACE_ID, workspace_type="personal")]


def _error_signature(exc: AgentException) -> tuple[type, str, dict[str, Any], int]:
    return type(exc), exc.message, exc.details, exc.status_code


def _identifier(users: dict[str, SimpleNamespace], state: str, field: str) -> str:
    if state == "missing":
        return "missing@example.test" if field == "email" else "missing_user"
    return getattr(users[state], field)


@pytest.fixture
def auth_env(
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[AuthService, dict[str, SimpleNamespace]]:
    monkeypatch.setattr(settings, "jwt_secret_key", FAKE_JWT_SECRET)
    monkeypatch.setattr(auth_service_module, "WorkspaceService", FakeWorkspaceService)
    monkeypatch.setattr(FakeWorkspaceService, "calls", [])
    service = AuthService(db=None)
    users = _build_users(service._hash_password)
    service.repo = FakeUserRepository(list(users.values()))
    return service, users


@pytest.fixture
def user_env() -> tuple[UserService, dict[str, SimpleNamespace]]:
    service = UserService(db=None)
    users = _build_users(service._hash_password)
    service.repo = FakeUserRepository(list(users.values()))
    return service, users


async def test_login_wrong_password_errors_identical_for_all_states(
    auth_env: tuple[AuthService, dict[str, SimpleNamespace]],
) -> None:
    service, users = auth_env
    signatures: dict[str, tuple[type, str, dict[str, Any], int]] = {}
    for state in ACCOUNT_STATES:
        with pytest.raises(AgentException) as exc_info:
            await service.login(_identifier(users, state, "email"), WRONG_PASSWORD)
        signatures[state] = _error_signature(exc_info.value)

    assert signatures["missing"][1] == "用户名或密码错误"
    assert all(signature == signatures["missing"] for signature in signatures.values()), signatures
    assert FakeWorkspaceService.calls == []
    assert all(user.last_login_at is None for user in users.values())


@pytest.mark.parametrize("state", ACCOUNT_STATES)
async def test_login_wrong_password_hides_account_state(
    auth_env: tuple[AuthService, dict[str, SimpleNamespace]],
    state: str,
) -> None:
    service, users = auth_env
    with pytest.raises(AgentException) as exc_info:
        await service.login(_identifier(users, state, "email"), WRONG_PASSWORD)

    assert exc_info.value.message == "用户名或密码错误"
    assert exc_info.value.status_code == AgentException.status_code


@pytest.mark.parametrize(
    ("state", "message"),
    [("suspended", "账号已被禁用"), ("deleted", "账号已被删除")],
)
async def test_login_right_password_reveals_inactive_state(
    auth_env: tuple[AuthService, dict[str, SimpleNamespace]],
    state: str,
    message: str,
) -> None:
    service, users = auth_env
    with pytest.raises(AgentException) as exc_info:
        await service.login(users[state].email, RIGHT_PASSWORD)

    assert exc_info.value.message == message
    assert users[state].last_login_at is None
    assert FakeWorkspaceService.calls == []


async def test_login_active_right_password_succeeds_and_updates_last_login(
    auth_env: tuple[AuthService, dict[str, SimpleNamespace]],
) -> None:
    service, users = auth_env
    active = users["active"]

    result = await service.login(active.email, RIGHT_PASSWORD)

    logged_in_user = result["user"]
    access_payload = jwt.decode(
        result["access_token"],
        FAKE_JWT_SECRET,
        algorithms=[settings.jwt_algorithm],
    )
    assert logged_in_user is active
    assert result["workspace_id"] == WORKSPACE_ID
    assert access_payload["sub"] == str(active.id)
    assert access_payload["workspace_id"] == WORKSPACE_ID
    assert FakeWorkspaceService.calls == [active.id]
    assert isinstance(active.last_login_at, datetime)
    assert active.last_login_at.tzinfo is not None
    assert users["suspended"].last_login_at is None
    assert users["deleted"].last_login_at is None


@pytest.mark.parametrize("state", ACCOUNT_STATES)
async def test_authenticate_wrong_password_hides_account_state(
    user_env: tuple[UserService, dict[str, SimpleNamespace]],
    state: str,
) -> None:
    service, users = user_env
    assert await service.authenticate(_identifier(users, state, "username"), WRONG_PASSWORD) is None


async def test_authenticate_suspended_right_password_raises(
    user_env: tuple[UserService, dict[str, SimpleNamespace]],
) -> None:
    service, users = user_env
    with pytest.raises(AgentException) as exc_info:
        await service.authenticate(users["suspended"].username, RIGHT_PASSWORD)

    assert exc_info.value.message == "用户已被禁用"


async def test_authenticate_deleted_right_password_returns_none(
    user_env: tuple[UserService, dict[str, SimpleNamespace]],
) -> None:
    service, users = user_env
    assert await service.authenticate(users["deleted"].username, RIGHT_PASSWORD) is None


async def test_authenticate_active_right_password_returns_user(
    user_env: tuple[UserService, dict[str, SimpleNamespace]],
) -> None:
    service, users = user_env
    authenticated = await service.authenticate(users["active"].username, RIGHT_PASSWORD)

    assert authenticated is users["active"]
    assert all(user.last_login_at is None for user in users.values())
