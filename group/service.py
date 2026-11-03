from datetime import UTC, datetime
from typing import Any, Literal

from sqlalchemy.ext.asyncio import AsyncSession

from core.errors import AgentException
from group.models import GroupMemberRecord, GroupRecord
from group.repository import GroupMemberRepository, GroupRepository
from user.service import UserService
from workspace.models import WorkspaceMemberRecord
from workspace.service import WorkspaceService


class GroupService:
    def __init__(self, db: AsyncSession):
        self.db = db
        self.repo = GroupRepository(db)
        self.member_repo = GroupMemberRepository(db)
        self.workspace_service = WorkspaceService(db)
        self.user_service = UserService(db)

    async def create_group(
        self,
        workspace_id: int,
        actor_user_id: int,
        **values: Any,
    ) -> GroupRecord:
        """创建群组，创建者同时成为负责人和第一个 owner 成员。"""
        await self.workspace_service.require_active_membership(workspace_id, actor_user_id)
        slug = str(values["slug"])
        if await self.repo.get_by_slug(workspace_id, slug):
            raise AgentException.message("群组标识已存在")
        group = await self.repo.create(
            workspace_id=workspace_id,
            created_by=actor_user_id,
            owner_id=actor_user_id,
            **values,
        )
        await self.member_repo.create_or_restore(
            group_id=group.id,
            user_id=actor_user_id,
            role="owner",
            invited_by=None,
            joined_at=datetime.now(UTC),
        )
        return group

    async def require_group(self, workspace_id: int, group_id: int) -> GroupRecord:
        group = await self.repo.get(workspace_id, group_id)
        if group is None:
            raise AgentException.message("群组不存在")
        return group

    async def require_active_group(self, workspace_id: int, group_id: int) -> GroupRecord:
        group = await self.require_group(workspace_id, group_id)
        if not group.is_active:
            raise AgentException.message("群组已停用")
        return group

    async def list_groups(
        self,
        workspace_id: int,
        actor_user_id: int,
        *,
        group_type: str | None = None,
        search: str | None = None,
    ) -> list[GroupRecord]:
        workspace_member = await self.workspace_service.require_active_membership(
            workspace_id,
            actor_user_id,
        )
        # 工作区 owner 能看到全部群组；其他成员看不到自己未加入的私有群组。
        return await self.repo.list_visible(
            workspace_id,
            actor_user_id,
            include_private=workspace_member.role == "owner",
            group_type=group_type,
            search=search,
        )

    async def get_group(
        self,
        workspace_id: int,
        group_id: int,
        actor_user_id: int,
    ) -> GroupRecord:
        """查看群组详情：私有群组仅对已加入的成员和工作区 owner 可见。"""
        workspace_member = await self.workspace_service.require_active_membership(
            workspace_id,
            actor_user_id,
        )
        group = await self.require_active_group(workspace_id, group_id)
        if group.visibility != "private":
            return group
        if workspace_member.role == "owner":
            return group
        # 待审批的申请（joined_at 为空）不算已加入。
        membership = await self.member_repo.get(group.id, actor_user_id)
        if membership is None or membership.joined_at is None:
            raise AgentException.message("无权查看该私有群组", status_code=403)
        return group

    async def update_group(
        self,
        workspace_id: int,
        group_id: int,
        actor_user_id: int,
        updates: dict[str, Any],
    ) -> GroupRecord:
        """更新群组信息，仅群组 owner/admin 或工作区 owner 可操作。"""
        if not updates:
            raise AgentException.message("至少需要提供一个待更新字段")
        group, _, _ = await self._require_group_manager(
            workspace_id,
            group_id,
            actor_user_id,
        )
        if "slug" in updates:
            slug = updates["slug"]
            if not isinstance(slug, str) or not slug:
                raise AgentException.message("群组标识不能为空")
            existing = await self.repo.get_by_slug(workspace_id, slug)
            if existing is not None and existing.id != group.id:
                raise AgentException.message("群组标识已存在")
        allowed = {
            "name",
            "slug",
            "description",
            "avatar_url",
            "group_type",
            "visibility",
            "join_mode",
            "settings",
            "is_active",
        }
        # 只有 description 和 avatar_url 允许显式置空。
        non_nullable = {
            "name",
            "slug",
            "group_type",
            "visibility",
            "join_mode",
            "settings",
            "is_active",
        }
        for key, value in updates.items():
            # 不在白名单内的字段直接忽略，不报错。
            if key not in allowed:
                continue
            if key in non_nullable and value is None:
                raise AgentException.message(f"{key} 不能设置为空")
            setattr(group, key, value)
        return await self.repo.update(group)

    async def delete_group(
        self,
        workspace_id: int,
        group_id: int,
        actor_user_id: int,
    ) -> None:
        """删除群组，仅群组 owner 或工作区 owner 可操作；已停用的群组也允许删除。"""
        group = await self.require_group(workspace_id, group_id)
        workspace_member = await self.workspace_service.require_active_membership(
            workspace_id,
            actor_user_id,
        )
        group_member = await self.member_repo.get(group.id, actor_user_id)
        is_group_owner = (
            group_member is not None
            and group_member.joined_at is not None
            and group_member.role == "owner"
        )
        if workspace_member.role != "owner" and not is_group_owner:
            raise AgentException.message("只有群组或工作区 owner 可以删除群组", status_code=403)
        await self.repo.soft_delete(group)

    async def invite_member(
        self,
        workspace_id: int,
        group_id: int,
        actor_user_id: int,
        target_user_id: int,
        role: Literal["admin", "member"] = "member",
    ) -> GroupMemberRecord:
        """管理员邀请工作区内的有效成员，被邀请人直接加入，无需审批。"""
        group, _, _ = await self._require_group_manager(
            workspace_id,
            group_id,
            actor_user_id,
        )
        await self.workspace_service.require_active_user_ids(workspace_id, [target_user_id])
        # 已加入或有待审批申请都算已存在，避免邀请覆盖掉申请记录。
        if await self.member_repo.get(group.id, target_user_id):
            raise AgentException.message("用户已经有群组成员或申请记录")
        return await self.member_repo.create_or_restore(
            group_id=group.id,
            user_id=target_user_id,
            role=role,
            invited_by=actor_user_id,
            joined_at=datetime.now(UTC),
        )

    async def join_group(
        self,
        workspace_id: int,
        group_id: int,
        actor_user_id: int,
    ) -> GroupMemberRecord:
        await self.workspace_service.require_active_membership(workspace_id, actor_user_id)
        group = await self.require_active_group(workspace_id, group_id)
        if group.visibility == "private":
            raise AgentException.message("私有群组只能通过邀请加入")
        if group.join_mode == "invite":
            raise AgentException.message("该群组仅支持邀请加入")
        if await self.member_repo.get(group.id, actor_user_id):
            raise AgentException.message("已经加入或申请过该群组")
        # open 模式立即加入；approval 模式先留空 joined_at，等待管理员审批。
        joined_at = datetime.now(UTC) if group.join_mode == "open" else None
        return await self.member_repo.create_or_restore(
            group_id=group.id,
            user_id=actor_user_id,
            role="member",
            invited_by=None,
            joined_at=joined_at,
        )

    async def approve_member(
        self,
        workspace_id: int,
        group_id: int,
        user_id: int,
        actor_user_id: int,
    ) -> GroupMemberRecord:
        group, _, _ = await self._require_group_manager(
            workspace_id,
            group_id,
            actor_user_id,
        )
        member = await self._require_group_member(group.id, user_id)
        if member.joined_at is not None:
            raise AgentException.message("该成员已经加入群组")
        member.joined_at = datetime.now(UTC)
        return await self.member_repo.update(member)

    async def reject_member(
        self,
        workspace_id: int,
        group_id: int,
        user_id: int,
        actor_user_id: int,
    ) -> None:
        group, _, _ = await self._require_group_manager(
            workspace_id,
            group_id,
            actor_user_id,
        )
        member = await self._require_group_member(group.id, user_id)
        if member.joined_at is not None:
            raise AgentException.message("只能拒绝待审批申请")
        await self.member_repo.soft_delete(member)

    async def remove_member(
        self,
        workspace_id: int,
        group_id: int,
        user_id: int,
        actor_user_id: int,
    ) -> None:
        """移除群组成员；actor 与 user 相同时表示本人退出群组。"""
        group = await self.require_active_group(workspace_id, group_id)
        target = await self._require_group_member(group.id, user_id)
        if target.joined_at is None:
            raise AgentException.message("待审批成员应使用拒绝操作")
        if target.role == "owner" or group.owner_id == user_id:
            raise AgentException.message("请先转让群组负责人")
        # 本人退出无需管理权限；移除他人时，群组 admin 只能移除普通成员。
        if actor_user_id != user_id:
            _, actor_group_member, workspace_member = await self._require_group_manager(
                workspace_id,
                group_id,
                actor_user_id,
            )
            if (
                target.role == "admin"
                and workspace_member.role != "owner"
                and actor_group_member is not None
                and actor_group_member.role != "owner"
            ):
                raise AgentException.message("群组管理员不能移除其他管理员", status_code=403)
        await self.member_repo.soft_delete(target)

    async def update_member_role(
        self,
        workspace_id: int,
        group_id: int,
        user_id: int,
        role: Literal["admin", "member"],
        actor_user_id: int,
    ) -> GroupMemberRecord:
        group = await self.require_active_group(workspace_id, group_id)
        await self._require_group_owner(workspace_id, group, actor_user_id)
        member = await self._require_group_member(group.id, user_id)
        if member.joined_at is None:
            raise AgentException.message("待审批成员不能设置角色")
        if member.role == "owner":
            raise AgentException.message("不能通过角色接口变更群组 owner")
        member.role = role
        return await self.member_repo.update(member)

    async def transfer_owner(
        self,
        workspace_id: int,
        group_id: int,
        new_owner_user_id: int,
        actor_user_id: int,
    ) -> GroupRecord:
        """转让群组负责人，原负责人降为 admin。"""
        # 先锁群组行并重读，串行化并发转让以及工作区移除成员时的负责人检查。
        group = await self.repo.get_for_update(workspace_id, group_id)
        if group is None:
            raise AgentException.message("群组不存在")
        if not group.is_active:
            raise AgentException.message("群组已停用")
        await self._require_group_owner(workspace_id, group, actor_user_id)
        new_owner = await self._require_group_member(group.id, new_owner_user_id)
        if new_owner.joined_at is None:
            raise AgentException.message("待审批成员不能成为群组负责人")
        current_owner = await self._require_group_member(group.id, group.owner_id)
        # 转给现任负责人本人时不做任何修改。
        if current_owner.id == new_owner.id:
            return group
        current_owner.role = "admin"
        new_owner.role = "owner"
        group.owner_id = new_owner.user_id
        await self.member_repo.update(current_owner)
        await self.member_repo.update(new_owner)
        return await self.repo.update(group)

    async def list_members(
        self,
        workspace_id: int,
        group_id: int,
        actor_user_id: int,
        *,
        include_pending: bool = False,
    ) -> list[tuple[GroupMemberRecord, dict[str, object]]]:
        """列出群组成员及其用户摘要；查看待审批申请需要管理权限。"""
        group = await self.get_group(workspace_id, group_id, actor_user_id)
        if include_pending:
            await self._require_group_manager(workspace_id, group.id, actor_user_id)
        members = await self.member_repo.list_by_group(
            group.id,
            include_pending=include_pending,
        )
        users = await self.user_service.list_users_by_ids([item.user_id for item in members])
        user_map = {user.id: user for user in users}
        result: list[tuple[GroupMemberRecord, dict[str, object]]] = []
        for member in members:
            # 用户已被软删除时查不到摘要，直接跳过该成员。
            user = user_map.get(member.user_id)
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

    async def list_user_groups(
        self,
        workspace_id: int,
        user_id: int,
    ) -> list[GroupRecord]:
        await self.workspace_service.require_active_membership(workspace_id, user_id)
        return await self.list_groups_for_active_member(workspace_id, user_id)

    async def list_groups_for_active_member(
        self,
        workspace_id: int,
        user_id: int,
    ) -> list[GroupRecord]:
        """调用方已校验工作区成员时，返回其当前有效群组。"""
        return await self.member_repo.list_groups_for_user(workspace_id, user_id)

    async def require_active_group_ids(
        self,
        workspace_id: int,
        group_ids: list[int],
    ) -> list[GroupRecord]:
        """校验共享目标群组均存在且启用，一次性报告全部无效的群组 ID。"""
        groups: list[GroupRecord] = []
        missing: list[int] = []
        for group_id in group_ids:
            group = await self.repo.get(workspace_id, group_id)
            if group is None or not group.is_active:
                missing.append(group_id)
            else:
                groups.append(group)
        if missing:
            raise AgentException.message("共享目标包含无效群组", {"group_ids": missing})
        return groups

    async def is_user_in_groups(
        self,
        workspace_id: int,
        user_id: int,
        group_ids: list[int],
    ) -> bool:
        return await self.member_repo.is_user_in_groups(workspace_id, user_id, group_ids)

    async def remove_user_from_workspace_groups(
        self,
        workspace_id: int,
        user_id: int,
    ) -> None:
        """工作区移除成员时同步清理群组关系，调用方已经完成成员权限校验。"""
        # 锁住相关群组后再检查负责人，避免与并发的负责人转让交错。
        user_groups = await self.member_repo.list_user_groups_for_update(workspace_id, user_id)
        owned = [group for group in user_groups if group.owner_id == user_id]
        if owned:
            raise AgentException.message(
                "该成员仍负责群组，请先转让负责人",
                {"group_ids": [group.id for group in owned]},
            )
        await self.member_repo.soft_delete_by_workspace_user(workspace_id, user_id)

    async def _require_group_member(
        self,
        group_id: int,
        user_id: int,
    ) -> GroupMemberRecord:
        member = await self.member_repo.get(group_id, user_id)
        if member is None:
            raise AgentException.message("群组成员或申请不存在")
        return member

    async def _require_group_manager(
        self,
        workspace_id: int,
        group_id: int,
        actor_user_id: int,
    ) -> tuple[GroupRecord, GroupMemberRecord | None, WorkspaceMemberRecord]:
        """要求 actor 是工作区 owner，或已加入群组的 owner/admin。

        工作区 owner 不必是群组成员，此时返回的群组成员记录可能为 None。
        """
        group = await self.require_active_group(workspace_id, group_id)
        workspace_member = await self.workspace_service.require_active_membership(
            workspace_id,
            actor_user_id,
        )
        group_member = await self.member_repo.get(group.id, actor_user_id)
        if workspace_member.role == "owner":
            return group, group_member, workspace_member
        if (
            group_member is None
            or group_member.joined_at is None
            or group_member.role not in {"owner", "admin"}
        ):
            raise AgentException.message("无权管理该群组", status_code=403)
        return group, group_member, workspace_member

    async def _require_group_owner(
        self,
        workspace_id: int,
        group: GroupRecord,
        actor_user_id: int,
    ) -> None:
        workspace_member = await self.workspace_service.require_active_membership(
            workspace_id,
            actor_user_id,
        )
        if workspace_member.role == "owner":
            return
        group_member = await self.member_repo.get(group.id, actor_user_id)
        if group_member is None or group_member.joined_at is None or group_member.role != "owner":
            raise AgentException.message("只有群组或工作区 owner 可以执行该操作", status_code=403)
