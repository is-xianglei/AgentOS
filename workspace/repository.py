from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import joinedload

from workspace.models import WorkspaceMemberRecord, WorkspaceRecord


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

    async def list_all(self, limit: int = 100, offset: int = 0) -> list[WorkspaceRecord]:
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
        workspace.deleted_at = datetime.now(UTC)
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
        role: str = "member",
        department_id: int | None = None,
        job_title: str | None = None,
    ) -> WorkspaceMemberRecord:
        existing = await self.get_by_workspace_and_user(
            workspace_id,
            user_id,
            include_deleted=True,
        )
        if existing is not None:
            existing.invited_by = invited_by
            existing.invited_at = invited_at
            existing.joined_at = joined_at
            existing.role = role
            existing.department_id = department_id
            existing.job_title = job_title
            existing.is_deleted = False
            existing.deleted_at = None
            await self.db.flush()
            await self.db.refresh(existing)
            return existing

        member = WorkspaceMemberRecord(
            workspace_id=workspace_id,
            user_id=user_id,
            invited_by=invited_by,
            invited_at=invited_at,
            joined_at=joined_at,
            role=role,
            department_id=department_id,
            job_title=job_title,
        )
        self.db.add(member)
        await self.db.flush()
        await self.db.refresh(member)
        return member

    async def get_by_id(self, member_id: int) -> WorkspaceMemberRecord | None:
        return await self.db.get(WorkspaceMemberRecord, member_id)

    async def get_by_workspace_and_user(
        self,
        workspace_id: int,
        user_id: int,
        *,
        include_deleted: bool = False,
    ) -> WorkspaceMemberRecord | None:
        stmt = select(WorkspaceMemberRecord).where(
            WorkspaceMemberRecord.workspace_id == workspace_id,
            WorkspaceMemberRecord.user_id == user_id,
        )
        if include_deleted:
            stmt = stmt.execution_options(include_deleted=True)
        return (await self.db.scalars(stmt)).first()

    async def list_by_workspace(self, workspace_id: int) -> list[WorkspaceMemberRecord]:
        stmt = (
            select(WorkspaceMemberRecord)
            .where(
                WorkspaceMemberRecord.workspace_id == workspace_id,
                WorkspaceMemberRecord.is_deleted.is_(False),
            )
            .order_by(WorkspaceMemberRecord.id)
        )
        return list(await self.db.scalars(stmt))

    async def list_by_department_ids(
        self,
        workspace_id: int,
        department_ids: list[int],
    ) -> list[WorkspaceMemberRecord]:
        """查询指定工作区内属于目标部门的有效成员。"""
        if not department_ids:
            return []
        stmt = (
            select(WorkspaceMemberRecord)
            .where(
                WorkspaceMemberRecord.workspace_id == workspace_id,
                WorkspaceMemberRecord.department_id.in_(department_ids),
                WorkspaceMemberRecord.joined_at.is_not(None),
            )
            .order_by(WorkspaceMemberRecord.id)
        )
        return list(await self.db.scalars(stmt))

    async def list_by_user_ids(
        self,
        workspace_id: int,
        user_ids: list[int],
    ) -> list[WorkspaceMemberRecord]:
        """批量查询工作区内已经正式加入的成员。"""
        if not user_ids:
            return []
        stmt = select(WorkspaceMemberRecord).where(
            WorkspaceMemberRecord.workspace_id == workspace_id,
            WorkspaceMemberRecord.user_id.in_(user_ids),
            WorkspaceMemberRecord.joined_at.is_not(None),
        )
        return list(await self.db.scalars(stmt))

    async def list_by_user(self, user_id: int) -> list[WorkspaceMemberRecord]:
        stmt = (
            select(WorkspaceMemberRecord)
            .options(joinedload(WorkspaceMemberRecord.workspace))
            .where(
                WorkspaceMemberRecord.user_id == user_id,
                WorkspaceMemberRecord.joined_at.is_not(None),
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
        member.deleted_at = datetime.now(UTC)
        await self.db.flush()
