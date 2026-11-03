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
            # 父部门被并发删除且未提交时仍能读到它，新部门会挂在已删除部门下；
            # 与 delete_department 共用部门树锁，锁内再校验父部门。顶级部门不加锁。
            await self.repo.lock_tree(workspace_id)
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

        # updates 只包含客户端显式提交的字段；parent_id 为 None 表示移到顶级。
        if "parent_id" in updates:
            parent_id = updates["parent_id"]
            if parent_id == department.id:
                raise AgentException.message("部门不能以自身作为父部门")
            if parent_id is not None:
                # 并发的反向移动各自通过子孙检查后会成环，按工作区加锁串行化，锁内再检查；
                # READ COMMITTED 下拿锁后的查询能看到先提交的移动。移到顶级不会成环，不加锁。
                await self.repo.lock_tree(workspace_id)
                parent = await self.get_required(workspace_id, parent_id)
                if not parent.is_active:
                    raise AgentException.message("不能移动到已停用部门下")
                # 移到自己的子孙部门下会让部门树形成环。
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
        """软删除部门，仅 owner 可操作；只能删除没有子部门且没有成员的叶子部门。"""
        await self.workspace_service.require_workspace_role(
            workspace_id,
            actor_user_id,
            {"owner"},
        )
        # 并发建子部门或移入部门未提交时，叶子检查看不到新子部门，删除后它会挂在已删除部门下；
        # 与 create_department、update_department 共用部门树锁串行化，锁内再加载和检查。
        await self.repo.lock_tree(workspace_id)
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
            # 目标部门被并发删除且未提交时仍能读到它，成员会挂在已删除部门下；与 delete_department
            # 共用部门树锁，锁内再校验。移出部门不加锁：不会产生不一致，只会让并发删除更保守地拒绝。
            await self.repo.lock_tree(workspace_id)
            target = await self.get_required(workspace_id, department_id)
            if not target.is_active:
                raise AgentException.message("不能向已停用部门分配成员")

        # owner/admin 可任意调整；部门负责人只能在自己负责的部门之间调动成员，
        # 成员原部门和目标部门（存在的那些）都必须由当前操作者负责。
        if actor.role not in {"owner", "admin"}:
            managed_department_ids: set[int] = set()
            if member.department_id is not None:
                managed_department_ids.add(member.department_id)
            if target is not None:
                managed_department_ids.add(target.id)
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
        # 去重但保留传入顺序，报错时 invalid_ids 的顺序与请求一致。
        unique_ids = list(dict.fromkeys(department_ids))
        departments = await self.repo.list_by_ids(workspace_id, unique_ids)
        active_ids = {department.id for department in departments if department.is_active}
        invalid_ids: list[int] = []
        for department_id in unique_ids:
            if department_id not in active_ids:
                invalid_ids.append(department_id)
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
        # 停用的祖先自身不计入范围，但不截断更上层的祖先。
        scope_ids = [department.id]
        for ancestor in ancestors:
            if ancestor.is_active:
                scope_ids.append(ancestor.id)
        return scope_ids

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
        """按 parent_id 把平铺列表组装成树，保留列表原有的排序。"""
        department_ids = {department.id for department in departments}
        # 父部门不在列表中（如已软删除）的部门挂到根节点（键为 None）下。
        children_by_parent: dict[int | None, list[DepartmentRecord]] = {}
        for department in departments:
            parent_id = department.parent_id
            if parent_id not in department_ids:
                parent_id = None
            children_by_parent.setdefault(parent_id, []).append(department)

        visited: set[int] = set()

        def build(department: DepartmentRecord, path: frozenset[int]) -> DepartmentTreeNode:
            # path 是从根到当前节点的部门 ID，重复出现说明存在环。
            if department.id in path:
                raise AgentException.message("部门层级存在循环，无法生成部门树")
            visited.add(department.id)
            next_path = path | {department.id}
            child_nodes: list[DepartmentTreeNode] = []
            for child in children_by_parent.get(department.id, []):
                child_nodes.append(build(child, next_path))
            return DepartmentTreeNode(department=department, children=tuple(child_nodes))

        root_nodes: list[DepartmentTreeNode] = []
        for root in children_by_parent.get(None, []):
            root_nodes.append(build(root, frozenset()))
        # 环上的部门互为父子，从根出发永远到达不了，用访问数量兜底检测。
        if len(visited) != len(departments):
            raise AgentException.message("部门层级存在循环，无法生成部门树")
        return tuple(root_nodes)
