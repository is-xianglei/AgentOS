from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

import database.registry  # noqa: F401
from user.service import UserService


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


@pytest.mark.anyio
async def test_用户更新支持显式置空可空字段() -> None:
    user = SimpleNamespace(
        full_name="成员",
        avatar_url="https://example.com/avatar.png",
        phone="13800138000",
        preferences={"theme": "dark"},
    )
    service = UserService(SimpleNamespace())
    service.get_user = AsyncMock(return_value=user)
    service.repo.update = AsyncMock()

    result = await service.update_user(
        7,
        full_name=None,
        avatar_url=None,
        phone=None,
        preferences=None,
    )

    assert result is user
    assert user.full_name is None
    assert user.avatar_url is None
    assert user.phone is None
    assert user.preferences == {}
    service.repo.update.assert_awaited_once_with(user)


@pytest.mark.anyio
async def test_用户更新保留未传字段() -> None:
    user = SimpleNamespace(
        full_name="成员",
        avatar_url="https://example.com/avatar.png",
        phone="13800138000",
        preferences={"theme": "dark"},
    )
    service = UserService(SimpleNamespace())
    service.get_user = AsyncMock(return_value=user)
    service.repo.update = AsyncMock()

    await service.update_user(7, full_name="新姓名")

    assert user.full_name == "新姓名"
    assert user.avatar_url == "https://example.com/avatar.png"
    assert user.phone == "13800138000"
    assert user.preferences == {"theme": "dark"}
