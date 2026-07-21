"""
测试认证 API 功能
"""
import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from main import app


@pytest.fixture
async def client():
    """创建测试客户端"""
    async with AsyncClient(app=app, base_url="http://test") as ac:
        yield ac


class TestAuthAPI:
    """认证 API 测试"""

    async def test_register_user(self, client: AsyncClient):
        """测试用户注册"""
        response = await client.post(
            "/api/auth/register",
            json={
                "username": "testuser",
                "email": "test@example.com",
                "password": "password123",
                "full_name": "Test User",
            },
        )
        assert response.status_code == 201
        data = response.json()
        assert "data" in data
        assert "access_token" in data["data"]
        assert "refresh_token" in data["data"]
        assert data["data"]["user"]["username"] == "testuser"

    async def test_register_duplicate_email(self, client: AsyncClient):
        """测试重复邮箱注册"""
        # 第一次注册
        await client.post(
            "/api/auth/register",
            json={
                "username": "user1",
                "email": "duplicate@example.com",
                "password": "password123",
            },
        )
        # 第二次注册相同邮箱
        response = await client.post(
            "/api/auth/register",
            json={
                "username": "user2",
                "email": "duplicate@example.com",
                "password": "password456",
            },
        )
        assert response.status_code == 400

    async def test_login_success(self, client: AsyncClient):
        """测试登录成功"""
        # 先注册
        await client.post(
            "/api/auth/register",
            json={
                "username": "loginuser",
                "email": "login@example.com",
                "password": "password123",
            },
        )
        # 再登录
        response = await client.post(
            "/api/auth/login",
            json={"email": "login@example.com", "password": "password123"},
        )
        assert response.status_code == 200
        data = response.json()
        assert "data" in data
        assert "access_token" in data["data"]

    async def test_login_wrong_password(self, client: AsyncClient):
        """测试密码错误"""
        await client.post(
            "/api/auth/register",
            json={
                "username": "wrongpass",
                "email": "wrong@example.com",
                "password": "correct123",
            },
        )
        response = await client.post(
            "/api/auth/login",
            json={"email": "wrong@example.com", "password": "wrong123"},
        )
        assert response.status_code == 401

    async def test_get_current_user(self, client: AsyncClient):
        """测试获取当前用户信息"""
        # 注册并获取 token
        reg_response = await client.post(
            "/api/auth/register",
            json={
                "username": "currentuser",
                "email": "current@example.com",
                "password": "password123",
            },
        )
        token = reg_response.json()["data"]["access_token"]

        # 使用 token 获取用户信息
        response = await client.get(
            "/api/users/me", headers={"Authorization": f"Bearer {token}"}
        )
        assert response.status_code == 200
        data = response.json()
        assert data["data"]["username"] == "currentuser"
