"""主 Agent 的人工交互流程：挂起、保存响应、取消暂停点、恢复后应用四类交互并续跑同一批工具。

挂起时把剩余工具、已完成的调用、附加上下文与工具预算冻结进 continuation，恢复时据此还原；
四类交互（工具审批、提问、进入与退出 Plan Mode）的响应在这里翻译成 tool_result。
"""

import logging
from dataclasses import dataclass, replace
from typing import Any
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from core.errors import AgentException
from core.event_bus import StreamBus
from core.events import ORCHESTRATOR_ACTOR, Actor, InteractionInfo, StreamEvent
from harness.app.session_port import StoredSession, StoredTurn, TurnStateStore
from harness.domain.budget import ToolCallBudget
from harness.domain.messages import ToolCall
from harness.domain.prompts.plan_mode import ENTER_PLAN_MODE_RESULT
from interaction.models import InteractionRequestRecord, RuntimeSuspensionRecord
from interaction.schemas import AskUserQuestionRequestPayload, InteractionRequestCreate
from interaction.service import InteractionService
from permission.service import DANGEROUS_TOOLS, PermissionService
from plan.service import PlanService
from runtime.execution import write_detached_run_end
from runtime.tool_execution import LeadToolExecutor, default_tool_budget
from runtime.turn_config import TurnConfiguration
from tools.models import ToolCallRecord
from tools.service import ToolService

logger = logging.getLogger(__name__)

# 已经终结的暂停点：相同响应再次恢复时只重放结束事件。
_FINISHED_STATUSES = frozenset({"resolved", "cancelled", "failed"})


@dataclass(frozen=True)
class SuspendedInteraction:
    """提交挂起事务后需要发布给客户端的交互事件快照。"""

    session_id: int
    suspension_id: UUID
    request_id: UUID
    kind: str
    request_payload: dict[str, Any]
    schema_version: int


class InteractionFlow:
    """一次运行的人工交互流程，工具边界取自回合配置，工具执行与合成结果经 LeadToolExecutor。

    保存响应与取消暂停点是请求阶段的完整用例，在流程内显式提交：两者都要在返回 HTTP 响应前
    让结果可见。挂起与恢复只写不提交，由运行时在隔离租约后统一提交。
    """

    def __init__(
        self,
        *,
        db: AsyncSession,
        configuration: TurnConfiguration,
        executor: LeadToolExecutor,
        sessions: TurnStateStore,
        interaction_service: InteractionService,
        plan_service: PlanService,
        permission_service: PermissionService,
        tool_service: ToolService,
        bus: StreamBus,
        user_id: int | None,
        workspace_id: int | None,
    ) -> None:
        self._db = db
        self._configuration = configuration
        self._executor = executor
        self._sessions = sessions
        self._interaction_service = interaction_service
        self._plan_service = plan_service
        self._permission_service = permission_service
        self._tool_service = tool_service
        self._bus = bus
        self._user_id = user_id
        self._workspace_id = workspace_id

    async def run_tool_calls(
        self,
        session_id: int,
        session: StoredSession,
        turn: StoredTurn,
        actor: Actor,
        assistant_message_id: int,
        tool_calls: list[ToolCall],
        done_ids: list[str],
        additional_context: str | None,
        existing_suspension: RuntimeSuspensionRecord | None = None,
        budget: ToolCallBudget | None = None,
    ) -> SuspendedInteraction | None:
        """按原始顺序执行工具；需要人工输入时挂起并返回可发布的交互事件快照。

        主循环与恢复后的续跑都走这里；existing_suspension 非空表示在同一暂停点上继续挂起。
        """
        if budget is None:
            budget = default_tool_budget()
        approval = await self._executor.execute(
            session_id,
            turn,
            actor,
            assistant_message_id,
            tool_calls,
            done_ids,
            budget,
        )
        if approval is None:
            return None
        return await self._suspend(
            session_id,
            session,
            turn,
            assistant_message_id,
            approval.remaining,
            done_ids,
            additional_context,
            approval.kind,
            existing_suspension,
            budget,
        )

    async def _suspend(
        self,
        session_id: int,
        session: StoredSession,
        turn: StoredTurn,
        assistant_message_id: int,
        remaining: list[ToolCall],
        done_ids: list[str],
        additional_context: str | None,
        kind: str,
        existing_suspension: RuntimeSuspensionRecord | None,
        budget: ToolCallBudget,
    ) -> SuspendedInteraction:
        """冻结 continuation 并创建一项业务交互请求。"""
        target = remaining[0]
        record = await self._configuration.gateway.start_call(
            session_id,
            target.tool_name,
            target.arguments,
            message_id=assistant_message_id,
            agent_id=self._configuration.agent_id,
        )
        await self._tool_service.mark_call_awaiting(record)
        continuation = {
            "assistant_message_id": assistant_message_id,
            "pending": [
                {
                    "tool_use_id": tu.id,
                    "tool_name": tu.tool_name,
                    "input": tu.arguments,
                    "tool_call_id": record.id if tu.id == target.id else None,
                }
                for tu in remaining
            ],
            "done_tool_use_ids": list(done_ids),
            "additional_context": additional_context,
            "tool_budget": budget.to_dict(),
        }
        request_payload = await self._build_payload(session_id, kind, target)
        request_create = InteractionRequestCreate(
            kind=kind,
            request_payload=request_payload,
            tool_call_id=record.id,
            tool_use_id=target.id,
        )

        if existing_suspension is None:
            suspension, request = await self._interaction_service.create_suspension_with_request(
                session_id,
                turn.id,
                continuation,
                {"mode": "sequential"},
                request_create,
            )
        else:
            suspension, request = (
                await self._interaction_service.continue_suspension_with_request(
                    existing_suspension.id,
                    continuation,
                    request_create,
                )
            )
        await self._sessions.set_status(session, "awaiting_interaction")
        await self._sessions.mark_turn_status(turn, "awaiting_interaction")
        logger.info(
            "人工交互挂起，等待用户响应",
            extra={
                "session_id": session_id,
                "turn_id": str(turn.id),
                "kind": kind,
                "suspension_id": str(suspension.id),
            },
        )
        return SuspendedInteraction(
            session_id=session_id,
            suspension_id=suspension.id,
            request_id=request.id,
            kind=request.kind,
            request_payload=dict(request.request_payload),
            schema_version=request.schema_version,
        )

    @staticmethod
    def _ask_reason(tool_name: str) -> str:
        """给前端展示的审批原因文案。"""
        if tool_name in DANGEROUS_TOOLS:
            return "危险工具默认需审批"
        return "该工具已被配置为需审批"

    async def _build_payload(
        self,
        session_id: int,
        kind: str,
        target: ToolCall,
    ) -> dict[str, Any]:
        if kind == "tool_approval":
            return {
                "tool": {
                    "id": target.id,
                    "name": target.tool_name,
                    "input": target.arguments,
                },
                "reason": self._ask_reason(target.tool_name),
                "allowed_decisions": ["allow_once", "always_allow", "deny"],
                "can_update_input": True,
            }
        if kind == "user_question":
            return AskUserQuestionRequestPayload.model_validate(target.arguments).model_dump(
                mode="json",
                by_alias=True,
                exclude_none=True,
            )
        if kind == "enter_plan_mode":
            return {"message": "是否进入只读Plan Mode？"}
        if kind == "exit_plan_mode":
            plan = await self._plan_service.get_active_required(session_id)
            return {
                "plan_id": str(plan.id),
                "plan": plan.content,
                "plan_version": plan.version,
                "allowedPrompts": target.arguments.get("allowedPrompts", []),
            }
        raise AgentException.message("不支持的人工交互类型")

    async def save_response(
        self,
        session_id: int,
        session: StoredSession,
        request_id: UUID,
        kind: str,
        response_payload: dict[str, Any],
    ) -> UUID:
        """校验并提交用户响应，使格式错误和冲突能在 SSE 建立前返回 HTTP 错误。

        session 是调用方已加锁读出的会话。返回暂停点所属轮次 ID,恢复执行沿用同一个轮次。
        """
        current = await self._interaction_service.get_request_required(
            request_id,
            session_id=session_id,
        )
        is_replay = current.status == "resolved"
        if not is_replay and session.status != "awaiting_interaction":
            raise AgentException.message("会话当前没有待处理的人工交互")

        normalized = await self._normalize_response(
            session_id,
            request_id,
            kind,
            response_payload,
        )
        request, suspension = await self._interaction_service.resolve_request(
            request_id,
            normalized,
            self._user_id,
            session_id=session_id,
            expected_kind=kind,
        )
        turn = await self._sessions.get_turn_required(suspension.turn_id)
        if turn.session_id != session_id:
            raise AgentException.message("人工交互对应的轮次不属于指定会话")
        if turn.user_id != self._user_id or turn.workspace_id != self._workspace_id:
            raise AgentException.message("无权限恢复该交互轮次", status_code=403)
        if not is_replay and turn.status != "awaiting_interaction":
            raise AgentException.message("人工交互对应的轮次不可恢复")
        if request.response_payload != normalized:
            raise AgentException.message("人工交互响应持久化结果不一致")

        # 流建立或客户端连接随后失败时，响应仍可从 resuming 暂停点重放。
        await self._db.commit()
        return turn.id

    async def _normalize_response(
        self,
        session_id: int,
        request_id: UUID,
        kind: str,
        response_payload: dict[str, Any],
    ) -> dict[str, Any]:
        """在响应落库前完成依赖工具集的校验。"""
        if kind != "tool_approval" or response_payload.get("updated_input") is None:
            return response_payload
        request = await self._interaction_service.get_request_required(
            request_id,
            session_id=session_id,
        )
        tool = request.request_payload.get("tool") or {}
        tool_name = tool.get("name")
        if not isinstance(tool_name, str):
            raise AgentException.message("工具审批请求缺少有效工具名")
        normalized = dict(response_payload)
        normalized["updated_input"] = self._configuration.tools.validate_input(
            tool_name,
            response_payload["updated_input"],
        )
        return normalized

    async def cancel(self, session_id: int, suspension_id: UUID) -> RuntimeSuspensionRecord:
        """取消当前暂停点，原子收尾关联的 Turn 与工具调用，提交后补写 run_end。

        补写的 run_end(interrupted, cancelled=true)让快照离开 awaiting_interaction、
        不再显示待处理交互;取消之后才订阅的客户端同样拿到该终态。
        """
        session = await self._sessions.ensure_interaction_resumable(session_id)
        suspension = await self._interaction_service.get_suspension_required(
            suspension_id,
            session_id=session_id,
        )
        turn = await self._sessions.get_turn_required(suspension.turn_id)
        if turn.session_id != session_id:
            raise AgentException.message("运行暂停点与交互轮次不匹配")
        if turn.user_id != self._user_id or turn.workspace_id != self._workspace_id:
            raise AgentException.message("无权限取消该交互轮次", status_code=403)

        suspension, requests = await self._interaction_service.cancel_suspension(
            suspension_id,
            self._user_id,
            session_id=session_id,
        )
        await self._finish_cancelled(session, turn, requests, "用户取消了运行暂停点。")
        termination = await self._sessions.release_turn_for_detached_end(session, turn)
        # 显式提交:数据库是事实来源,补写 run_end 必须在终态提交之后;
        # 与轮次取消接口一致,写事件流失败只记日志,不影响已提交的取消。
        await self._db.commit()
        await write_detached_run_end(termination, cancelled=True)
        logger.info(
            "人工交互已取消",
            extra={
                "session_id": session_id,
                "turn_id": str(turn.id),
                "kind": ",".join(req.kind for req in requests),
                "suspension_id": str(suspension_id),
            },
        )
        return suspension

    async def load_resume_point(self, session_id: int, request_id: UUID) -> ResumePoint:
        """读取恢复所需的请求、暂停点（加锁）与轮次，不做校验。

        调用方先锁会话再调用：所有恢复者都按 Session -> Suspension 的顺序加锁，竞争者会在首个
        恢复事务提交后读取最新状态，不会重复应用工具或 Plan Mode 副作用。
        """
        request = await self._interaction_service.get_request_required(
            request_id,
            session_id=session_id,
        )
        suspension = await self._interaction_service.lock_suspension_required(
            request.suspension_id,
            session_id=session_id,
        )
        turn = await self._sessions.get_turn_required(suspension.turn_id)
        return ResumePoint(request=request, suspension=suspension, turn=turn)

    def resume_step(self, session_id: int, session: StoredSession, point: ResumePoint) -> str:
        """校验恢复现场并决定下一步，返回 RESUME_* 之一；现场不一致时抛中文异常。"""
        request, suspension, turn = point.request, point.suspension, point.turn
        if request.suspension_id != suspension.id or turn.session_id != session_id:
            raise AgentException.message("人工交互与运行暂停点不匹配")
        if turn.user_id != self._user_id or turn.workspace_id != self._workspace_id:
            raise AgentException.message("无权限恢复该交互轮次", status_code=403)

        # 上一次恢复已经推进到同一 suspension 的下一项交互时，相同响应只需
        # 重放当前待处理请求，不能再次执行已经完成的工具副作用。
        if suspension.status == "pending":
            if session.status != "awaiting_interaction":
                raise AgentException.message("人工交互暂停点与会话状态不一致")
            return RESUME_REPLAY_PENDING
        if suspension.status in _FINISHED_STATUSES:
            return RESUME_REPLAY_FINISHED
        if suspension.status != "resuming":
            raise AgentException.message(
                "运行暂停点当前不可恢复",
                {"status": suspension.status},
            )
        if session.status != "awaiting_interaction" or turn.status != "awaiting_interaction":
            raise AgentException.message("人工交互对应的会话或轮次不可恢复")

        persisted_response = request.response_payload
        if persisted_response is None:
            raise AgentException.message("人工交互缺少已持久化的响应")
        if request.kind == "user_question" and persisted_response["decision"] == "cancel":
            return RESUME_CANCEL_QUESTION
        return RESUME_APPLY

    async def cancel_question(
        self,
        session_id: int,
        session: StoredSession,
        point: ResumePoint,
    ) -> None:
        """用户取消提问：取消暂停点并把本轮中断，不再续跑。"""
        _, requests = await self._interaction_service.cancel_suspension(
            point.suspension.id,
            self._user_id,
            session_id=session_id,
        )
        await self._finish_cancelled(session, point.turn, requests, "用户取消了问题交互。")

    @staticmethod
    def restored_budget(suspension: RuntimeSuspensionRecord) -> ToolCallBudget:
        """取回挂起时冻结的工具预算，旧 continuation 没有预算时按配置新建。"""
        return ToolCallBudget.from_dict(
            suspension.continuation_payload.get("tool_budget"),
            default=default_tool_budget(),
        )

    async def mark_resumed(
        self,
        session: StoredSession,
        turn: StoredTurn,
        suspension: RuntimeSuspensionRecord,
    ) -> None:
        """同一批工具全部处理完：暂停点完结，会话与轮次回到 running，接着续跑主循环。"""
        await self._interaction_service.mark_suspension_resolved(suspension.id)
        await self._sessions.set_status(session, "running")
        await self._sessions.mark_turn_status(turn, "running")

    async def pending_interaction(self, session_id: int) -> SuspendedInteraction:
        """读取已提交的唯一待处理请求，用于幂等重放 SSE 通知。"""
        pending = await self._interaction_service.get_pending(session_id)
        if pending is None:
            raise AgentException.message("会话没有可重放的人工交互")
        suspension, requests = pending
        if suspension.status != "pending" or len(requests) != 1:
            raise AgentException.message(
                "运行暂停点的待处理请求数量无效",
                {"status": suspension.status, "request_count": len(requests)},
            )
        request = requests[0]
        return SuspendedInteraction(
            session_id=session_id,
            suspension_id=suspension.id,
            request_id=request.id,
            kind=request.kind,
            request_payload=dict(request.request_payload),
            schema_version=request.schema_version,
        )

    async def emit(self, interaction: SuspendedInteraction) -> None:
        """挂起事务提交之后向客户端发布交互请求事件。"""
        await self._bus.emit(
            StreamEvent.interaction_request(
                ORCHESTRATOR_ACTOR,
                interaction.session_id,
                InteractionInfo(
                    request_id=str(interaction.request_id),
                    suspension_id=str(interaction.suspension_id),
                    kind=interaction.kind,
                    schema_version=interaction.schema_version,
                    request_payload=interaction.request_payload,
                ),
            )
        )

    async def apply_and_continue(
        self,
        session_id: int,
        session: StoredSession,
        turn: StoredTurn,
        request: InteractionRequestRecord,
        suspension: RuntimeSuspensionRecord,
        *,
        budget: ToolCallBudget | None = None,
    ) -> SuspendedInteraction | None:
        """应用已持久化的业务响应，并继续同一 assistant 工具序列。"""
        pending = suspension.continuation_payload
        if budget is None:
            budget = self.restored_budget(suspension)
        pending_items = pending.get("pending", [])
        if not pending_items:
            raise AgentException.message("运行暂停点缺少待处理工具")
        assistant_message_id = pending.get("assistant_message_id")
        if not isinstance(assistant_message_id, int):
            raise AgentException.message("运行暂停点缺少关联的助手消息")
        done_ids = list(pending.get("done_tool_use_ids", []))
        tool_calls = [
            ToolCall(
                id=it["tool_use_id"], tool_name=it["tool_name"], arguments=it.get("input") or {}
            )
            for it in pending_items
        ]
        target_item = pending_items[0]
        target = tool_calls[0]
        # 复用挂起时建的 awaiting 记录(避免同一调用留两条)。
        record: ToolCallRecord | None = None
        tool_call_id = target_item.get("tool_call_id")
        if tool_call_id is not None:
            record = await self._tool_service.get_call(tool_call_id)
        if record is None or request.tool_call_id != record.id or request.tool_use_id != target.id:
            raise AgentException.message("人工交互与工具调用记录不匹配")
        response = request.response_payload
        if response is None:
            raise AgentException.message("人工交互缺少已持久化的响应")

        actor = ORCHESTRATOR_ACTOR
        if request.kind == "tool_approval":
            await self._apply_tool_approval(
                session_id,
                turn,
                actor,
                assistant_message_id,
                target,
                tool_calls,
                record,
                response,
            )
        elif request.kind == "user_question":
            await self._apply_user_question(turn, actor, target, record, request, response)
        elif request.kind == "enter_plan_mode":
            await self._apply_enter_plan_mode(turn, actor, target, record, response)
        elif request.kind == "exit_plan_mode":
            await self._apply_exit_plan_mode(turn, actor, target, record, request, response)
        else:
            raise AgentException.message("不支持的人工交互类型")
        done_ids.append(target.id)

        return await self.run_tool_calls(
            session_id,
            session,
            turn,
            actor,
            assistant_message_id,
            tool_calls,
            done_ids,
            additional_context=pending.get("additional_context"),
            existing_suspension=suspension,
            budget=budget,
        )

    async def _apply_tool_approval(
        self,
        session_id: int,
        turn: StoredTurn,
        actor: Actor,
        assistant_message_id: int,
        target: ToolCall,
        tool_calls: list[ToolCall],
        record: ToolCallRecord,
        response: dict[str, Any],
    ) -> None:
        decision = response["decision"]
        if decision == "deny":
            message = response.get("message") or "用户拒绝执行该工具调用。"
            await self._executor.record_response(
                session_id,
                turn.id,
                actor,
                target,
                message,
                is_error=True,
                record=record,
            )
            return

        updated_input = response.get("updated_input")
        if updated_input is not None:
            normalized = self._configuration.tools.validate_input(target.tool_name, updated_input)
            target = replace(target, arguments=normalized)
            tool_calls[0] = target
        if decision == "always_allow":
            await self._permission_service.add_always_allow(
                session_id,
                target.tool_name,
                response.get("always_scope", "session"),
                workspace_id=self._workspace_id,
            )
        # 审批通过后执行的若是委派,同样按此刻的 Plan 状态计算本轮生效的工具名单。
        active_plan = await self._plan_service.get_active(session_id)
        round_tool_names = self._configuration.tools_for_mode(active_plan is not None).names
        await self._executor.execute_one(
            session_id,
            turn.id,
            actor,
            assistant_message_id,
            target,
            record=record,
            round_tool_names=round_tool_names,
        )

    async def _apply_user_question(
        self,
        turn: StoredTurn,
        actor: Actor,
        target: ToolCall,
        record: ToolCallRecord,
        request: InteractionRequestRecord,
        response: dict[str, Any],
    ) -> None:
        decision = response["decision"]
        answers = response.get("answers") or {}
        if decision == "submit":
            output = _format_question_answers(
                request.request_payload,
                answers,
                response.get("annotations") or {},
            )
            is_error = False
        elif decision == "cancel":
            raise AgentException.message("取消问题交互必须由运行时中止路径处理")
        elif decision == "discuss":
            output = _format_question_discussion(
                request.request_payload,
                answers,
                response.get("message"),
            )
            is_error = True
        else:
            active_plan = await self._plan_service.get_active(turn.session_id)
            if active_plan is None:
                output = "只有Plan Mode中的规划访谈可以使用finish_plan_interview。"
            else:
                output = _format_finish_plan_interview(
                    request.request_payload,
                    answers,
                    response.get("message"),
                )
            is_error = True
        await self._executor.record_response(
            turn.session_id,
            turn.id,
            actor,
            target,
            output,
            is_error=is_error,
            record=record,
        )

    async def _apply_enter_plan_mode(
        self,
        turn: StoredTurn,
        actor: Actor,
        target: ToolCall,
        record: ToolCallRecord,
        response: dict[str, Any],
    ) -> None:
        if response["decision"] == "approve":
            await self._plan_service.enter(turn.session_id, turn.id)
            output = ENTER_PLAN_MODE_RESULT
            is_error = False
        else:
            feedback = response.get("feedback")
            output = "用户拒绝进入Plan Mode。"
            if feedback:
                output = f"{output}\n\n用户反馈：{feedback}"
            is_error = True
        await self._executor.record_response(
            turn.session_id,
            turn.id,
            actor,
            target,
            output,
            is_error=is_error,
            record=record,
        )

    async def _apply_exit_plan_mode(
        self,
        turn: StoredTurn,
        actor: Actor,
        target: ToolCall,
        record: ToolCallRecord,
        request: InteractionRequestRecord,
        response: dict[str, Any],
    ) -> None:
        if self._user_id is None:
            raise AgentException.message("退出Plan Mode缺少可信用户")
        edited_plan = response.get("edited_plan")
        feedback = response.get("feedback")
        if response["decision"] == "approve":
            allowed_prompts = request.request_payload.get("allowedPrompts") or []
            plan = await self._plan_service.approve_exit(
                turn.session_id,
                turn.id,
                self._user_id,
                allowed_prompts,
                feedback,
                edited_plan,
            )
            output = f"用户已批准以下计划并退出Plan Mode：\n\n{plan.content}"
            if feedback:
                output = f"{output}\n\n用户反馈：{feedback}"
            if allowed_prompts:
                output = (
                    f"{output}\n\nallowedPrompts仅作为计划声明保存，"
                    "不会绕过现有工具权限规则。"
                )
            is_error = False
        else:
            plan = await self._plan_service.reject_exit(
                turn.session_id,
                feedback,
                edited_plan,
            )
            output = "用户暂未批准计划，Plan Mode保持启用。"
            if feedback:
                output = f"{output}\n\n用户反馈：{feedback}"
            output = f"{output}\n\n当前计划：\n{plan.content}"
            is_error = True
        await self._executor.record_response(
            turn.session_id,
            turn.id,
            actor,
            target,
            output,
            is_error=is_error,
            record=record,
        )

    async def _finish_cancelled(
        self,
        session: StoredSession,
        turn: StoredTurn,
        requests: list[InteractionRequestRecord],
        message: str,
    ) -> None:
        """将取消投影到工具调用、Turn 与 Session，避免留下半挂起状态。"""
        for request in requests:
            if request.tool_call_id is not None:
                record = await self._tool_service.get_interaction_call_required(
                    request.tool_call_id,
                    session.id,
                )
                if record.status == "awaiting_interaction":
                    if request.tool_use_id is None:
                        raise AgentException.message("人工交互请求缺少工具调用ID")
                    await self._executor.record_response(
                        session.id,
                        turn.id,
                        ORCHESTRATOR_ACTOR,
                        ToolCall(
                            id=request.tool_use_id,
                            tool_name=record.tool_name,
                            arguments=dict(record.input_args),
                        ),
                        message,
                        is_error=True,
                        record=record,
                    )
                elif record.status not in {"succeeded", "failed", "rejected"}:
                    raise AgentException.message(
                        "人工交互关联的工具调用状态无效",
                        {"tool_call_id": record.id, "status": record.status},
                    )
        await self._sessions.mark_turn_status(turn, "interrupted")
        await self._sessions.set_status(session, "idle")


# 恢复执行的下一步：重放仍待处理的请求、重放已终结的暂停点、取消提问、应用响应后续跑。
# 运行时只需区分前三种，其余一律按应用响应续跑。
RESUME_REPLAY_PENDING = "replay_pending"
RESUME_REPLAY_FINISHED = "replay_finished"
RESUME_CANCEL_QUESTION = "cancel_question"
RESUME_APPLY = "apply"


@dataclass(frozen=True)
class ResumePoint:
    """一次恢复要用到的现场：用户已响应的请求、加锁读出的暂停点与它所属的轮次。"""

    request: InteractionRequestRecord
    suspension: RuntimeSuspensionRecord
    turn: StoredTurn


def _format_question_answers(
    request_payload: dict[str, Any],
    answers: dict[str, str],
    annotations: dict[str, dict[str, str]],
) -> str:
    parts: list[str] = []
    for question in request_payload.get("questions", []):
        question_text = question["question"]
        answer = answers[question_text]
        answer_parts = [f'"{question_text}"="{answer}"']
        annotation = annotations.get(question_text) or {}
        if annotation.get("preview"):
            answer_parts.append(f"selected preview:\n{annotation['preview']}")
        if annotation.get("notes"):
            answer_parts.append(f"user notes: {annotation['notes']}")
        parts.append(" ".join(answer_parts))
    joined = ", ".join(parts)
    return (
        "User has answered your questions: "
        f"{joined}. You can now continue with the user's answers in mind."
    )


def _format_question_discussion(
    request_payload: dict[str, Any],
    answers: dict[str, str],
    message: str | None,
) -> str:
    lines = ["用户希望先讨论这些问题。请先询问用户想澄清什么，再按需重写问题。"]
    for question in request_payload.get("questions", []):
        text = question["question"]
        lines.append(f'- "{text}": {answers.get(text, "（未回答）")}')
    if message:
        lines.append(f"用户补充：{message}")
    return "\n".join(lines)


def _format_finish_plan_interview(
    request_payload: dict[str, Any],
    answers: dict[str, str],
    message: str | None,
) -> str:
    lines = ["用户表示规划访谈信息已经足够。停止继续澄清并完成计划。"]
    for question in request_payload.get("questions", []):
        text = question["question"]
        lines.append(f'- "{text}": {answers.get(text, "（未回答）")}')
    if message:
        lines.append(f"用户补充：{message}")
    return "\n".join(lines)
