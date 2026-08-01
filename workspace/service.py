from datetime import UTC, datetime
from typing import Literal

from sqlalchemy.ext.asyncio import AsyncSession

from core.errors import AgentException
from user.service import UserService
from workspace.models import WorkspaceMemberRecord, WorkspaceRecord
from workspace.repository import WorkspaceMemberRepository, WorkspaceRepository

WorkspaceRole = Literal["owner", "admin", "member"]


class WorkspaceService:
    """维护工作区及其成员，是工作区角色与成员归属的唯一业务入口。"""

    def __init__(self, db: AsyncSession):
        self.db = db
        self.repo = WorkspaceRepository(db)
        self.member_repo = WorkspaceMemberRepository(db)
        self.user_service = UserService(db)

    async def create_workspace(
        self,
        creator_user_id: int,
        name: str,
        slug: str,
        display_name: str,
        workspace_type: str = "team",
        logo_url: str | None = None,
        industry: str | None = None,
        company_size: str | None = None,
        billing_email: str | None = None,
    ) -> WorkspaceRecord:
        """创建工作区，并在同一事务中把创建者登记为 owner。"""
        if await self.repo.get_by_name(name):
            raise AgentException.message("工作区名称已存在")
        if await self.repo.get_by_slug(slug):
            raise AgentException.message("工作区标识已存在")

        workspace = await self.repo.create(
            name=name,
            slug=slug,
            display_name=display_name,
            workspace_type=workspace_type,
            logo_url=logo_url,
            industry=industry,
            company_size=company_size,
            billing_email=billing_email,
        )
        await self.member_repo.create(
            workspace_id=workspace.id,
            user_id=creator_user_id,
            joined_at=datetime.now(UTC),
            role="owner",
        )
        return workspace

    async def get_workspace(self, workspace_id: int) -> WorkspaceRecord:
        workspace = await self.repo.get_by_id(workspace_id)
        if workspace is None or workspace.is_deleted:
            raise AgentException.message("工作区不存在")
        return workspace

    async def list_workspaces(
        self,
        limit: int = 100,
        offset: int = 0,
    ) -> list[WorkspaceRecord]:
        return await self.repo.list_all(limit=limit, offset=offset)

    async def list_user_workspaces(self, user_id: int) -> list[WorkspaceRecord]:
        """仅返回已经正式加入且仍可用的工作区。"""
        memberships = await self.member_repo.list_by_user(user_id)
        return [
            member.workspace
            for member in memberships
            if not member.workspace.is_deleted and not member.workspace.suspended
        ]

    async def update_workspace(
        self,
        workspace_id: int,
        actor_user_id: int,
        display_name: str | None = None,
        logo_url: str | None = None,
        industry: str | None = None,
        company_size: str | None = None,
        billing_email: str | None = None,
        settings: dict | None = None,
    ) -> WorkspaceRecord:
        await self.require_workspace_role(workspace_id, actor_user_id, {"owner", "admin"})
        workspace = await self.get_workspace(workspace_id)
        if display_name is not None:
            workspace.display_name = display_name
        if logo_url is not None:
            workspace.logo_url = logo_url
        if industry is not None:
            workspace.industry = industry
        if company_size is not None:
            workspace.company_size = company_size
        if billing_email is not None:
            workspace.billing_email = billing_email
        if settings is not None:
            workspace.settings = settings
        return await self.repo.update(workspace)

    async def delete_workspace(self, workspace_id: int, actor_user_id: int) -> None:
        await self.require_workspace_role(workspace_id, actor_user_id, {"owner"})
        await self.repo.delete(await self.get_workspace(workspace_id))

    async def get_membership(
        self,
        workspace_id: int,
        user_id: int,
    ) -> WorkspaceMemberRecord | None:
        return await self.member_repo.get_by_workspace_and_user(workspace_id, user_id)

    async def require_active_membership(
        self,
        workspace_id: int,
        user_id: int,
    ) -> WorkspaceMemberRecord:
        workspace = await self.repo.get_by_id(workspace_id)
        if workspace is None or workspace.is_deleted:
            raise AgentException.message("当前工作区不存在")
        if workspace.suspended:
            raise AgentException.message("当前工作区已被停用")
        member = await self.member_repo.get_by_workspace_and_user(workspace_id, user_id)
        if member is None or member.joined_at is None:
            raise AgentException.message("当前用户不是该工作区的有效成员")
        return member

    async def require_active_member(
        self,
        workspace_id: int,
        user_id: int,
    ) -> WorkspaceRecord:
        """兼容认证域的既有契约，校验后返回工作区。"""
        await self.require_active_membership(workspace_id, user_id)
        return await self.get_workspace(workspace_id)

    async def require_workspace_role(
        self,
        workspace_id: int,
        user_id: int,
        allowed_roles: set[str],
    ) -> WorkspaceMemberRecord:
        member = await self.require_active_membership(workspace_id, user_id)
        if member.role not in allowed_roles:
            raise AgentException.message("无权执行该工作区操作", status_code=403)
        return member

    async def is_member(self, workspace_id: int, user_id: int) -> bool:
        member = await self.member_repo.get_by_workspace_and_user(workspace_id, user_id)
        return member is not None and member.joined_at is not None

    async def is_active_member(self, workspace_id: int, user_id: int) -> bool:
        try:
            await self.require_active_membership(workspace_id, user_id)
        except AgentException:
            return False
        return True

    async def require_active_user_ids(
        self,
        workspace_id: int,
        user_ids: list[int],
    ) -> list[WorkspaceMemberRecord]:
        """校验一组用户都是当前工作区的有效成员，并返回去重后的成员记录。"""
        unique_ids = list(dict.fromkeys(user_ids))
        members = await self.member_repo.list_by_user_ids(workspace_id, unique_ids)
        found_ids = {member.user_id for member in members}
        missing_ids = [user_id for user_id in unique_ids if user_id not in found_ids]
        if missing_ids:
            raise AgentException.message(
                "共享或组织目标包含非工作区成员",
                {"user_ids": missing_ids},
            )
        return members

    async def invite_member(
        self,
        workspace_id: int,
        inviter_user_id: int,
        email: str,
        role: WorkspaceRole = "member",
    ) -> WorkspaceMemberRecord:
        inviter = await self.require_workspace_role(
            workspace_id,
            inviter_user_id,
            {"owner", "admin"},
        )
        if role == "owner":
            raise AgentException.message("不能通过邀请创建工作区 owner")
        if role == "admin" and inviter.role != "owner":
            raise AgentException.message("只有工作区 owner 可以邀请管理员", status_code=403)

        invited_user = await self.user_service.get_user_by_email(email)
        if invited_user is None:
            raise AgentException.message("被邀请的用户不存在")
        existing = await self.member_repo.get_by_workspace_and_user(
            workspace_id,
            invited_user.id,
        )
        if existing is not None:
            raise AgentException.message("用户已经有工作区成员或邀请记录")
        return await self.member_repo.create(
            workspace_id=workspace_id,
            user_id=invited_user.id,
            invited_by=inviter_user_id,
            invited_at=datetime.now(UTC),
            role=role,
        )

    async def accept_invite(
        self,
        workspace_id: int,
        user_id: int,
    ) -> WorkspaceMemberRecord:
        member = await self.member_repo.get_by_workspace_and_user(workspace_id, user_id)
        if member is None or member.invited_at is None:
            raise AgentException.message("邀请记录不存在")
        if member.joined_at is not None:
            raise AgentException.message("已经加入工作区")
        member.joined_at = datetime.now(UTC)
        return await self.member_repo.update(member)

    async def list_members(
        self,
        workspace_id: int,
        actor_user_id: int,
    ) -> list[tuple[WorkspaceMemberRecord, dict[str, object]]]:
        await self.require_active_membership(workspace_id, actor_user_id)
        members = await self.member_repo.list_by_workspace(workspace_id)
        return await self._attach_users(members)

    async def list_members_by_department_ids(
        self,
        workspace_id: int,
        department_ids: list[int],
    ) -> list[tuple[WorkspaceMemberRecord, dict[str, object]]]:
        """供 DepartmentService 在完成权限校验后读取部门成员。"""
        members = await self.member_repo.list_by_department_ids(workspace_id, department_ids)
        return await self._attach_users(members)

    async def _attach_users(
        self,
        members: list[WorkspaceMemberRecord],
    ) -> list[tuple[WorkspaceMemberRecord, dict[str, object]]]:
        users = await self.user_service.list_users_by_ids([member.user_id for member in members])
        by_id = {user.id: user for user in users}
        result: list[tuple[WorkspaceMemberRecord, dict[str, object]]] = []
        for member in members:
            user = by_id.get(member.user_id)
            if user is None:
                continue
            result.append(
                (
                    member,
                    {
                        "username": user.username,
                        "email": user.email,
                        "full_name": user.full_name,
                        "avatar_url": user.avatar_url,
                    },
                )
            )
        return result

    async def remove_member(
        self,
        workspace_id: int,
        user_id: int,
        operator_user_id: int,
    ) -> None:
        operator = await self.require_workspace_role(
            workspace_id,
            operator_user_id,
            {"owner", "admin"},
        )
        member = await self.get_member(workspace_id, user_id)
        if member.role == "owner":
            raise AgentException.message("不能移除工作区 owner")
        if member.role == "admin" and operator.role != "owner":
            raise AgentException.message("只有工作区 owner 可以移除管理员", status_code=403)
        # 群组成员关系属于 group 域，由该域负责同步软删除。
        from group.service import GroupService

        await GroupService(self.db).remove_user_from_workspace_groups(workspace_id, user_id)
        await self.member_repo.delete(member)

    async def get_member(self, workspace_id: int, user_id: int) -> WorkspaceMemberRecord:
        member = await self.member_repo.get_by_workspace_and_user(workspace_id, user_id)
        if member is None:
            raise AgentException.message("成员不存在")
        return member

    async def set_member_department(
        self,
        workspace_id: int,
        user_id: int,
        department_id: int | None,
        job_title: str | None,
    ) -> WorkspaceMemberRecord:
        """由 DepartmentService 校验组织规则后写入本域的成员记录。"""
        member = await self.get_member(workspace_id, user_id)
        if member.joined_at is None:
            raise AgentException.message("待接受邀请的用户不能分配部门")
        member.department_id = department_id
        member.job_title = job_title
        return await self.member_repo.update(member)

    async def has_members_in_department_ids(
        self,
        workspace_id: int,
        department_ids: list[int],
    ) -> bool:
        members = await self.member_repo.list_by_department_ids(workspace_id, department_ids)
        return bool(members)

    async def update_member_role(
        self,
        workspace_id: int,
        user_id: int,
        role: Literal["admin", "member"],
        operator_user_id: int,
    ) -> WorkspaceMemberRecord:
        await self.require_workspace_role(workspace_id, operator_user_id, {"owner"})
        member = await self.get_member(workspace_id, user_id)
        if member.role == "owner":
            raise AgentException.message("不能通过成员角色接口变更 owner")
        member.role = role
        return await self.member_repo.update(member)
