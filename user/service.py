import hashlib

from sqlalchemy.ext.asyncio import AsyncSession

from core.errors import AgentException
from user.models import UserRecord
from user.repository import UserRepository


class _Unset:
    """参数哨兵：区分“未传”与“显式传 None”，便于 PATCH 语义把字段清空。"""


UNSET = _Unset()


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
        """管理员创建用户，用户名和邮箱都必须未被占用。"""
        existing_user = await self.repo.get_by_username(username)
        if existing_user:
            raise AgentException.message("用户名已存在")

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

    async def get_user_by_email(self, email: str) -> UserRecord | None:
        """按邮箱查询有效用户，供其他业务域通过 Service 调用。"""
        user = await self.repo.get_by_email(email)
        if user is None or user.is_deleted:
            return None
        return user

    async def list_users(self, limit: int = 100, offset: int = 0) -> list[UserRecord]:
        """获取用户列表"""
        return await self.repo.list_all(limit=limit, offset=offset)

    async def list_users_by_ids(self, user_ids: list[int]) -> list[UserRecord]:
        """批量获取有效用户，供跨域编排使用。"""
        return await self.repo.list_by_ids(user_ids)

    async def update_user(
        self,
        user_id: int,
        full_name: str | None | _Unset = UNSET,
        avatar_url: str | None | _Unset = UNSET,
        phone: str | None | _Unset = UNSET,
        preferences: dict | None | _Unset = UNSET,
    ) -> UserRecord:
        """更新用户资料：未传的字段保持不变，显式传 None 表示清空。"""
        user = await self.get_user(user_id)

        if not isinstance(full_name, _Unset):
            user.full_name = full_name
        if not isinstance(avatar_url, _Unset):
            user.avatar_url = avatar_url
        if not isinstance(phone, _Unset):
            user.phone = phone
        if not isinstance(preferences, _Unset):
            # preferences 列不允许为空，清空时存成空字典。
            user.preferences = preferences or {}

        await self.repo.update(user)
        return user

    async def change_password(
        self, user_id: int, old_password: str, new_password: str
    ) -> UserRecord:
        """修改密码，必须先校验旧密码；管理员代改他人密码时同样要求旧密码。"""
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
        """按用户名和密码认证：用户不存在或密码错误返回 None，已停用则直接报错。"""
        user = await self.repo.get_by_username(username)
        if not user or user.is_deleted:
            return None

        if user.suspended:
            raise AgentException.message("用户已被禁用")

        if not self._verify_password(password, user.password):
            return None

        return user
