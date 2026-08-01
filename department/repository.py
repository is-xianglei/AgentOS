from datetime import UTC, datetime

from sqlalchemy import literal, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased

from department.models import DepartmentRecord


class DepartmentRepository:
    """部门持久化，仅操作 department 域拥有的表。"""

    def __init__(self, db: AsyncSession):
        self.db = db

    async def create(
        self,
        workspace_id: int,
        name: str,
        parent_id: int | None = None,
        code: str | None = None,
        description: str | None = None,
        manager_id: int | None = None,
        sort_order: int = 0,
        is_active: bool = True,
    ) -> DepartmentRecord:
        department = DepartmentRecord(
            workspace_id=workspace_id,
            parent_id=parent_id,
            name=name,
            code=code,
            description=description,
            manager_id=manager_id,
            sort_order=sort_order,
            is_active=is_active,
        )
        self.db.add(department)
        await self.db.flush()
        await self.db.refresh(department)
        return department

    async def get_by_id(
        self,
        workspace_id: int,
        department_id: int,
    ) -> DepartmentRecord | None:
        stmt = select(DepartmentRecord).where(
            DepartmentRecord.id == department_id,
            DepartmentRecord.workspace_id == workspace_id,
        )
        return (await self.db.scalars(stmt)).first()

    async def list_by_ids(
        self,
        workspace_id: int,
        department_ids: list[int],
    ) -> list[DepartmentRecord]:
        if not department_ids:
            return []
        stmt = (
            select(DepartmentRecord)
            .where(
                DepartmentRecord.workspace_id == workspace_id,
                DepartmentRecord.id.in_(department_ids),
            )
            .order_by(DepartmentRecord.sort_order, DepartmentRecord.name, DepartmentRecord.id)
        )
        return list(await self.db.scalars(stmt))

    async def list_by_workspace(self, workspace_id: int) -> list[DepartmentRecord]:
        stmt = (
            select(DepartmentRecord)
            .where(DepartmentRecord.workspace_id == workspace_id)
            .order_by(DepartmentRecord.sort_order, DepartmentRecord.name, DepartmentRecord.id)
        )
        return list(await self.db.scalars(stmt))

    async def list_ancestors(
        self,
        workspace_id: int,
        department_id: int,
    ) -> list[DepartmentRecord]:
        """按从父部门到根部门的顺序查询全部祖先。"""
        tree = (
            select(
                DepartmentRecord.id.label("id"),
                DepartmentRecord.parent_id.label("parent_id"),
                literal(0).label("depth"),
            )
            .where(
                DepartmentRecord.id == department_id,
                DepartmentRecord.workspace_id == workspace_id,
                DepartmentRecord.is_deleted.is_(False),
            )
            .cte("department_ancestors", recursive=True)
        )
        parent = aliased(DepartmentRecord)
        tree = tree.union_all(
            select(
                parent.id,
                parent.parent_id,
                (tree.c.depth + 1).label("depth"),
            )
            .join(tree, parent.id == tree.c.parent_id)
            .where(
                parent.workspace_id == workspace_id,
                parent.is_deleted.is_(False),
            )
        )
        stmt = (
            select(DepartmentRecord)
            .join(tree, DepartmentRecord.id == tree.c.id)
            .where(tree.c.depth > 0)
            .order_by(tree.c.depth)
        )
        return list(await self.db.scalars(stmt))

    async def list_descendants(
        self,
        workspace_id: int,
        department_id: int,
    ) -> list[DepartmentRecord]:
        """按层级、排序值和名称查询全部子孙部门，不包含起始部门。"""
        tree = (
            select(
                DepartmentRecord.id.label("id"),
                DepartmentRecord.parent_id.label("parent_id"),
                literal(0).label("depth"),
            )
            .where(
                DepartmentRecord.id == department_id,
                DepartmentRecord.workspace_id == workspace_id,
                DepartmentRecord.is_deleted.is_(False),
            )
            .cte("department_descendants", recursive=True)
        )
        child = aliased(DepartmentRecord)
        tree = tree.union_all(
            select(
                child.id,
                child.parent_id,
                (tree.c.depth + 1).label("depth"),
            )
            .join(tree, child.parent_id == tree.c.id)
            .where(
                child.workspace_id == workspace_id,
                child.is_deleted.is_(False),
            )
        )
        stmt = (
            select(DepartmentRecord)
            .join(tree, DepartmentRecord.id == tree.c.id)
            .where(tree.c.depth > 0)
            .order_by(
                tree.c.depth,
                DepartmentRecord.sort_order,
                DepartmentRecord.name,
                DepartmentRecord.id,
            )
        )
        return list(await self.db.scalars(stmt))

    async def has_children(self, workspace_id: int, department_id: int) -> bool:
        stmt = (
            select(DepartmentRecord.id)
            .where(
                DepartmentRecord.workspace_id == workspace_id,
                DepartmentRecord.parent_id == department_id,
            )
            .limit(1)
        )
        return (await self.db.scalar(stmt)) is not None

    async def update(self, department: DepartmentRecord) -> DepartmentRecord:
        await self.db.flush()
        await self.db.refresh(department)
        return department

    async def delete(self, department: DepartmentRecord) -> None:
        department.is_deleted = True
        department.deleted_at = datetime.now(UTC)
        await self.db.flush()
