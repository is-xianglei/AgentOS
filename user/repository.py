from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from user.models import UserRecord

# 首个用户判定的咨询锁，即 pg_advisory_xact_lock(int4, int4) 的两个键：
# 第一个键是命名空间（ASCII "USER"），第二个键固定为 1。
# 部门树锁用的是 "DEPT" 命名空间，两者互不阻塞；新增咨询锁要另选命名空间。
FIRST_USER_LOCK_NAMESPACE = 0x55534552
FIRST_USER_LOCK_ID = 1


class UserRepository:
    def __init__(self, db: AsyncSession):
        self.db = db

    async def create(
        self,
        username: str,
        email: str,
        password: str,
        full_name: str | None = None,
        phone: str | None = None,
        auth_provider: str = "local",
        provider_user_id: str | None = None,
        preferences: dict | None = None,
        is_platform_admin: bool = False,
    ) -> UserRecord:
        user = UserRecord(
            username=username,
            email=email,
            password=password,
            full_name=full_name,
            phone=phone,
            email_verified=False,
            suspended=False,
            is_platform_admin=is_platform_admin,
            auth_provider=auth_provider,
            provider_user_id=provider_user_id,
            preferences=preferences or {},
        )
        self.db.add(user)
        await self.db.flush()
        await self.db.refresh(user)
        return user

    async def lock_first_user_check(self) -> None:
        """加事务级咨询锁，锁在事务提交或回滚时自动释放。"""
        await self.db.execute(
            select(func.pg_advisory_xact_lock(FIRST_USER_LOCK_NAMESPACE, FIRST_USER_LOCK_ID))
        )

    async def has_any_user(self) -> bool:
        """users 表里是否已有任意一行，软删除的也算。"""
        stmt = select(UserRecord.id).limit(1).execution_options(include_deleted=True)
        return (await self.db.scalar(stmt)) is not None

    async def get_by_id(self, user_id: int) -> UserRecord | None:
        return await self.db.get(UserRecord, user_id)

    async def get_by_username(self, username: str) -> UserRecord | None:
        stmt = select(UserRecord).where(UserRecord.username == username)
        return (await self.db.scalars(stmt)).first()

    async def get_by_email(self, email: str) -> UserRecord | None:
        stmt = select(UserRecord).where(UserRecord.email == email)
        return (await self.db.scalars(stmt)).first()

    async def list_all(self, limit: int = 100, offset: int = 0) -> list[UserRecord]:
        stmt = (
            select(UserRecord)
            .where(UserRecord.is_deleted.is_(False))
            .order_by(UserRecord.id.desc())
            .limit(limit)
            .offset(offset)
        )
        return list(await self.db.scalars(stmt))

    async def list_by_ids(self, user_ids: list[int]) -> list[UserRecord]:
        """批量查询有效用户，供其他域通过 UserService 获取用户摘要。"""
        if not user_ids:
            return []
        stmt = select(UserRecord).where(UserRecord.id.in_(user_ids)).order_by(UserRecord.id)
        return list(await self.db.scalars(stmt))

    async def update(self, user: UserRecord) -> UserRecord:
        """持久化调用方已在实体上做的修改，并刷新 updated_at 等数据库生成的列。"""
        await self.db.flush()
        await self.db.refresh(user)
        return user

    async def delete(self, user: UserRecord) -> None:
        """软删除用户"""
        user.is_deleted = True
        await self.db.flush()
