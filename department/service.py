from dataclasses import dataclass
from typing import TypedDict

from sqlalchemy.ext.asyncio import AsyncSession

from core.errors import AgentException
from department.models import DepartmentRecord
from department.repository import DepartmentRepository
from workspace.models import WorkspaceMemberRecord
from workspace.service import WorkspaceService


class DepartmentUpdateFields(TypedDict, total=False):
    parent_id: int | None
    name: str
    code: str | None
    description: str | None
    manager_id: int | None
    sort_order: int
    is_active: bool


@dataclass(frozen=True)
class DepartmentTreeNode:
    department: DepartmentRecord
    children: tuple[DepartmentTreeNode, ...]


class DepartmentService:
    """部门业务规则、权限与跨域成员编排入口。"""

    def __init__(self, db: AsyncSession):
        self.db = db
        self.repo = DepartmentRepository(db)
        self.workspace_service = WorkspaceService(db)

    async def create_department(
        self,
        workspace_id: int,
        actor_user_id: int,
        name: str,
        parent_id: int | None = None,
        code: str | None = None,
        description: str | None = None,
        manager_id: int | None = None,
        sort_order: int = 0,
        is_active: bool = True,
    ) -> DepartmentRecord:
        await self.workspace_service.require_workspace_role(
            workspace_id,
            actor_user_id,
            {"owner", "admin"},
        )
        if parent_id is not None:
            parent = await self.get_required(workspace_id, parent_id)
            if not parent.is_active:
                raise AgentException.message("不能在已停用部门下创建子部门")
        await self._validate_manager(workspace_id, manager_id)
        return await self.repo.create(
            workspace_id=workspace_id,
            parent_id=parent_id,
            name=name,
            code=code,
            description=description,
            manager_id=manager_id,
            sort_order=sort_order,
            is_active=is_active,
        )

    async def get_required(
        self,
        workspace_id: int,
        department_id: int,
    ) -> DepartmentRecord:
        department = await self.repo.get_by_id(workspace_id, department_id)
        if department is None:
            raise AgentException.message("部门不存在", status_code=404)
        return department

    async def get_department(
        self,
        workspace_id: int,
        department_id: int,
        actor_user_id: int,
    ) -> DepartmentRecord:
        await self.workspace_service.require_active_membership(workspace_id, actor_user_id)
        return await self.get_required(workspace_id, department_id)

    async def list_departments(
        self,
        workspace_id: int,
        actor_user_id: int,
    ) -> list[DepartmentRecord]:
        await self.workspace_service.require_active_membership(workspace_id, actor_user_id)
        return await self.repo.list_by_workspace(workspace_id)

    async def get_department_tree(
        self,
        workspace_id: int,
        actor_user_id: int,
    ) -> tuple[DepartmentTreeNode, ...]:
        departments = await self.list_departments(workspace_id, actor_user_id)
        return self._build_tree(departments)

    async def update_department(
        self,
        workspace_id: int,
        department_id: int,
        actor_user_id: int,
        updates: DepartmentUpdateFields,
    ) -> DepartmentRecord:
        department = await self.get_required(workspace_id, department_id)
        await self._require_manager_or_roles(workspace_id, actor_user_id, department)

        if "parent_id" in updates:
            parent_id = updates["parent_id"]
            if parent_id == department.id:
                raise AgentException.message("部门不能以自身作为父部门")
            if parent_id is not None:
                parent = await self.get_required(workspace_id, parent_id)
                if not parent.is_active:
                    raise AgentException.message("不能移动到已停用部门下")
                descendants = await self.repo.list_descendants(workspace_id, department.id)
                if parent_id in {item.id for item in descendants}:
                    raise AgentException.message("不能将部门移动到其子孙部门下")
            department.parent_id = parent_id

        if "manager_id" in updates:
            manager_id = updates["manager_id"]
            await self._validate_manager(workspace_id, manager_id)
            department.manager_id = manager_id
        if "name" in updates:
            department.name = updates["name"]
        if "code" in updates:
            department.code = updates["code"]
        if "description" in updates:
            department.description = updates["description"]
        if "sort_order" in updates:
            department.sort_order = updates["sort_order"]
        if "is_active" in updates:
            department.is_active = updates["is_active"]
        return await self.repo.update(department)

    async def delete_department(
        self,
        workspace_id: int,
        department_id: int,
        actor_user_id: int,
    ) -> None:
        await self.workspace_service.require_workspace_role(
            workspace_id,
            actor_user_id,
            {"owner"},
        )
        department = await self.get_required(workspace_id, department_id)
        if await self.repo.has_children(workspace_id, department.id):
            raise AgentException.message("仅允许删除叶子部门", status_code=409)
        if await self.workspace_service.has_members_in_department_ids(
            workspace_id,
            [department.id],
        ):
            raise AgentException.message("部门仍有成员，不能删除", status_code=409)
        await self.repo.delete(department)

    async def list_ancestors(
        self,
        workspace_id: int,
        department_id: int,
        actor_user_id: int,
    ) -> list[DepartmentRecord]:
        await self.workspace_service.require_active_membership(workspace_id, actor_user_id)
        await self.get_required(workspace_id, department_id)
        return await self.repo.list_ancestors(workspace_id, department_id)

    async def list_descendants(
        self,
        workspace_id: int,
        department_id: int,
        actor_user_id: int,
    ) -> list[DepartmentRecord]:
        await self.workspace_service.require_active_membership(workspace_id, actor_user_id)
        await self.get_required(workspace_id, department_id)
        return await self.repo.list_descendants(workspace_id, department_id)

    async def list_members(
        self,
        workspace_id: int,
        department_id: int,
        actor_user_id: int,
        *,
        include_descendants: bool = False,
    ) -> list[tuple[WorkspaceMemberRecord, dict[str, object]]]:
        await self.workspace_service.require_active_membership(workspace_id, actor_user_id)
        department = await self.get_required(workspace_id, department_id)
        department_ids = [department.id]
        if include_descendants:
            descendants = await self.repo.list_descendants(workspace_id, department.id)
            department_ids.extend(item.id for item in descendants)
        return await self.workspace_service.list_members_by_department_ids(
            workspace_id,
            department_ids,
        )

    async def set_member_department(
        self,
        workspace_id: int,
        user_id: int,
        actor_user_id: int,
        department_id: int | None,
        job_title: str | None,
    ) -> WorkspaceMemberRecord:
        actor = await self.workspace_service.require_active_membership(
            workspace_id,
            actor_user_id,
        )
        member = await self.workspace_service.get_member(workspace_id, user_id)
        target: DepartmentRecord | None = None
        if department_id is not None:
            target = await self.get_required(workspace_id, department_id)
            if not target.is_active:
                raise AgentException.message("不能向已停用部门分配成员")

        if actor.role not in {"owner", "admin"}:
            managed_department_ids = {
                value
                for value in (member.department_id, target.id if target is not None else None)
                if value is not None
            }
            if not managed_department_ids:
                raise AgentException.message("无权管理该成员的部门归属", status_code=403)
            for managed_department_id in managed_department_ids:
                managed = await self.get_required(workspace_id, managed_department_id)
                if managed.manager_id != actor_user_id:
                    raise AgentException.message("无权跨负责人部门移动成员", status_code=403)

        return await self.workspace_service.set_member_department(
            workspace_id,
            user_id,
            department_id,
            job_title,
        )

    async def require_department_ids(
        self,
        workspace_id: int,
        department_ids: list[int],
    ) -> list[DepartmentRecord]:
        """供会话共享等跨域编排校验部门均属于工作区且处于启用状态。"""
        unique_ids = list(dict.fromkeys(department_ids))
        departments = await self.repo.list_by_ids(workspace_id, unique_ids)
        found_ids = {department.id for department in departments if department.is_active}
        invalid_ids = [
            department_id for department_id in unique_ids if department_id not in found_ids
        ]
        if invalid_ids:
            raise AgentException.message(
                "共享目标包含无效部门",
                {"department_ids": invalid_ids},
            )
        return departments

    async def get_member_department_scope_ids(
        self,
        workspace_id: int,
        user_id: int,
    ) -> list[int]:
        """返回成员直属部门及其有效祖先，供“共享给部门（含子部门）”判定。"""
        member = await self.workspace_service.require_active_membership(workspace_id, user_id)
        return await self.get_department_scope_ids(workspace_id, member.department_id)

    async def get_department_scope_ids(
        self,
        workspace_id: int,
        department_id: int | None,
    ) -> list[int]:
        """调用方已校验成员时，按直属部门解析自身及有效祖先。"""
        if department_id is None:
            return []
        department = await self.repo.get_by_id(workspace_id, department_id)
        if department is None or not department.is_active:
            return []
        ancestors = await self.repo.list_ancestors(workspace_id, department.id)
        return [department.id, *(item.id for item in ancestors if item.is_active)]

    async def is_department_in_shared_departments(
        self,
        workspace_id: int,
        department_id: int | None,
        shared_department_ids: list[int],
    ) -> bool:
        """调用方已校验成员时，判断直属部门或任一祖先是否在共享范围内。"""
        if not shared_department_ids:
            return False
        scope_ids = await self.get_department_scope_ids(workspace_id, department_id)
        return not set(scope_ids).isdisjoint(shared_department_ids)

    async def is_user_in_shared_departments(
        self,
        workspace_id: int,
        user_id: int,
        shared_department_ids: list[int],
    ) -> bool:
        if not shared_department_ids:
            return False
        member = await self.workspace_service.require_active_membership(workspace_id, user_id)
        return await self.is_department_in_shared_departments(
            workspace_id,
            member.department_id,
            shared_department_ids,
        )

    async def _validate_manager(self, workspace_id: int, manager_id: int | None) -> None:
        if manager_id is not None:
            await self.workspace_service.require_active_user_ids(workspace_id, [manager_id])

    async def _require_manager_or_roles(
        self,
        workspace_id: int,
        actor_user_id: int,
        department: DepartmentRecord,
    ) -> None:
        member = await self.workspace_service.require_active_membership(
            workspace_id,
            actor_user_id,
        )
        if member.role in {"owner", "admin"} or department.manager_id == actor_user_id:
            return
        raise AgentException.message("无权管理该部门", status_code=403)

    def _build_tree(
        self,
        departments: list[DepartmentRecord],
    ) -> tuple[DepartmentTreeNode, ...]:
        by_id = {department.id: department for department in departments}
        children: dict[int | None, list[DepartmentRecord]] = {}
        for department in departments:
            parent_id = department.parent_id if department.parent_id in by_id else None
            children.setdefault(parent_id, []).append(department)

        visited: set[int] = set()

        def build(department: DepartmentRecord, path: frozenset[int]) -> DepartmentTreeNode:
            if department.id in path:
                raise AgentException.message("部门层级存在循环，无法生成部门树")
            visited.add(department.id)
            next_path = path | {department.id}
            return DepartmentTreeNode(
                department=department,
                children=tuple(
                    build(child, next_path) for child in children.get(department.id, [])
                ),
            )

        roots = tuple(build(root, frozenset()) for root in children.get(None, []))
        if len(visited) != len(departments):
            raise AgentException.message("部门层级存在循环，无法生成部门树")
        return roots
