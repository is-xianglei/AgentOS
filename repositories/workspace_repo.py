from datetime import datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import joinedload

from models.user import UserRecord
from models.workspace import WorkspaceMemberRecord, WorkspaceRecord


class WorkspaceRepository:
    def __init__(self, db: AsyncSession):
        self.db = db

    async def create(
        self,
        name: str,
        slug: str,
        display_name: str,
        workspace_type: str = "team",
        logo_url: str | None = None,
        industry: str | None = None,
        company_size: str | None = None,
        billing_email: str | None = None,
        plan: str = "free",
        quotas: dict | None = None,
        settings: dict | None = None,
    ) -> WorkspaceRecord:
        workspace = WorkspaceRecord(
            name=name,
            slug=slug,
            display_name=display_name,
            workspace_type=workspace_type,
            logo_url=logo_url,
            industry=industry,
            company_size=company_size,
            billing_email=billing_email,
            plan=plan,
            quotas=quotas or {},
            settings=settings or {},
            suspended=False,
        )
        self.db.add(workspace)
        await self.db.flush()
        await self.db.refresh(workspace)
        return workspace

    async def get_by_id(self, workspace_id: int) -> WorkspaceRecord | None:
        return await self.db.get(WorkspaceRecord, workspace_id)

    async def get_by_name(self, name: str) -> WorkspaceRecord | None:
        stmt = select(WorkspaceRecord).where(WorkspaceRecord.name == name)
        return (await self.db.scalars(stmt)).first()

    async def get_by_slug(self, slug: str) -> WorkspaceRecord | None:
        stmt = select(WorkspaceRecord).where(WorkspaceRecord.slug == slug)
        return (await self.db.scalars(stmt)).first()

    async def list_all(
        self, limit: int = 100, offset: int = 0
    ) -> list[WorkspaceRecord]:
        stmt = (
            select(WorkspaceRecord)
            .where(WorkspaceRecord.is_deleted.is_(False))
            .order_by(WorkspaceRecord.id.desc())
            .limit(limit)
            .offset(offset)
        )
        return list(await self.db.scalars(stmt))

    async def update(self, workspace: WorkspaceRecord) -> WorkspaceRecord:
        await self.db.flush()
        await self.db.refresh(workspace)
        return workspace

    async def delete(self, workspace: WorkspaceRecord) -> None:
        """软删除工作区"""
        workspace.is_deleted = True
        workspace.deleted_at = datetime.utcnow()
        await self.db.flush()


class WorkspaceMemberRepository:
    def __init__(self, db: AsyncSession):
        self.db = db

    async def create(
        self,
        workspace_id: int,
        user_id: int,
        invited_by: int | None = None,
        invited_at: datetime | None = None,
        joined_at: datetime | None = None,
    ) -> WorkspaceMemberRecord:
        member = WorkspaceMemberRecord(
            workspace_id=workspace_id,
            user_id=user_id,
            invited_by=invited_by,
            invited_at=invited_at,
            joined_at=joined_at,
        )
        self.db.add(member)
        await self.db.flush()
        await self.db.refresh(member)
        return member

    async def get_by_id(self, member_id: int) -> WorkspaceMemberRecord | None:
        return await self.db.get(WorkspaceMemberRecord, member_id)

    async def get_by_workspace_and_user(
        self, workspace_id: int, user_id: int
    ) -> WorkspaceMemberRecord | None:
        stmt = select(WorkspaceMemberRecord).where(
            WorkspaceMemberRecord.workspace_id == workspace_id,
            WorkspaceMemberRecord.user_id == user_id,
            WorkspaceMemberRecord.is_deleted.is_(False),
        )
        return (await self.db.scalars(stmt)).first()

    async def list_by_workspace(
        self, workspace_id: int
    ) -> list[WorkspaceMemberRecord]:
        stmt = (
            select(WorkspaceMemberRecord)
            .where(
                WorkspaceMemberRecord.workspace_id == workspace_id,
                WorkspaceMemberRecord.is_deleted.is_(False),
            )
            .order_by(WorkspaceMemberRecord.id)
        )
        return list(await self.db.scalars(stmt))

    async def list_by_workspace_with_user(
        self, workspace_id: int
    ) -> list[tuple[WorkspaceMemberRecord, UserRecord]]:
        """查询工作区成员列表，并关联用户信息"""
        stmt = (
            select(WorkspaceMemberRecord, UserRecord)
            .join(UserRecord, WorkspaceMemberRecord.user_id == UserRecord.id)
            .where(
                WorkspaceMemberRecord.workspace_id == workspace_id,
                WorkspaceMemberRecord.is_deleted.is_(False),
            )
            .order_by(WorkspaceMemberRecord.id)
        )
        result = await self.db.execute(stmt)
        return list(result.all())

    async def list_by_user(self, user_id: int) -> list[WorkspaceMemberRecord]:
        stmt = (
            select(WorkspaceMemberRecord)
            .options(joinedload(WorkspaceMemberRecord.workspace))
            .where(
                WorkspaceMemberRecord.user_id == user_id,
                WorkspaceMemberRecord.is_deleted.is_(False),
            )
            .order_by(WorkspaceMemberRecord.id)
        )
        return list(await self.db.scalars(stmt))

    async def update(self, member: WorkspaceMemberRecord) -> WorkspaceMemberRecord:
        await self.db.flush()
        await self.db.refresh(member)
        return member

    async def delete(self, member: WorkspaceMemberRecord) -> None:
        """软删除成员"""
        member.is_deleted = True
        member.deleted_at = datetime.utcnow()
        await self.db.flush()
