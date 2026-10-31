from collections.abc import Awaitable, Callable
from copy import deepcopy
from dataclasses import dataclass
from typing import Any
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from core.errors import AgentException, LeaseLostError
from department.service import DepartmentService
from group.service import GroupService
from session.models import SessionMessage, SessionRecord, SessionSnapshot, SessionTurnRecord
from session.repository import SessionRepository
from workspace.service import WorkspaceService


@dataclass(frozen=True)
class TurnExecutionState:
    """轮次的业务状态与执行状态;executing 表示租约有效、有执行中的任务。"""

    turn_id: UUID
    session_id: int
    status: str
    executing: bool
    last_event_offset: int


@dataclass(frozen=True)
class TurnTermination:
    """清除租约后的收尾结果;offset 是为 run_end 分配的 offset。"""

    turn_id: UUID
    session_id: int
    status: str
    session_status: str
    offset: int


@dataclass(frozen=True)
class MessageDraft:
    """取消收尾时补写的一条中立形状消息(assistant 中立块数组或 tool 结果 dict)。"""

    role: str
    content: Any


# 取消方在锁内按需读取未落库内容:只有轮次仍是 running 时才调用。
type PartialLoader = Callable[[], Awaitable[list[MessageDraft]]]


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
        workflow_id: int | None = None,
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
            workflow_id=workflow_id,
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

        if session.shared_with_departments and await DepartmentService(
            self.db
        ).is_department_in_shared_departments(
            workspace_id,
            membership.department_id,
            session.shared_with_departments,
        ):
            return True

        if not session.shared_with_groups:
            return False
        return await GroupService(self.db).is_user_in_groups(
            workspace_id,
            user_id,
            session.shared_with_groups,
        )

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
            # 准入在推流前完成,冲突需以 HTTP 409 返回给客户端。
            raise AgentException.message(
                "会话当前不可执行",
                {"status": session.status},
                status_code=409,
            )
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
        workflow_id: int | None = None,
    ) -> SessionRecord:
        """获取或创建本次消息所属会话，并标记为运行中。

        workflow_id 与 agent_id 语义互斥；会话一旦锁定 Workflow，后续每轮都复检其可运行性，
        且不允许切换（与 Agent 的锁定规则同构）。不涉及 Workflow 的请求不进入任何新分支。
        """
        if agent_id is not None and workflow_id is not None:
            raise AgentException.message("同一次请求不能同时指定Agent和Workflow")
        if session_id is None:
            if agent_id is not None:
                # 局部导入：真实双向 Service 依赖，改顶层仍成环 agent.service -> tools.service
                # -> tools.registry -> tools.builtin.task -> task.service -> session.service，待依赖倒置任务处理。
                from agent.service import AgentService

                await AgentService(self.db).require_runnable(agent_id, workspace_id)
            if workflow_id is not None:
                # 局部导入：真实双向 Service 依赖，改顶层仍成环 workflow.service -> agent.service
                # -> ... -> task.service -> session.service，待依赖倒置任务处理。
                from workflow.service import WorkflowService

                await WorkflowService(self.db).require_runnable(workflow_id, workspace_id)
            session: SessionRecord = await self.create_from_first_message(
                content,
                user_id,
                workspace_id,
                agent_id,
                workflow_id,
            )
            await self.repo.update_status(session, "running")
            return session
        session = await self.get_required(session_id)
        if agent_id is not None and agent_id != session.agent_id:
            raise AgentException.message("已有会话不能切换Agent", status_code=409)
        if workflow_id is not None and workflow_id != session.workflow_id:
            raise AgentException.message("已有会话不能切换Workflow", status_code=409)
        if session.agent_id is not None:
            # 局部导入：真实双向 Service 依赖，改顶层仍成环 agent.service -> tools.service
            # -> tools.registry -> tools.builtin.task -> task.service -> session.service，待依赖倒置任务处理。
            from agent.service import AgentService

            await AgentService(self.db).require_runnable(session.agent_id, workspace_id)
        if session.workflow_id is not None:
            # 局部导入：真实双向 Service 依赖，改顶层仍成环 workflow.service -> agent.service
            # -> ... -> task.service -> session.service，待依赖倒置任务处理。
            from workflow.service import WorkflowService

            await WorkflowService(self.db).require_runnable(session.workflow_id, workspace_id)
        return await self.mark_running(session_id)

    @staticmethod
    def resolve_effective_workflow_id(
        requested_workflow_id: int | None,
        session: SessionRecord | None,
    ) -> int | None:
        """本次消息实际生效的 Workflow：请求体的 workflow_id 或会话已锁定的 workflow_id。

        会话锁定 Workflow 后前端不再带 workflow_id，只看请求体会让第二轮静默退回普通 Agent
        路径且不报错；发消息入口必须以本方法的结果作为分发判据。
        """
        if requested_workflow_id is not None:
            return requested_workflow_id
        return session.workflow_id if session is not None else None

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
        provider: str | None = None,
        model: str | None = None,
    ) -> SessionMessage:
        """保存会话消息并估算 token 数。

        provider/model 是成本归因元数据，仅 LLM 产出的 assistant 消息由调用方
        显式传入（实际生效的模型 ID，非场景档位名）；user/tool 消息保持默认 None。
        """
        token_estimate = len(str(content)) // 4
        return await self.repo.add_message(
            session_id,
            role,
            content,
            token_estimate,
            turn_id=turn_id,
            provider=provider,
            model=model,
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
        """把持久化消息转换为中立上下文，可对指定消息创建临时请求副本。

        三类角色均原样透出库中的中立形状（user 纯文本、assistant 中立块数组、
        tool 为 ToolResultMessage dict），不做任何 provider wire 编码——
        编码（含 Anthropic 的连续工具结果合并）由各 LLM Client 的出站翻译私有完成。
        deepcopy 隔离 ORM 可变字段：下游压缩会原地改写上下文，不得写回落库对象。
        """
        context: list[dict[str, Any]] = []
        for message in messages:
            if message.role not in {"user", "assistant", "tool"}:
                continue
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
        return context

    def _summarize_title(self, content: str) -> str:
        """从首条消息生成一个简短会话标题。"""
        normalized = " ".join(content.strip().split())
        if not normalized:
            return "新会话"
        if len(normalized) <= 24:
            return normalized
        return f"{normalized[:24].rstrip()}..."

    # ---- 执行租约(run 即 turn):事务由调用方提交,本层只做校验与写入 ----

    async def claim_turn_lease(self, turn_id: UUID, owner: str, lease_seconds: float) -> int:
        """认领执行租约,返回认领时的 last_event_offset;已有执行中的任务时拒绝。"""
        offset = await self.repo.claim_turn_lease(turn_id, owner, lease_seconds)
        if offset is None:
            raise AgentException.message(
                "该轮次已有执行中的任务",
                {"turn_id": str(turn_id)},
                status_code=409,
            )
        return offset

    async def renew_turn_lease(self, turn_id: UUID, owner: str, lease_seconds: float) -> bool:
        """心跳续约;返回 False 表示租约已不属于 owner。"""
        return await self.repo.renew_turn_lease(turn_id, owner, lease_seconds)

    async def reserve_turn_offsets(self, turn_id: UUID, owner: str, count: int) -> int:
        """在租约内预留连续 offset,返回首个;租约丢失时抛 LeaseLostError。"""
        first = await self.repo.reserve_turn_offsets(turn_id, owner, count)
        if first is None:
            raise LeaseLostError("执行租约已失效", {"turn_id": str(turn_id)})
        return first

    async def verify_turn_lease(self, turn_id: UUID, owner: str) -> None:
        """只读校验租约归属,供高频的取消可见性检查使用。"""
        if await self.repo.get_turn_lease_owner(turn_id) != owner:
            raise LeaseLostError("执行租约已失效", {"turn_id": str(turn_id)})

    async def lock_owned_turn(
        self,
        session_id: int,
        turn_id: UUID,
        owner: str,
    ) -> tuple[SessionRecord, SessionTurnRecord]:
        """按「会话 → 轮次」顺序加锁并确认租约仍属于 owner,供执行方提交终态前隔离。

        与取消接口、回收任务的加锁顺序一致,避免互相等待成死锁。
        """
        session = await self.lock_required(session_id)
        turn = await self.repo.get_turn_for_update(turn_id)
        if turn is None or turn.lease_owner != owner:
            raise LeaseLostError("执行租约已失效", {"turn_id": str(turn_id)})
        return session, turn

    async def finish_turn_execution(
        self,
        session_id: int,
        turn_id: UUID,
        owner: str,
        *,
        partial: list[MessageDraft] | None = None,
        interrupted_session_status: str = "failed",
    ) -> TurnTermination:
        """持有者收尾:为 run_end 分配 offset;仍是 running 时按中断收尾。

        仍是 running 说明执行被取消或失联:同一事务里先补写 partial(执行事务回滚掉的
        半截回答),再改 turn=interrupted、会话=interrupted_session_status(用户取消为 idle,
        其余为 failed),保证半截回答排在下一条用户消息之前。
        租约在 run_end 写入 Stream 之后才由 release_turn_lease 清除,
        保证下一次执行的事件一定排在本次 run_end 之后。
        """
        session, turn = await self.lock_owned_turn(session_id, turn_id, owner)
        if turn.status == "running":
            await self._save_partial(turn, partial or [])
            await self.repo.update_turn_status(turn, "interrupted")
            if session.status == "running":
                await self.repo.update_status(session, interrupted_session_status)
        offset = await self.reserve_turn_offsets(turn_id, owner, 1)
        return TurnTermination(turn.id, session.id, turn.status, session.status, offset)

    async def _save_partial(self, turn: SessionTurnRecord, drafts: list[MessageDraft]) -> None:
        """按顺序落库取消时补写的消息;provider/model 未知,成本归因留空。"""
        for draft in drafts:
            await self.add_message(turn.session_id, draft.role, draft.content, turn_id=turn.id)

    async def release_turn_lease(self, turn_id: UUID, owner: str) -> None:
        """清除 owner 自己的租约;已被他人清除时为空操作。"""
        await self.repo.clear_turn_lease(turn_id, owner=owner)

    async def cancel_turn_execution(
        self,
        session_id: int,
        turn_id: UUID,
        *,
        partial_loader: PartialLoader | None = None,
    ) -> TurnTermination:
        """用户取消执行中的轮次:清除租约并为 run_end 分配 offset。

        running → 补写 partial_loader 给出的半截回答、turn=interrupted、会话=idle,同一事务;
        已 completed 但仍在唤醒队友等情况只停止执行,不改业务状态;
        awaiting_interaction 须走交互取消接口。partial_loader 在锁内调用,
        持有者此后提交终态时会在 lock_owned_turn 处被隔离,不会重复写入。
        """
        session = await self.lock_required(session_id)
        found = await self.repo.get_turn_lease_state(turn_id, for_update=True)
        if found is None or found[0].session_id != session_id:
            raise AgentException.message("交互轮次不存在", status_code=404)
        turn, executing = found
        if not executing:
            raise AgentException.message("该轮次没有执行中的任务", status_code=409)
        if turn.status == "awaiting_interaction":
            raise AgentException.message(
                "轮次正在等待人工交互,请使用交互取消接口",
                {"turn_id": str(turn_id)},
                status_code=409,
            )
        if turn.status == "running" and partial_loader is not None:
            await self._save_partial(turn, await partial_loader())
        offset = await self.repo.clear_turn_lease(turn_id, reserve_offset=True)
        if offset is None:
            raise AgentException.message("交互轮次不存在", status_code=404)
        if turn.status == "running":
            await self.repo.update_turn_status(turn, "interrupted")
            await self.repo.update_status(session, "idle")
        return TurnTermination(turn.id, session.id, turn.status, session.status, offset)

    async def release_turn_for_detached_end(
        self,
        session: SessionRecord,
        turn: SessionTurnRecord,
    ) -> TurnTermination:
        """执行之外改了轮次终态(如交互取消)时,清除残留租约并为补写的 run_end 分配 offset。

        调用方须已按「会话 → 轮次」顺序持锁并写好终态。offset 由与租约预留同一行的原子
        UPDATE 分配,不与任何写入者重复;残留的持有者(挂起后正在收尾或刚认领的恢复执行)
        随后在 offset 校验或 lock_owned_turn 处被隔离,不再写入。
        """
        offset = await self.repo.clear_turn_lease(turn.id, reserve_offset=True)
        if offset is None:
            raise AgentException.message("交互轮次不存在", status_code=404)
        return TurnTermination(turn.id, session.id, turn.status, session.status, offset)

    async def list_reapable_turn_ids(self, *, include_unleased_running: bool) -> list[UUID]:
        return await self.repo.list_reapable_turn_ids(
            include_unleased_running=include_unleased_running,
        )

    async def reap_turn(
        self,
        turn_id: UUID,
        *,
        include_unleased_running: bool,
    ) -> TurnTermination | None:
        """回收一个孤儿轮次;加锁后复核条件,已被续约或认领时返回 None。

        running 改 interrupted,会话仍为 running 时改 failed(与 _mark_abnormal_end 一致,
        failed 可再次执行);不重跑。
        """
        turn = await self.repo.get_turn(turn_id)
        if turn is None:
            return None
        session = await self.lock_required(turn.session_id)
        found = await self.repo.get_turn_lease_state(turn_id, for_update=True)
        if found is None:
            return None
        turn, executing = found
        expired = turn.lease_owner is not None and not executing
        unleased = (
            include_unleased_running and turn.lease_owner is None and turn.status == "running"
        )
        if not (expired or unleased):
            return None
        offset = await self.repo.clear_turn_lease(turn_id, reserve_offset=True)
        if offset is None:
            return None
        if turn.status == "running":
            await self.repo.update_turn_status(turn, "interrupted")
            if session.status == "running":
                await self.repo.update_status(session, "failed")
        return TurnTermination(turn.id, session.id, turn.status, session.status, offset)

    async def get_turn_execution(self, turn_id: UUID) -> TurnExecutionState | None:
        found = await self.repo.get_turn_lease_state(turn_id)
        return None if found is None else self._execution_state(*found)

    async def get_session_turn_execution(
        self,
        session_id: int,
        turn_id: UUID,
    ) -> TurnExecutionState:
        """读取属于指定会话的轮次执行状态,不属于时按不存在处理。"""
        state = await self.get_turn_execution(turn_id)
        if state is None or state.session_id != session_id:
            raise AgentException.message("交互轮次不存在", status_code=404)
        return state

    async def get_active_turn(self, session_id: int) -> TurnExecutionState | None:
        """会话最新一轮的状态与是否有执行中的任务;尚无轮次时返回 None。"""
        found = await self.repo.get_latest_turn_lease_state(session_id)
        return None if found is None else self._execution_state(*found)

    @staticmethod
    def _execution_state(turn: SessionTurnRecord, executing: bool) -> TurnExecutionState:
        return TurnExecutionState(
            turn_id=turn.id,
            session_id=turn.session_id,
            status=turn.status,
            executing=executing,
            last_event_offset=turn.last_event_offset,
        )
