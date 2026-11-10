"""平台管理员的接口契约：get_current_platform_admin 的放行与 403，当前用户响应带只读的
is_platform_admin，用户创建、更新和注册请求都不接收这个字段。

不连接数据库：当前用户通过依赖覆盖直接给出。
"""

from datetime import UTC, datetime
from typing import Annotated

import httpx
import pytest
from fastapi import Depends, FastAPI

import database.registry  # noqa: F401  构造 ORM 实例前需全部映射在册
from api import deps
from auth.schemas import RegisterRequest
from core.error_handlers import agent_error_handler
from core.errors import AgentException
from user.models import UserRecord
from user.schemas import UserCreateRequest, UserUpdateRequest

pytestmark = pytest.mark.anyio


def _user(*, is_platform_admin: bool) -> UserRecord:
    """构造内存中的用户；时间字段不会自动生成，需显式给出。"""
    now = datetime(2026, 11, 4, 10, 0, tzinfo=UTC)
    return UserRecord(
        id=1,
        username="platform-user",
        email="platform-user@test.local",
        password="x",
        email_verified=False,
        suspended=False,
        is_platform_admin=is_platform_admin,
        auth_provider="local",
        preferences={},
        created_at=now,
        updated_at=now,
        is_deleted=False,
    )


async def test_platform_admin_dependency_rejects_normal_user() -> None:
    with pytest.raises(AgentException) as exc_info:
        await deps.get_current_platform_admin(_user(is_platform_admin=False))

    assert exc_info.value.status_code == 403
    assert exc_info.value.message == "需要平台管理员权限"


async def test_platform_admin_dependency_returns_platform_admin() -> None:
    user = _user(is_platform_admin=True)

    assert await deps.get_current_platform_admin(user) is user


async def test_platform_admin_route_returns_403_without_workspace() -> None:
    """挂到路由上时，普通用户拿到 403 和统一错误外壳；依赖不读工作区，没有工作区也能判定。"""
    app = FastAPI()
    app.add_exception_handler(AgentException, agent_error_handler)

    @app.get("/probe")
    async def probe(
        user: Annotated[UserRecord, Depends(deps.get_current_platform_admin)],
    ) -> dict[str, int]:
        return {"user_id": user.id}

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        app.dependency_overrides[deps.get_current_active_user] = lambda: _user(
            is_platform_admin=False
        )
        denied = await client.get("/probe")
        app.dependency_overrides[deps.get_current_active_user] = lambda: _user(
            is_platform_admin=True
        )
        allowed = await client.get("/probe")

    assert denied.status_code == 403
    assert denied.json()["error"]["message"] == "需要平台管理员权限"
    assert allowed.status_code == 200
    assert allowed.json() == {"user_id": 1}


async def test_current_user_response_has_platform_admin_flag() -> None:
    from main import create_app

    app = create_app()
    app.dependency_overrides[deps.get_current_active_user] = lambda: _user(is_platform_admin=True)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get("/api/users/me")

    assert response.status_code == 200, response.text
    assert response.json()["data"]["is_platform_admin"] is True


def test_request_schemas_do_not_accept_platform_admin_flag() -> None:
    """请求里带上 is_platform_admin 也会被丢弃，不会传到 Service。"""
    update = UserUpdateRequest.model_validate({"full_name": "新名字", "is_platform_admin": True})
    create = UserCreateRequest.model_validate(
        {
            "username": "new-user",
            "email": "new-user@example.com",
            "password": "secret123",
            "is_platform_admin": True,
        }
    )
    register = RegisterRequest.model_validate(
        {
            "username": "new-user",
            "email": "new-user@example.com",
            "password": "secret123",
            "is_platform_admin": True,
        }
    )

    assert update.model_dump(exclude_unset=True) == {"full_name": "新名字"}
    assert "is_platform_admin" not in create.model_dump()
    assert "is_platform_admin" not in register.model_dump()
