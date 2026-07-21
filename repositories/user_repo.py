from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from models.user import UserRecord


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
    ) -> UserRecord:
        user = UserRecord(
            username=username,
            email=email,
            password=password,
            full_name=full_name,
            phone=phone,
            email_verified=False,
            suspended=False,
            auth_provider=auth_provider,
            provider_user_id=provider_user_id,
            preferences=preferences or {},
        )
        self.db.add(user)
        await self.db.flush()
        await self.db.refresh(user)
        return user

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

    async def update(self, user: UserRecord) -> UserRecord:
        await self.db.flush()
        await self.db.refresh(user)
        return user

    async def delete(self, user: UserRecord) -> None:
        """软删除用户"""
        user.is_deleted = True
        await self.db.flush()
