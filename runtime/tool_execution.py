"""主 Agent 的工具执行：逐个审核工具调用，按审核结论拒绝、停下等待人工交互，或经网关执行并写回结果。

主循环与人工交互恢复后的续跑共用这一份实现。审核要求人工交互时只停在该调用并返回
ToolApprovalRequired，冻结续跑状态与挂起由人工交互流程负责，这里不碰交互的持久化。
"""

import logging
from dataclasses import dataclass, replace
from uuid import UUID

from core.config import settings
from core.event_bus import StreamBus
from core.events import Actor, StreamEvent, ToolInfo
from harness.app.ports import ToolCallReviewer
from harness.app.session_port import MessageWriter, StoredTurn
from harness.domain.budget import ToolCallBudget
from harness.domain.messages import ToolCall, ToolResult
from harness.domain.review import SOURCE_PERMISSION, ReviewRequest
from plan.service import PlanService
from runtime.turn_config import TurnConfiguration
from tools.models import ToolCallRecord
from tools.service import ToolService

logger = logging.getLogger(__name__)


def default_tool_budget() -> ToolCallBudget:
    """一次 Turn 的默认工具预算：总额度是轮次上限的 4 倍，单个工具不超过轮次上限。"""
    return ToolCallBudget(
        total_limit=max(1, settings.max_tool_iterations * 4),
        per_tool_limit=max(1, settings.max_tool_iterations),
    )


@dataclass(frozen=True)
class ToolApprovalRequired:
    """审核要求人工交互：kind 是交互类型，remaining 从该调用起是本批还没完成的调用。"""

    kind: str
    remaining: list[ToolCall]


class LeadToolExecutor:
    """主 Agent 一次运行的工具执行者，身份、工具边界与网关都取自回合配置。"""

    def __init__(
        self,
        *,
        configuration: TurnConfiguration,
        reviewer: ToolCallReviewer,
        plan_service: PlanService,
        messages: MessageWriter,
        tool_service: ToolService,
        bus: StreamBus,
        user_id: int | None,
        workspace_id: int | None,
    ) -> None:
        self._configuration = configuration
        self._reviewer = reviewer
        self._plan_service = plan_service
        self._messages = messages
        self._tool_service = tool_service
        self._bus = bus
        self._user_id = user_id
        self._workspace_id = workspace_id

    async def execute(
        self,
        session_id: int,
        turn: StoredTurn,
        actor: Actor,
        assistant_message_id: int,
        tool_calls: list[ToolCall],
        done_ids: list[str],
        budget: ToolCallBudget | None = None,
    ) -> ToolApprovalRequired | None:
        """按原始顺序审核并执行工具；需要人工交互时停在该调用，返回待挂起的剩余调用。

        审核规范化后的入参会写回 tool_calls，完成（含被拒）的调用 ID 追加到 done_ids。
        """
        if budget is None:
            budget = default_tool_budget()
        configuration = self._configuration
        for idx, tool_call in enumerate(tool_calls):
            # 恢复人工交互时，挂起前已完成的工具不再重复执行。
            if tool_call.id in done_ids:
                continue

            # 每个工具前重新读取 Plan 状态：同一批工具中前面的 Enter/ExitPlanMode
            # 经人工确认恢复后可能已改变它。
            if configuration.turbo:
                active_plan = None
            else:
                active_plan = await self._plan_service.get_active(session_id)
            plan_content = None
            if active_plan is not None:
                plan_content = active_plan.content
            decision = await self._reviewer.review(
                tool_call,
                ReviewRequest(
                    session_id=session_id,
                    profile=configuration.profile,
                    agent_type=configuration.profile,
                    budget=budget,
                    workspace_id=self._workspace_id,
                    allowed_skill_names=configuration.allowed_skill_names,
                    plan_content=plan_content,
                    tools=configuration.tools,
                    policies=configuration.policies,
                ),
            )
            # 审核可能规范化了入参：执行、落库与挂起的续跑状态都用审核后的入参。
            if decision.arguments is not tool_call.arguments:
                tool_call = replace(tool_call, arguments=decision.arguments)
                tool_calls[idx] = tool_call

            if decision.behavior == "deny":
                if decision.source == SOURCE_PERMISSION:
                    logger.warning(
                        "工具权限策略拒绝执行该工具调用",
                        extra={
                            "session_id": session_id,
                            "turn_id": str(turn.id),
                            "tool_name": tool_call.tool_name,
                            "decision": "deny",
                        },
                    )
                await self.record_response(
                    session_id,
                    turn.id,
                    actor,
                    tool_call,
                    decision.message,
                    is_error=True,
                )
                done_ids.append(tool_call.id)
                continue

            if decision.behavior == "ask":
                interaction = decision.require_interaction()
                if decision.source == SOURCE_PERMISSION:
                    logger.info(
                        "工具权限策略要求人工审批，挂起等待用户确认",
                        extra={
                            "session_id": session_id,
                            "turn_id": str(turn.id),
                            "tool_name": tool_call.tool_name,
                            "decision": "ask",
                        },
                    )
                remaining = [item for item in tool_calls[idx:] if item.id not in done_ids]
                return ToolApprovalRequired(kind=interaction, remaining=remaining)

            # 本轮实际生效的工具名单按刚读到的 Plan 状态计算;委派自建 Agent 时,
            # 被委派方的工具不能超出它,否则 Plan Mode 下可借子 Agent 绕过只读限制。
            round_tool_names = configuration.tools_for_mode(active_plan is not None).names
            await self.execute_one(
                session_id,
                turn.id,
                actor,
                assistant_message_id,
                tool_call,
                round_tool_names=round_tool_names,
            )
            done_ids.append(tool_call.id)
        return None

    async def execute_one(
        self,
        session_id: int,
        turn_id: UUID,
        actor: Actor,
        assistant_message_id: int,
        tool_call: ToolCall,
        record: ToolCallRecord | None = None,
        round_tool_names: frozenset[str] | None = None,
    ) -> str:
        """执行单个工具:经网关执行、emit tool_result、落 tool 消息。

        record 非空时复用该 tool_calls 记录(审批通过后复用之前的 awaiting 记录),
        避免同一调用留下两条记录。round_tool_names 是本轮实际生效的工具名单,
        透传给工具上下文,供 Agent 工具委派自建 Agent 时裁剪工具。
        """
        configuration = self._configuration
        result = await configuration.gateway.run(
            session_id,
            tool_call.tool_name,
            tool_call.arguments,
            message_id=assistant_message_id,
            bus=self._bus,
            record=record,
            actor=actor,
            turn_id=turn_id,
            user_id=self._user_id,
            workspace_id=self._workspace_id,
            agent_id=configuration.agent_id,
            allowed_skill_names=configuration.allowed_skill_names,
            round_tool_names=round_tool_names,
        )
        await self._bus.emit(
            StreamEvent.tool_result(
                actor,
                session_id,
                ToolInfo(
                    id=tool_call.id,
                    name=tool_call.tool_name,
                    output=result.text,
                    is_error=result.is_error,
                ),
            )
        )
        await self._messages.add_message(
            session_id,
            "tool",
            ToolResult(
                tool_call_id=tool_call.id,
                tool_name=tool_call.tool_name,
                arguments=tool_call.arguments,
                content=result.text,
                is_error=result.is_error,
            ).to_content(),
            turn_id=turn_id,
        )
        return result.text

    async def record_response(
        self,
        session_id: int,
        turn_id: UUID,
        actor: Actor,
        tool_call: ToolCall,
        output: str,
        *,
        is_error: bool,
        record: ToolCallRecord | None = None,
    ) -> None:
        """持久化不经过真实工具执行的合成 tool_result。"""
        if record is not None:
            if is_error:
                await self._tool_service.reject_awaiting_call(record, output)
            else:
                await self._tool_service.complete_call(record, output)
        await self._bus.emit(
            StreamEvent.tool_result(
                actor,
                session_id,
                ToolInfo(
                    id=tool_call.id,
                    name=tool_call.tool_name,
                    output=output,
                    is_error=is_error,
                ),
            )
        )
        await self._messages.add_message(
            session_id,
            "tool",
            ToolResult(
                tool_call_id=tool_call.id,
                tool_name=tool_call.tool_name,
                arguments=tool_call.arguments,
                content=output,
                is_error=is_error,
            ).to_content(),
            turn_id=turn_id,
        )
