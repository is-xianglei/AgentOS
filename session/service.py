from copy import deepcopy
from typing import Any
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from core.errors import AgentException
from llm.types import ToolResultMessage
from session.models import SessionMessage, SessionRecord, SessionSnapshot, SessionTurnRecord
from session.repository import SessionRepository
from workspace.service import WorkspaceService


class SessionService:
    def __init__(self, db: AsyncSession):
        """初始化会话服务依赖。"""
        self.db = db
        self.repo = SessionRepository(db)

    async def create(
        self,
        title: str | None = None,
        model_name: str | None = None,
        system_prompt: str | None = None,
        metadata: dict[str, Any] | None = None,
        user_id: int | None = None,
        workspace_id: int | None = None,
        agent_id: int | None = None,
    ) -> SessionRecord:
        """创建会话。提交交给请求边界（get_db）统一处理。"""
        session: SessionRecord = await self.repo.create(
            title=title or "新会话",
            model_name=model_name,
            system_prompt=system_prompt,
            metadata=metadata or {},
            user_id=user_id,
            workspace_id=workspace_id,
            agent_id=agent_id,
        )
        return session

    async def create_from_first_message(
        self,
        content: str,
        user_id: int | None = None,
        workspace_id: int | None = None,
        agent_id: int | None = None,
    ) -> SessionRecord:
        """根据首条用户消息创建会话。"""
        title = self._summarize_title(content)
        return await self.repo.create(
            title=title,
            model_name=None,
            system_prompt=None,
            metadata={},
            user_id=user_id,
            workspace_id=workspace_id,
            agent_id=agent_id,
        )

    async def list(self) -> list[SessionRecord]:
        """查询全部会话列表。"""
        return await self.repo.list()

    async def list_by_user(self, user_id: int) -> list[SessionRecord]:
        """查询指定用户的会话列表。"""
        return await self.repo.list_by_user(user_id)

    async def list_by_user_and_workspace(
        self,
        user_id: int,
        workspace_id: int,
    ) -> list[SessionRecord]:
        """查询当前工作区内的用户会话。"""
        return await self.repo.list_by_user_and_workspace(user_id, workspace_id)

    async def list_accessible(
        self,
        user_id: int,
        workspace_id: int,
    ) -> list[SessionRecord]:
        """查询当前工作区内由所有权、可见性或共享范围授予读取权的会话。"""
        membership = await WorkspaceService(self.db).require_active_membership(
            workspace_id,
            user_id,
        )

        from department.service import DepartmentService
        from group.service import GroupService

        department_scope_ids = await DepartmentService(self.db).get_department_scope_ids(
            workspace_id,
            membership.department_id,
        )
        groups = await GroupService(self.db).list_groups_for_active_member(
            workspace_id,
            user_id,
        )
        return await self.repo.list_accessible(
            user_id,
            workspace_id,
            department_scope_ids=department_scope_ids,
            group_ids=[group.id for group in groups],
        )

    async def check_access(
        self,
        session: SessionRecord,
        user_id: int,
        workspace_id: int,
    ) -> bool:
        """检查用户是否有权读取某个会话。

        访问规则：
        1. 会话的创建者
        2. 会话在 shared_with 列表中的用户
        3. 会话可见性为 workspace 且用户是该工作区成员
        4. 会话可见性为 public
        5. 用户直属部门或任一有效祖先在部门共享范围内
        6. 用户是任一有效共享群组的正式成员
        无可信用户或工作区归属的历史会话默认拒绝，避免跨租户自动认领。
        """
        if session.workspace_id != workspace_id or session.user_id is None:
            return False

        try:
            membership = await WorkspaceService(self.db).require_active_membership(
                workspace_id,
                user_id,
            )
        except AgentException:
            return False

        if session.user_id == user_id:
            return True
        if user_id in session.shared_with:
            return True
        if session.visibility == "public":
            return True

        if session.visibility == "workspace":
            return True

        if session.shared_with_departments:
            from department.service import DepartmentService

            if await DepartmentService(self.db).is_department_in_shared_departments(
                workspace_id,
                membership.department_id,
                session.shared_with_departments,
            ):
                return True

        if session.shared_with_groups:
            from group.service import GroupService

            if await GroupService(self.db).is_user_in_groups(
                workspace_id,
                user_id,
                session.shared_with_groups,
            ):
                return True

        return False

    async def check_write_access(
        self,
        session: SessionRecord,
        user_id: int,
        workspace_id: int,
    ) -> bool:
        """共享仅授予读取权；写入、删除和恢复运行只允许会话创建者。"""
        if session.workspace_id != workspace_id or session.user_id != user_id:
            return False
        return await WorkspaceService(self.db).is_active_member(workspace_id, user_id)

    async def require_read_access(
        self,
        session_id: int,
        user_id: int,
        workspace_id: int,
    ) -> SessionRecord:
        """返回当前用户可读的会话，否则拒绝访问。"""
        session = await self.get_required(session_id)
        if not await self.check_access(session, user_id, workspace_id):
            raise AgentException.message("无权限访问该会话", status_code=403)
        return session

    async def require_write_access(
        self,
        session_id: int,
        user_id: int,
        workspace_id: int,
    ) -> SessionRecord:
        """返回当前用户拥有的会话，共享读取者不能修改。"""
        session = await self.get_required(session_id)
        if not await self.check_write_access(session, user_id, workspace_id):
            raise AgentException.message("只有会话创建者可以执行该操作", status_code=403)
        return session

    async def share(
        self,
        session_id: int,
        actor_user_id: int,
        workspace_id: int,
        *,
        visibility: str,
        user_ids: list[int],
        department_ids: list[int],
        group_ids: list[int],
    ) -> SessionRecord:
        """校验并覆盖会话共享范围，部门范围在读取时动态包含子部门。"""
        session = await self.require_write_access(session_id, actor_user_id, workspace_id)
        if visibility not in {"private", "workspace", "public"}:
            raise AgentException.message("会话可见性无效")

        users = [item for item in dict.fromkeys(user_ids) if item != actor_user_id]
        departments = list(dict.fromkeys(department_ids))
        groups = list(dict.fromkeys(group_ids))
        await WorkspaceService(self.db).require_active_user_ids(workspace_id, users)

        from department.service import DepartmentService
        from group.service import GroupService

        await DepartmentService(self.db).require_department_ids(
            workspace_id,
            departments,
        )
        await GroupService(self.db).require_active_group_ids(workspace_id, groups)
        return await self.repo.update_sharing(
            session,
            visibility=visibility,
            user_ids=users,
            department_ids=departments,
            group_ids=groups,
        )

    async def get_required(self, session_id: int) -> SessionRecord:
        """查询会话，不存在时抛出业务错误。"""
        session: SessionRecord = await self.repo.get(session_id)
        if session is None:
            raise AgentException.message("会话不存在")
        return session

    async def archive(
        self,
        session_id: int,
        actor_user_id: int,
        workspace_id: int,
    ) -> SessionRecord:
        """归档指定会话。提交交给请求边界统一处理。"""
        session = await self.require_write_access(session_id, actor_user_id, workspace_id)
        await self.repo.update_status(session, "archived")
        return session

    async def rename(
        self,
        session_id: int,
        title: str,
        actor_user_id: int,
        workspace_id: int,
    ) -> SessionRecord:
        """重命名会话标题。提交交给请求边界统一处理。"""
        session = await self.require_write_access(session_id, actor_user_id, workspace_id)
        session.title = title
        await self.db.flush()
        await self.db.refresh(session)
        return session

    async def delete(
        self,
        session_id: int,
        actor_user_id: int,
        workspace_id: int,
    ) -> None:
        """软删除单个会话(级联标记消息/快照/任务等子表)。提交交给请求边界统一处理。"""
        session = await self.require_write_access(session_id, actor_user_id, workspace_id)
        await self.repo.delete(session)

    async def delete_many(
        self,
        ids: list[int],
        actor_user_id: int,
        workspace_id: int,
    ) -> int:
        """批量软删除会话,返回实际标记数量。级联同上。提交交给请求边界统一处理。"""
        normalized_ids = list(dict.fromkeys(ids))
        for session_id in normalized_ids:
            await self.require_write_access(session_id, actor_user_id, workspace_id)
        deleted: int = await self.repo.delete_by_ids(normalized_ids)
        return deleted

    async def ensure_runnable(self, session_id: int) -> SessionRecord:
        """校验会话当前是否允许继续执行。"""
        session = await self.lock_required(session_id)
        if session.status not in {"idle", "created", "failed"}:
            raise AgentException.message("会话当前不可执行")
        return session

    async def lock_required(self, session_id: int) -> SessionRecord:
        """锁定并刷新会话，供跨事务状态机按统一顺序获取行锁。"""
        session = await self.repo.get_for_update(session_id)
        if session is None:
            raise AgentException.message("会话不存在")
        return session

    async def ensure_interaction_resumable(self, session_id: int) -> SessionRecord:
        """锁定并校验等待人工交互的会话。"""
        session = await self.lock_required(session_id)
        if session.status != "awaiting_interaction":
            raise AgentException.message("会话当前没有待处理的人工交互")
        return session

    async def mark_running(self, session_id: int) -> SessionRecord:
        """将可执行会话标记为运行中。"""
        session: SessionRecord = await self.ensure_runnable(session_id)
        await self.repo.update_status(session, "running")
        return session

    async def mark_finished(self, session: SessionRecord, status: str) -> SessionRecord:
        """会话执行结束(idle/failed)统一切状态并提交。"""
        await self.repo.update_status(session, status)
        await self.db.commit()
        return session

    async def prepare_for_message(
        self,
        session_id: int | None,
        content: str,
        user_id: int,
        workspace_id: int,
        agent_id: int | None = None,
    ) -> SessionRecord:
        """获取或创建本次消息所属会话，并标记为运行中。"""
        if session_id is None:
            if agent_id is not None:
                from agent.service import AgentService

                await AgentService(self.db).require_runnable(agent_id, workspace_id)
            session: SessionRecord = await self.create_from_first_message(
                content,
                user_id,
                workspace_id,
                agent_id,
            )
            await self.repo.update_status(session, "running")
            return session
        session = await self.get_required(session_id)
        if agent_id is not None and agent_id != session.agent_id:
            raise AgentException.message("已有会话不能切换Agent", status_code=409)
        if session.agent_id is not None:
            from agent.service import AgentService

            await AgentService(self.db).require_runnable(session.agent_id, workspace_id)
        return await self.mark_running(session_id)

    async def start_turn(
        self,
        session: SessionRecord,
        user_id: int,
        workspace_id: int,
        content: str,
    ) -> tuple[SessionTurnRecord, SessionMessage]:
        """原样保存用户输入并创建 Turn，两阶段写入解决消息与 Turn 的双向引用。"""
        if session.user_id != user_id or session.workspace_id != workspace_id:
            raise AgentException.message("会话不属于当前用户或工作区")

        message = await self.add_message(session.id, "user", content)
        turn = await self.repo.create_turn(
            session_id=session.id,
            user_id=user_id,
            workspace_id=workspace_id,
            started_message_id=message.id,
        )
        await self.repo.bind_message_to_turn(message, turn.id)
        return turn, message

    async def get_turn_required(self, turn_id: UUID) -> SessionTurnRecord:
        """查询交互轮次，不存在时抛出业务错误。"""
        turn = await self.repo.get_turn(turn_id)
        if turn is None:
            raise AgentException.message("交互轮次不存在")
        return turn

    async def mark_turn_status(
        self,
        turn: SessionTurnRecord,
        status: str,
        completed_message_id: int | None = None,
    ) -> SessionTurnRecord:
        """更新 Turn 状态，成功完成时必须提供最终助手消息。"""
        if status == "completed" and completed_message_id is None:
            raise AgentException.message("完成交互轮次时缺少最终助手消息")
        return await self.repo.update_turn_status(turn, status, completed_message_id)

    async def add_message(
        self,
        session_id: int,
        role: str,
        content: Any,
        turn_id: UUID | None = None,
    ) -> SessionMessage:
        """保存会话消息并估算 token 数。"""
        token_estimate = len(str(content)) // 4
        return await self.repo.add_message(
            session_id,
            role,
            content,
            token_estimate,
            turn_id=turn_id,
        )

    async def list_messages(self, session_id: int) -> list[SessionMessage]:
        """查询指定会话的消息历史。"""
        await self.get_required(session_id)
        return await self.repo.list_messages(session_id)

    async def load_context(self, session_id: int) -> list[dict[str, Any]]:
        """按“有效快照 + 水位后消息”加载可恢复的会话上下文。"""
        await self.get_required(session_id)
        snapshot = await self.repo.latest_snapshot(session_id)
        context: list[dict[str, Any]] = []
        if snapshot is not None:
            context.extend(deepcopy(snapshot.messages))
        messages = await self.repo.list_messages_range(
            session_id,
            after_message_id=snapshot.through_message_id if snapshot else None,
        )
        context.extend(self._messages_to_context(messages))
        return context

    async def load_context_for_turn(
        self,
        turn: SessionTurnRecord,
        additional_context: str | None = None,
        rendered_memories: str | None = None,
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]], int | None]:
        """分别加载可压缩历史和必须原样保留的当前 Turn 上下文。"""
        snapshot: SessionSnapshot | None = await self.repo.latest_snapshot(turn.session_id)
        history: list[dict[str, Any]] = deepcopy(snapshot.messages) if snapshot else []
        history_messages = await self.repo.list_messages_range(
            turn.session_id,
            after_message_id=snapshot.through_message_id if snapshot else None,
            before_message_id=turn.started_message_id,
        )
        history.extend(self._messages_to_context(history_messages))

        current_messages = await self.repo.list_messages_range(
            turn.session_id,
            turn_id=turn.id,
        )
        current = self._messages_to_context(
            current_messages,
            additional_context_message_id=turn.started_message_id,
            additional_context=additional_context,
            rendered_memories_message_id=turn.started_message_id,
            rendered_memories=rendered_memories,
        )
        through_message_id = (
            history_messages[-1].id
            if history_messages
            else snapshot.through_message_id
            if snapshot
            else None
        )
        return history, current, through_message_id

    def _messages_to_context(
        self,
        messages: list[SessionMessage],
        *,
        additional_context_message_id: int | None = None,
        additional_context: str | None = None,
        rendered_memories_message_id: int | None = None,
        rendered_memories: str | None = None,
    ) -> list[dict[str, Any]]:
        """把持久化消息转换为模型上下文，可对指定消息创建临时请求副本。"""
        context: list[dict[str, Any]] = []
        for message in messages:
            if message.role in {"user", "assistant"}:
                content = deepcopy(message.content)
                if (
                    message.id == rendered_memories_message_id
                    and rendered_memories
                    and isinstance(content, str)
                ):
                    content = f"{rendered_memories}\n\n{content}"
                if (
                    message.id == additional_context_message_id
                    and additional_context
                    and isinstance(content, str)
                ):
                    content = f"{content}\n\n{additional_context}"
                context.append({"role": message.role, "content": content})
            elif message.role == "tool":
                tool_result = ToolResultMessage.from_content_dict(message.content)
                block: dict[str, Any] = {
                    "type": "tool_result",
                    "tool_use_id": tool_result.tool_use_id,
                    "content": tool_result.output,
                }
                if tool_result.is_error:
                    block["is_error"] = True
                previous_content = context[-1].get("content") if context else None
                if (
                    context
                    and context[-1].get("role") == "user"
                    and isinstance(previous_content, list)
                    and previous_content
                    and previous_content[0].get("type") == "tool_result"
                ):
                    context[-1]["content"].append(block)
                else:
                    context.append({"role": "user", "content": [block]})
        return context

    def _summarize_title(self, content: str) -> str:
        """从首条消息生成一个简短会话标题。"""
        normalized = " ".join(content.strip().split())
        if not normalized:
            return "新会话"
        if len(normalized) <= 24:
            return normalized
        return f"{normalized[:24].rstrip()}..."
