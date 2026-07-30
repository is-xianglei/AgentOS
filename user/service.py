import hashlib
from sqlalchemy.ext.asyncio import AsyncSession

from core.errors import AgentException
from user.models import UserRecord
from user.repository import UserRepository


class UserService:
    def __init__(self, db: AsyncSession):
        self.db = db
        self.repo = UserRepository(db)

    def _hash_password(self, password: str) -> str:
        """密码哈希（MD5）"""
        return hashlib.md5(password.encode("utf-8")).hexdigest()

    def _verify_password(self, password: str, hashed_password: str) -> bool:
        """验证密码"""
        return self._hash_password(password) == hashed_password

    async def create_user(
        self,
        username: str,
        email: str,
        password: str,
        full_name: str | None = None,
    ) -> UserRecord:
        """创建用户"""
        # 检查用户名是否已存在
        existing_user = await self.repo.get_by_username(username)
        if existing_user:
            raise AgentException.message("用户名已存在")

        # 检查邮箱是否已存在
        existing_email = await self.repo.get_by_email(email)
        if existing_email:
            raise AgentException.message("邮箱已被注册")

        hashed_password = self._hash_password(password)
        user = await self.repo.create(
            username=username,
            email=email,
            password=hashed_password,
            full_name=full_name,
        )
        return user

    async def get_user(self, user_id: int) -> UserRecord:
        """获取用户详情"""
        user = await self.repo.get_by_id(user_id)
        if not user or user.is_deleted:
            raise AgentException.message("用户不存在")
        return user

    async def list_users(self, limit: int = 100, offset: int = 0) -> list[UserRecord]:
        """获取用户列表"""
        return await self.repo.list_all(limit=limit, offset=offset)

    async def update_user(
        self,
        user_id: int,
        full_name: str | None = None,
        avatar_url: str | None = None,
        phone: str | None = None,
        preferences: dict | None = None,
    ) -> UserRecord:
        """更新用户信息"""
        user = await self.get_user(user_id)

        if full_name is not None:
            user.full_name = full_name
        if avatar_url is not None:
            user.avatar_url = avatar_url
        if phone is not None:
            user.phone = phone
        if preferences is not None:
            user.preferences = preferences

        await self.repo.update(user)
        return user

    async def change_password(
        self, user_id: int, old_password: str, new_password: str
    ) -> UserRecord:
        """修改密码"""
        user = await self.get_user(user_id)

        if not self._verify_password(old_password, user.password):
            raise AgentException.message("旧密码错误")

        user.password = self._hash_password(new_password)
        await self.repo.update(user)
        return user

    async def delete_user(self, user_id: int) -> None:
        """删除用户（软删除）"""
        user = await self.get_user(user_id)
        await self.repo.delete(user)

    async def authenticate(self, username: str, password: str) -> UserRecord | None:
        """认证用户"""
        user = await self.repo.get_by_username(username)
        if not user or user.is_deleted:
            return None

        if user.suspended:
            raise AgentException.message("用户已被禁用")

        if not self._verify_password(password, user.password):
            return None

        return user
