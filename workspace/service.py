from datetime import UTC, datetime

from sqlalchemy.ext.asyncio import AsyncSession

from core.errors import AgentException
from workspace.models import WorkspaceMemberRecord, WorkspaceRecord
from repositories.user_repo import UserRepository
from workspace.repository import WorkspaceMemberRepository, WorkspaceRepository


class WorkspaceService:
    def __init__(self, db: AsyncSession):
        self.db = db
        self.repo = WorkspaceRepository(db)
        self.member_repo = WorkspaceMemberRepository(db)
        self.user_repo = UserRepository(db)

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
        """创建工作区"""
        # 检查名称唯一性
        existing_name = await self.repo.get_by_name(name)
        if existing_name:
            raise AgentException.message("工作区名称已存在")

        # 检查 slug 唯一性
        existing_slug = await self.repo.get_by_slug(slug)
        if existing_slug:
            raise AgentException.message("工作区标识已存在")

        # 创建工作区
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

        # 将创建者加入工作区（作为 owner）
        await self.member_repo.create(
            workspace_id=workspace.id,
            user_id=creator_user_id,
            joined_at=datetime.now(UTC),
        )

        # 提交交给请求边界（get_db）统一处理，保证与调用方（如注册建 user）同事务原子提交
        return workspace

    async def get_workspace(self, workspace_id: int) -> WorkspaceRecord:
        """获取工作区详情"""
        workspace = await self.repo.get_by_id(workspace_id)
        if not workspace or workspace.is_deleted:
            raise AgentException.message("工作区不存在")
        return workspace

    async def list_workspaces(self, limit: int = 100, offset: int = 0) -> list[WorkspaceRecord]:
        """获取工作区列表"""
        return await self.repo.list_all(limit=limit, offset=offset)

    async def list_user_workspaces(self, user_id: int) -> list[WorkspaceRecord]:
        """获取用户加入的工作区列表"""
        memberships = await self.member_repo.list_by_user(user_id)
        return [m.workspace for m in memberships if not m.workspace.is_deleted]

    async def update_workspace(
        self,
        workspace_id: int,
        display_name: str | None = None,
        logo_url: str | None = None,
        industry: str | None = None,
        company_size: str | None = None,
        billing_email: str | None = None,
        settings: dict | None = None,
    ) -> WorkspaceRecord:
        """更新工作区信息"""
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

        await self.repo.update(workspace)
        return workspace

    async def delete_workspace(self, workspace_id: int) -> None:
        """删除工作区（软删除）"""
        workspace = await self.get_workspace(workspace_id)
        await self.repo.delete(workspace)

    async def is_member(self, workspace_id: int, user_id: int) -> bool:
        """检查用户是否是工作区成员"""
        member = await self.member_repo.get_by_workspace_and_user(workspace_id, user_id)
        return member is not None

    async def is_active_member(self, workspace_id: int, user_id: int) -> bool:
        """检查工作区可用且用户已经正式加入。"""
        workspace = await self.repo.get_by_id(workspace_id)
        if workspace is None or workspace.is_deleted or workspace.suspended:
            return False
        member = await self.member_repo.get_by_workspace_and_user(workspace_id, user_id)
        return member is not None and member.joined_at is not None

    async def require_active_member(self, workspace_id: int, user_id: int) -> WorkspaceRecord:
        """校验可信工作区作用域，不满足时拒绝请求。"""
        workspace = await self.repo.get_by_id(workspace_id)
        if workspace is None or workspace.is_deleted:
            raise AgentException.message("当前工作区不存在")
        if workspace.suspended:
            raise AgentException.message("当前工作区已被停用")
        member = await self.member_repo.get_by_workspace_and_user(workspace_id, user_id)
        if member is None or member.joined_at is None:
            raise AgentException.message("当前用户不是该工作区的有效成员")
        return workspace

    # ===== 成员管理 =====

    async def invite_member(
        self, workspace_id: int, inviter_user_id: int, email: str
    ) -> WorkspaceMemberRecord:
        """邀请成员加入工作区"""
        # 验证工作区存在
        await self.get_workspace(workspace_id)

        # 验证邀请人是工作区成员
        if not await self.is_member(workspace_id, inviter_user_id):
            raise AgentException.message("无权限邀请成员")

        # 查找被邀请用户
        invited_user = await self.user_repo.get_by_email(email)
        if not invited_user:
            raise AgentException.message("被邀请的用户不存在")

        if invited_user.is_deleted:
            raise AgentException.message("被邀请的用户已被删除")

        # 检查是否已经是成员
        existing_member = await self.member_repo.get_by_workspace_and_user(
            workspace_id, invited_user.id
        )
        if existing_member:
            raise AgentException.message("用户已经是工作区成员")

        # 创建成员记录（待接受邀请状态）
        member = await self.member_repo.create(
            workspace_id=workspace_id,
            user_id=invited_user.id,
            invited_by=inviter_user_id,
            invited_at=datetime.now(UTC),
            joined_at=None,  # 待接受邀请
        )

        # TODO: 发送邀请邮件
        return member

    async def accept_invite(self, workspace_id: int, user_id: int) -> WorkspaceMemberRecord:
        """接受工作区邀请"""
        member = await self.member_repo.get_by_workspace_and_user(workspace_id, user_id)
        if not member:
            raise AgentException.message("邀请记录不存在")

        if member.joined_at is not None:
            raise AgentException.message("已经加入工作区")

        # 更新加入时间
        member.joined_at = datetime.now(UTC)
        await self.member_repo.update(member)
        return member

    async def list_members(self, workspace_id: int) -> list[tuple[WorkspaceMemberRecord, dict]]:
        """获取工作区成员列表（包含用户信息）"""
        # 验证工作区存在
        await self.get_workspace(workspace_id)

        # 查询成员和用户信息
        members_with_users = await self.member_repo.list_by_workspace_with_user(workspace_id)

        # 转换为返回格式
        result = []
        for member, user in members_with_users:
            result.append(
                (
                    member,
                    {
                        "user_id": user.id,
                        "username": user.username,
                        "email": user.email,
                        "full_name": user.full_name,
                        "avatar_url": user.avatar_url,
                    },
                )
            )
        return result

    async def remove_member(self, workspace_id: int, user_id: int, operator_user_id: int) -> None:
        """移除工作区成员"""
        # 验证工作区存在
        await self.get_workspace(workspace_id)

        # 验证操作者是工作区成员
        if not await self.is_member(workspace_id, operator_user_id):
            raise AgentException.message("无权限移除成员")

        # 获取被移除的成员
        member = await self.member_repo.get_by_workspace_and_user(workspace_id, user_id)
        if not member:
            raise AgentException.message("成员不存在")

        # 软删除成员
        await self.member_repo.delete(member)

    async def get_member(self, workspace_id: int, user_id: int) -> WorkspaceMemberRecord:
        """获取成员信息"""
        member = await self.member_repo.get_by_workspace_and_user(workspace_id, user_id)
        if not member:
            raise AgentException.message("成员不存在")
        return member
