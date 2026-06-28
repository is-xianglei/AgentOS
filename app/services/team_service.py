from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import ConflictError, NotFoundError, ValidationAppError
from app.core.team_task_isolation import (
    current_team_task_instance_id,
    outgoing_team_task_consumer_instance_id,
    team_task_debug_isolation_enabled,
)
from app.repositories.subagent_run_repo import SubAgentRunRepository
from app.repositories.team_repo import TeamRepository
from app.services.session_service import SessionService


class TeamService:
    def __init__(self, db: AsyncSession):
        """初始化团队服务依赖。"""
        self.repo = TeamRepository(db)
        self.run_repo = SubAgentRunRepository(db)
        self.session_service = SessionService(db)

    async def list_members(self, session_id: int):
        """查询指定会话的团队成员。"""
        await self.session_service.get_required(session_id)
        return await self.repo.list_members(session_id)

    async def create_member(self, session_id: int, name: str, role: str):
        """创建团队成员。"""
        await self.session_service.get_required(session_id)
        self._validate_member_input(name, role)
        if await self.repo.get_member(session_id, name):
            raise ConflictError("TEAM_MEMBER_EXISTS", "团队成员已存在")
        member = await self.repo.create_member(session_id, name, role)
        return member

    async def get_or_create_member(self, session_id: int, name: str, role: str):
        """获取已有成员或创建新成员。"""
        await self.session_service.get_required(session_id)
        self._validate_member_input(name, role)
        member = await self.repo.get_member(session_id, name)
        if member is not None:
            return member
        member = await self.repo.create_member(session_id, name, role)
        return member

    async def update_member_status(self, session_id: int, name: str, status: str):
        """更新团队成员状态。"""
        await self.session_service.get_required(session_id)
        if status not in {"idle", "running", "failed"}:
            raise ValidationAppError("TEAM_MEMBER_STATUS_INVALID", "成员状态无效")
        member = await self.repo.get_member(session_id, name)
        if member is None:
            raise NotFoundError("TEAM_MEMBER_NOT_FOUND", "团队成员不存在")
        updated = await self.repo.update_member_status(member, status)
        return updated

    async def list_messages(self, session_id: int):
        """查询指定会话的团队消息。"""
        await self.session_service.get_required(session_id)
        return await self.repo.list_messages(session_id)

    async def send_message(
        self,
        session_id: int,
        sender: str,
        recipient: str,
        content: str,
        message_type: str = "direct",
        consumer_instance_id: str | None = None,
    ):
        """发送团队消息。"""
        await self.session_service.get_required(session_id)
        if not content.strip():
            raise ValidationAppError("TEAM_MESSAGE_EMPTY", "消息内容不能为空")
        if sender != "lead" and await self.repo.get_member(session_id, sender) is None:
            raise NotFoundError("TEAM_SENDER_NOT_FOUND", "发送方不存在")
        if recipient != "lead" and await self.repo.get_member(session_id, recipient) is None:
            raise NotFoundError("TEAM_RECIPIENT_NOT_FOUND", "接收方不存在")
        if consumer_instance_id is None:
            consumer_instance_id = outgoing_team_task_consumer_instance_id(message_type)
        message = await self.repo.send_message(
            session_id,
            sender,
            recipient,
            content,
            message_type,
            consumer_instance_id=consumer_instance_id,
        )
        return message

    async def read_inbox(
        self,
        session_id: int,
        recipient: str,
        consumer_instance_id: str | None = None,
    ):
        """读取并标记收件箱消息为已读。"""
        await self.session_service.get_required(session_id)
        await self._ensure_recipient(session_id, recipient)
        if consumer_instance_id is None and team_task_debug_isolation_enabled():
            consumer_instance_id = current_team_task_instance_id()
        messages = await self.repo.unread_messages(session_id, recipient, consumer_instance_id)
        await self.repo.mark_read(messages)
        return messages

    async def peek_inbox(
        self,
        session_id: int,
        recipient: str,
        consumer_instance_id: str | None = None,
    ):
        """只查看收件箱未读消息不标记已读。"""
        await self.session_service.get_required(session_id)
        await self._ensure_recipient(session_id, recipient)
        if consumer_instance_id is None and team_task_debug_isolation_enabled():
            consumer_instance_id = current_team_task_instance_id()
        return await self.repo.unread_messages(session_id, recipient, consumer_instance_id)

    async def mark_messages_read(self, messages):
        """批量标记团队消息为已读。"""
        await self.repo.mark_read(messages)

    async def list_subagent_runs(self, session_id: int):
        """查询指定会话的子代理运行记录。"""
        await self.session_service.get_required(session_id)
        return await self.run_repo.list_by_session(session_id)

    # ----- 团队(会话 1:1) -----

    async def get_or_create_team(
        self,
        session_id: int,
        team_name: str = "default",
        creator_type: str = "agent",
        creator_id: str | None = None,
    ):
        """获取已有团队或新建团队,委托 repo 完成。"""
        await self.session_service.get_required(session_id)
        return await self.repo.get_or_create_team(
            session_id,
            team_name=team_name,
            creator_type=creator_type,
            creator_id=creator_id,
        )

    async def spawn_member(
        self,
        session_id: int,
        name: str,
        role: str,
        prompt: str,
        agent_type: str = "general_purpose",
        creator_type: str = "agent",
        creator_id: str | None = None,
    ):
        """确保团队存在并落地一个 teammate 成员:写入 team_id/agent_type/prompt,置 working。

        复用 get_or_create_member 保证幂等;运行时字段走 update_member_runtime,
        与旧 update_member_status 的 {idle/running/failed} 校验互不干扰。
        """
        await self.session_service.get_required(session_id)
        team = await self.repo.get_or_create_team(
            session_id,
            creator_type=creator_type,
            creator_id=creator_id,
        )
        member = await self.get_or_create_member(session_id, name, role)
        member = await self.repo.update_member_runtime(
            member,
            status="working",
            agent_type=agent_type,
            prompt=prompt,
            team_id=team.id,
        )
        return member

    async def set_member_runtime_status(self, session_id: int, name: str, status: str):
        """更新 teammate 的 s11 运行状态(working/idle/shutdown)。

        独立于旧 update_member_status:旧方法服务于子代理运行记录语义
        ({idle/running/failed}),这里走 update_member_runtime,两套状态机共存。
        """
        await self.session_service.get_required(session_id)
        if status not in {"working", "idle", "shutdown"}:
            raise ValidationAppError("TEAM_MEMBER_RUNTIME_STATUS_INVALID", "成员运行状态无效")
        member = await self.repo.get_member(session_id, name)
        if member is None:
            raise NotFoundError("TEAM_MEMBER_NOT_FOUND", "团队成员不存在")
        return await self.repo.update_member_runtime(member, status=status)

    async def broadcast(self, session_id: int, sender: str, content: str):
        """向本会话除 sender 外的所有成员逐个广播消息,返回送达数量。"""
        await self.session_service.get_required(session_id)
        if not content.strip():
            raise ValidationAppError("TEAM_MESSAGE_EMPTY", "消息内容不能为空")
        members = await self.repo.list_members(session_id)
        delivered = 0
        for member in members:
            if member.name == sender:
                continue
            await self.send_message(
                session_id,
                sender,
                member.name,
                content,
                message_type="broadcast",
            )
            delivered += 1
        return delivered

    # ----- 任务认领(委托 repo,多实例安全) -----

    async def list_claimable_tasks(self, session_id: int):
        """查本会话可认领任务,委托 repo。"""
        await self.session_service.get_required(session_id)
        return await self.repo.list_claimable_tasks(session_id)

    async def claim_task(self, session_id: int, task_id: int, owner: str):
        """以条件 UPDATE 认领任务,委托 repo,返回 (是否成功, 原因)。"""
        await self.session_service.get_required(session_id)
        return await self.repo.claim_task(session_id, task_id, owner)

    def _validate_member_input(self, name: str, role: str):
        """校验团队成员名称和角色。"""
        if not name.strip():
            raise ValidationAppError("TEAM_MEMBER_NAME_REQUIRED", "成员名称不能为空")
        if not role.strip():
            raise ValidationAppError("TEAM_MEMBER_ROLE_REQUIRED", "成员角色不能为空")

    async def _ensure_recipient(self, session_id: int, recipient: str):
        """校验团队消息接收方存在。"""
        if recipient != "lead" and await self.repo.get_member(session_id, recipient) is None:
            raise NotFoundError("TEAM_RECIPIENT_NOT_FOUND", "接收方不存在")
