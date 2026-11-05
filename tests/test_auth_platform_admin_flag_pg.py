"""登录、注册接口响应里的 is_platform_admin：平台首个注册用户为 true，其他用户为 false。

走真实 HTTP 接口和真实 PostgreSQL。开发库里已经有用户，没法直接验证「users 表为空」，
所以沿用 test_platform_admin_pg.py 的做法：在外层事务里建临时 schema，把 users、workspaces、
workspace_members 建在里面并放到 search_path 最前面；用例结束时整体回滚，临时 schema 一并消失。
库不可达时跳过并给出原因。
"""

import uuid

import httpx
import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.pool import NullPool

import database.engine  # noqa: F401  副作用注册全部映射与全局软删过滤
from api.deps import get_db
from auth.schemas import LoginRequest
from core.config import settings
from database.base import Base
from user.models import UserRecord
from workspace.models import WorkspaceMemberRecord, WorkspaceRecord

pytestmark = pytest.mark.anyio

# 测试专用假密码，不对应任何真实账号。
FAKE_PASSWORD = "fake-password-123"


@pytest.fixture
async def auth_client():
    engine = create_async_engine(settings.database_url, poolclass=NullPool)
    try:
        conn = await engine.connect()
    except Exception as exc:  # noqa: BLE001  任何连接错误都只用于决定跳过
        await engine.dispose()
        pytest.skip(f"PostgreSQL 不可达(AGENTOS_DATABASE_URL): {type(exc).__name__}")
    transaction = await conn.begin()
    schema = f"test_auth_admin_flag_{uuid.uuid4().hex[:12]}"
    await conn.execute(text(f'CREATE SCHEMA "{schema}"'))
    await conn.execute(text(f'SET LOCAL search_path TO "{schema}", public'))
    # checkfirst 会把 public 里同名的表当成已存在而跳过，这里必须关掉。
    await conn.run_sync(
        Base.metadata.create_all,
        tables=[UserRecord.__table__, WorkspaceRecord.__table__, WorkspaceMemberRecord.__table__],
        checkfirst=False,
    )
    session = AsyncSession(bind=conn, expire_on_commit=False)

    from main import create_app

    # 请求共用外层事务里的会话且不 commit，写入的数据随外层事务一起回滚。
    async def test_db():
        yield session

    app = create_app()
    app.dependency_overrides[get_db] = test_db
    transport = httpx.ASGITransport(app=app)
    client = httpx.AsyncClient(transport=transport, base_url="http://test")
    try:
        yield client
    finally:
        await client.aclose()
        await session.close()
        await transaction.rollback()
        await conn.close()
        await engine.dispose()


async def _register(
    client: httpx.AsyncClient,
    username: str,
    extra_fields: dict[str, object] | None = None,
) -> httpx.Response:
    payload: dict[str, object] = {
        "email": f"{username}@example.com",
        "username": username,
        "password": FAKE_PASSWORD,
    }
    if extra_fields is not None:
        payload.update(extra_fields)
    return await client.post("/api/auth/register", json=payload)


async def _login(client: httpx.AsyncClient, username: str) -> httpx.Response:
    return await client.post(
        "/api/auth/login",
        json={"email": f"{username}@example.com", "password": FAKE_PASSWORD},
    )


async def test_register_and_login_return_platform_admin_flag(auth_client) -> None:
    first_register = await _register(auth_client, "first-user")
    # 请求里带上 is_platform_admin 也不会生效，第二个用户仍然不是平台管理员。
    second_register = await _register(
        auth_client, "second-user", extra_fields={"is_platform_admin": True}
    )
    first_login = await _login(auth_client, "first-user")
    second_login = await _login(auth_client, "second-user")

    # 响应里有 Token，失败时不打印响应正文。
    assert first_register.status_code == 201
    assert second_register.status_code == 201
    assert first_login.status_code == 200
    assert second_login.status_code == 200
    assert first_register.json()["user"]["is_platform_admin"] is True
    assert second_register.json()["user"]["is_platform_admin"] is False
    assert first_login.json()["user"]["is_platform_admin"] is True
    assert second_login.json()["user"]["is_platform_admin"] is False
    # 其余用户字段保持原样。
    assert first_login.json()["user"]["username"] == "first-user"
    assert first_login.json()["user"]["email"] == "first-user@example.com"


def test_login_request_does_not_accept_platform_admin_flag() -> None:
    login = LoginRequest.model_validate(
        {"email": "user@example.com", "password": FAKE_PASSWORD, "is_platform_admin": True}
    )

    assert "is_platform_admin" not in login.model_dump()
