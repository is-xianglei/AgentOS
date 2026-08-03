from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

import database.registry  # noqa: F401
from user import api as user_api
from user.schemas import UserUpdateRequest


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


def _user_data() -> dict[str, object]:
    now = datetime.now(UTC)
    return {
        "id": 7,
        "username": "member",
        "email": "member@example.com",
        "full_name": "成员",
        "avatar_url": None,
        "phone": "13800138000",
        "email_verified": False,
        "suspended": False,
        "auth_provider": "local",
        "preferences": {"theme": "dark"},
        "last_login_at": None,
        "created_at": now,
        "updated_at": now,
    }


def _request() -> SimpleNamespace:
    return SimpleNamespace(state=SimpleNamespace(request_id="request-1"))


@pytest.mark.anyio
async def test_管理入口按用户更新Schema转发字段(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = SimpleNamespace(update_user=AsyncMock(return_value=_user_data()))
    monkeypatch.setattr(user_api, "UserService", lambda db: service)
    payload = UserUpdateRequest(
        full_name="成员",
        avatar_url="https://example.com/avatar.png",
        phone="13800138000",
        preferences={"theme": "dark"},
    )

    await user_api.update_user(
        user_id=7,
        payload=payload,
        request=_request(),
        db=SimpleNamespace(),
    )

    service.update_user.assert_awaited_once_with(
        user_id=7,
        full_name="成员",
        avatar_url="https://example.com/avatar.png",
        phone="13800138000",
        preferences={"theme": "dark"},
    )


@pytest.mark.anyio
async def test_当前用户入口不再忽略电话和偏好(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = SimpleNamespace(update_user=AsyncMock(return_value=_user_data()))
    monkeypatch.setattr(user_api, "UserService", lambda db: service)
    payload = UserUpdateRequest(
        phone="13800138000",
        preferences={"theme": "dark"},
    )

    await user_api.update_current_user(
        payload=payload,
        request=_request(),
        current_user=SimpleNamespace(id=7),
        db=SimpleNamespace(),
    )

    service.update_user.assert_awaited_once_with(
        user_id=7,
        phone="13800138000",
        preferences={"theme": "dark"},
    )


@pytest.mark.anyio
async def test_我的工作区列表返回当前成员角色(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    now = datetime.now(UTC)
    workspace = SimpleNamespace(
        id=3,
        name="owner-space",
        slug="owner-space",
        display_name="Owner Space",
        logo_url=None,
        workspace_type="team",
        suspended=False,
        industry=None,
        company_size=None,
        billing_email=None,
        plan="free",
        quotas={},
        settings={},
        created_at=now,
        updated_at=now,
    )
    member = SimpleNamespace(workspace=workspace, role="owner")
    service = SimpleNamespace(list_user_workspace_memberships=AsyncMock(return_value=[member]))
    monkeypatch.setattr(user_api, "WorkspaceService", lambda db: service)

    response = await user_api.get_current_user_workspaces(
        request=_request(),
        current_user=SimpleNamespace(id=7),
        db=SimpleNamespace(),
    )

    assert response["data"][0].membership_role == "owner"
