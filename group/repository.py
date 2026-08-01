from datetime import UTC, datetime

from sqlalchemy import and_, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from group.models import GroupMemberRecord, GroupRecord


class GroupRepository:
    def __init__(self, db: AsyncSession):
        self.db = db

    async def create(self, **values: object) -> GroupRecord:
        group = GroupRecord(**values)
        self.db.add(group)
        await self.db.flush()
        await self.db.refresh(group)
        return group

    async def get(self, workspace_id: int, group_id: int) -> GroupRecord | None:
        stmt = select(GroupRecord).where(
            GroupRecord.id == group_id,
            GroupRecord.workspace_id == workspace_id,
        )
        return (await self.db.scalars(stmt)).first()

    async def get_for_update(self, workspace_id: int, group_id: int) -> GroupRecord | None:
        """锁定群组并刷新状态，串行化负责人转让。"""
        stmt = (
            select(GroupRecord)
            .where(
                GroupRecord.id == group_id,
                GroupRecord.workspace_id == workspace_id,
            )
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        return (await self.db.scalars(stmt)).first()

    async def get_by_slug(self, workspace_id: int, slug: str) -> GroupRecord | None:
        stmt = select(GroupRecord).where(
            GroupRecord.workspace_id == workspace_id,
            GroupRecord.slug == slug,
        )
        return (await self.db.scalars(stmt)).first()

    async def list_visible(
        self,
        workspace_id: int,
        user_id: int,
        *,
        include_private: bool,
        group_type: str | None = None,
        search: str | None = None,
    ) -> list[GroupRecord]:
        stmt = select(GroupRecord)
        if not include_private:
            stmt = stmt.outerjoin(
                GroupMemberRecord,
                and_(
                    GroupMemberRecord.group_id == GroupRecord.id,
                    GroupMemberRecord.user_id == user_id,
                    GroupMemberRecord.joined_at.is_not(None),
                    GroupMemberRecord.is_deleted.is_(False),
                ),
            ).where(
                or_(
                    GroupRecord.visibility.in_(("workspace", "public")),
                    GroupMemberRecord.id.is_not(None),
                )
            )
        stmt = stmt.where(
            GroupRecord.workspace_id == workspace_id,
            GroupRecord.is_active.is_(True),
        )
        if group_type is not None:
            stmt = stmt.where(GroupRecord.group_type == group_type)
        if search:
            stmt = stmt.where(GroupRecord.name.ilike(f"%{search}%"))
        stmt = stmt.distinct().order_by(GroupRecord.name, GroupRecord.id)
        return list(await self.db.scalars(stmt))

    async def update(self, group: GroupRecord) -> GroupRecord:
        await self.db.flush()
        await self.db.refresh(group)
        return group

    async def soft_delete(self, group: GroupRecord) -> None:
        now = datetime.now(UTC)
        group.is_deleted = True
        group.deleted_at = now
        await self.db.execute(
            update(GroupMemberRecord)
            .where(
                GroupMemberRecord.group_id == group.id,
                GroupMemberRecord.is_deleted.is_(False),
            )
            .values(is_deleted=True, deleted_at=now)
        )
        await self.db.flush()


class GroupMemberRepository:
    def __init__(self, db: AsyncSession):
        self.db = db

    async def create_or_restore(
        self,
        *,
        group_id: int,
        user_id: int,
        role: str,
        invited_by: int | None,
        joined_at: datetime | None,
    ) -> GroupMemberRecord:
        existing = await self.get(group_id, user_id, include_deleted=True)
        if existing is not None:
            existing.role = role
            existing.invited_by = invited_by
            existing.joined_at = joined_at
            existing.is_deleted = False
            existing.deleted_at = None
            await self.db.flush()
            await self.db.refresh(existing)
            return existing
        member = GroupMemberRecord(
            group_id=group_id,
            user_id=user_id,
            role=role,
            invited_by=invited_by,
            joined_at=joined_at,
        )
        self.db.add(member)
        await self.db.flush()
        await self.db.refresh(member)
        return member

    async def get(
        self,
        group_id: int,
        user_id: int,
        *,
        include_deleted: bool = False,
    ) -> GroupMemberRecord | None:
        stmt = select(GroupMemberRecord).where(
            GroupMemberRecord.group_id == group_id,
            GroupMemberRecord.user_id == user_id,
        )
        if include_deleted:
            stmt = stmt.execution_options(include_deleted=True)
        return (await self.db.scalars(stmt)).first()

    async def list_by_group(
        self,
        group_id: int,
        *,
        include_pending: bool,
    ) -> list[GroupMemberRecord]:
        stmt = select(GroupMemberRecord).where(GroupMemberRecord.group_id == group_id)
        if not include_pending:
            stmt = stmt.where(GroupMemberRecord.joined_at.is_not(None))
        return list(await self.db.scalars(stmt.order_by(GroupMemberRecord.id)))

    async def list_groups_for_user(
        self,
        workspace_id: int,
        user_id: int,
    ) -> list[GroupRecord]:
        stmt = (
            select(GroupRecord)
            .join(GroupMemberRecord, GroupMemberRecord.group_id == GroupRecord.id)
            .where(
                GroupRecord.workspace_id == workspace_id,
                GroupRecord.is_active.is_(True),
                GroupMemberRecord.user_id == user_id,
                GroupMemberRecord.joined_at.is_not(None),
            )
            .order_by(GroupMemberRecord.joined_at.desc(), GroupRecord.id)
        )
        return list(await self.db.scalars(stmt))

    async def is_user_in_groups(
        self,
        workspace_id: int,
        user_id: int,
        group_ids: list[int],
    ) -> bool:
        if not group_ids:
            return False
        stmt = (
            select(GroupMemberRecord.id)
            .join(GroupRecord, GroupRecord.id == GroupMemberRecord.group_id)
            .where(
                GroupMemberRecord.group_id.in_(group_ids),
                GroupMemberRecord.user_id == user_id,
                GroupMemberRecord.joined_at.is_not(None),
                GroupRecord.workspace_id == workspace_id,
                GroupRecord.is_active.is_(True),
            )
            .limit(1)
        )
        return (await self.db.scalar(stmt)) is not None

    async def update(self, member: GroupMemberRecord) -> GroupMemberRecord:
        await self.db.flush()
        await self.db.refresh(member)
        return member

    async def soft_delete(self, member: GroupMemberRecord) -> None:
        member.is_deleted = True
        member.deleted_at = datetime.now(UTC)
        await self.db.flush()

    async def soft_delete_by_workspace_user(
        self,
        workspace_id: int,
        user_id: int,
    ) -> None:
        group_ids = select(GroupRecord.id).where(GroupRecord.workspace_id == workspace_id)
        await self.db.execute(
            update(GroupMemberRecord)
            .where(
                GroupMemberRecord.group_id.in_(group_ids),
                GroupMemberRecord.user_id == user_id,
                GroupMemberRecord.is_deleted.is_(False),
            )
            .values(is_deleted=True, deleted_at=datetime.now(UTC))
        )
        await self.db.flush()

    async def list_user_groups_for_update(
        self,
        workspace_id: int,
        user_id: int,
    ) -> list[GroupRecord]:
        """锁定用户参与的群组，串行化工作区移除与负责人转让。"""
        stmt = (
            select(GroupRecord)
            .outerjoin(
                GroupMemberRecord,
                and_(
                    GroupMemberRecord.group_id == GroupRecord.id,
                    GroupMemberRecord.user_id == user_id,
                ),
            )
            .where(
                GroupRecord.workspace_id == workspace_id,
                or_(
                    GroupRecord.owner_id == user_id,
                    GroupMemberRecord.id.is_not(None),
                ),
            )
            .order_by(GroupRecord.id)
            .with_for_update(of=GroupRecord)
        )
        return list(await self.db.scalars(stmt))
