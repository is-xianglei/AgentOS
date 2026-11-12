"""规则链审核员：把预算、Plan 模式、入参、档位能力、交互工具和权限规则合成一条有序规则链。

每条规则本身也是 ToolCallReviewer：拒绝或挂起即终局；放行则带着（可能已规范化的）入参交给下一条，
全部放行才执行。主 Agent 与子代理的差异只体现在规则的组合上，见 for_main_agent 与 for_subagent。
"""

import json
from collections.abc import Sequence
from dataclasses import replace

from core.errors import AgentException
from harness.app.ports import ToolCallReviewer
from harness.domain.messages import ToolCall
from harness.domain.review import (
    INTERACTION_TOOL_KINDS,
    PLAN_MODE_ALLOWED_TOOLS,
    SOURCE_ARGUMENTS,
    SOURCE_BUDGET,
    SOURCE_CAPABILITY,
    SOURCE_INTERACTION,
    SOURCE_PERMISSION,
    SOURCE_PLAN_MODE_AGENT,
    SOURCE_PLAN_MODE_TOOLS,
    Decision,
    ReviewRequest,
    ToolPolicyLookup,
    check_capability,
)
from harness.domain.tool import ToolPolicy
from permission.service import PermissionService

# Plan 模式下 Agent 工具只能派发的只读子代理类型。
PLAN_MODE_AGENT_TYPES = frozenset({"Explore", "Plan"})


class PolicyReviewer(ToolCallReviewer):
    """按顺序执行一组规则：遇到拒绝或挂起立即返回，全部放行时返回最后一条规则的放行结论。"""

    def __init__(self, rules: Sequence[ToolCallReviewer]) -> None:
        if not rules:
            raise AgentException.message("审核规则链不能为空")
        self._rules = tuple(rules)

    @property
    def rules(self) -> tuple[ToolCallReviewer, ...]:
        return self._rules

    @classmethod
    def for_main_agent(
        cls,
        permission_service: PermissionService,
        *,
        policy_source: ToolPolicyLookup,
    ) -> PolicyReviewer:
        """主 Agent 的规则链：预算 → Plan 白名单 → 入参规范化 → 能力 → Plan 下的 Agent 类型
        → 交互工具 → 权限规则。权限规则要求审批时挂起为 tool_approval。

        policy_source 是组装根注入的平台工具集，请求没带本回合的策略全集时按它查能力策略。
        """
        return cls(
            [
                BudgetRule(),
                PlanModeToolsRule(),
                ArgumentsRule(),
                CapabilityRule(policy_source),
                PlanModeAgentRule(),
                InteractionToolRule(),
                PermissionRule(permission_service, policy_source),
            ]
        )

    @classmethod
    def for_subagent(
        cls,
        permission_service: PermissionService,
        *,
        policy_source: ToolPolicyLookup,
    ) -> PolicyReviewer:
        """子代理的规则链：预算 → 能力 + 权限规则。子代理没有审批通道，后两条的挂起合并为拒绝，
        拒绝时回给模型的文本换成子代理文案。子代理不做入参规范化、Plan 模式检查和交互工具处理。
        policy_source 的含义同 for_main_agent。
        """
        return cls(
            [
                BudgetRule(),
                NoApprovalChannelRule(
                    CapabilityRule(policy_source),
                    PermissionRule(permission_service, policy_source),
                ),
            ]
        )

    async def review(self, call: ToolCall, request: ReviewRequest) -> Decision:
        decision = await self._rules[0].review(call, request)
        for rule in self._rules[1:]:
            if decision.behavior != "allow":
                return decision
            # 放行的规则可以改写入参（如规范化），后续规则按改写后的入参判定。
            call = replace(call, arguments=decision.arguments)
            decision = await rule.review(call, request)
        return decision


def _policy_for(
    request: ReviewRequest,
    policy_source: ToolPolicyLookup,
    tool_name: str,
) -> ToolPolicy:
    """按名查能力策略：优先查请求带来的本回合策略全集，没带时查构造时注入的平台工具集。"""
    policies = request.policies
    if policies is None:
        policies = policy_source
    return policies.policy_for(tool_name)


class BudgetRule(ToolCallReviewer):
    """扣减一次工具调用额度，额度用尽时拒绝。排在第一条：被后续规则拒绝的调用照样计入额度。"""

    async def review(self, call: ToolCall, request: ReviewRequest) -> Decision:
        allowed, budget_error = request.budget.try_consume(call.tool_name)
        if not allowed:
            return Decision.deny(
                budget_error or "error: 工具调用预算不足。",
                arguments=call.arguments,
                source=SOURCE_BUDGET,
            )
        return Decision.allow(call.arguments, source=SOURCE_BUDGET)


class PlanModeToolsRule(ToolCallReviewer):
    """Plan 模式下只放行白名单里的工具。"""

    async def review(self, call: ToolCall, request: ReviewRequest) -> Decision:
        if request.plan_content is not None and call.tool_name not in PLAN_MODE_ALLOWED_TOOLS:
            return Decision.deny(
                "Plan Mode仅允许只读探索、AskUserQuestion、WritePlan和ExitPlanMode。",
                arguments=call.arguments,
                source=SOURCE_PLAN_MODE_TOOLS,
            )
        return Decision.allow(call.arguments, source=SOURCE_PLAN_MODE_TOOLS)


class ArgumentsRule(ToolCallReviewer):
    """按本轮可见工具集校验并规范化入参；放行时把入参改写为规范化后的值。

    工具不在本轮可见集里、或入参不合法时拒绝，把错误详情回给模型自行修正，不中断回合。
    """

    async def review(self, call: ToolCall, request: ReviewRequest) -> Decision:
        if request.tools is None:
            raise AgentException.message("入参规范化缺少本轮可见的工具集")
        try:
            normalized = request.tools.validate_input(call.tool_name, call.arguments)
        except AgentException as exc:
            details = ""
            if exc.details:
                details = f"：{json.dumps(exc.details, ensure_ascii=False, default=str)}"
            return Decision.deny(
                f"{exc.message}{details}",
                arguments=call.arguments,
                source=SOURCE_ARGUMENTS,
            )
        return Decision.allow(normalized, source=SOURCE_ARGUMENTS)


class CapabilityRule(ToolCallReviewer):
    """静态能力检查：档位能否调用该工具，以及 Skill 类工具是否只访问已绑定的 skill。

    策略按策略全集查，而不是本轮可见集：被裁掉的工具按可见集查会变成未知工具。
    交互工具会在后面提前挂起，所以这条排在它们之前，防止伪造的调用绕过档位边界。
    """

    def __init__(self, policy_source: ToolPolicyLookup) -> None:
        self._policy_source = policy_source

    async def review(self, call: ToolCall, request: ReviewRequest) -> Decision:
        denied = check_capability(
            call.tool_name,
            _policy_for(request, self._policy_source, call.tool_name),
            request.profile,
            call.arguments,
            request.allowed_skill_names,
        )
        if denied is not None:
            return Decision.deny(denied, arguments=call.arguments, source=SOURCE_CAPABILITY)
        return Decision.allow(call.arguments, source=SOURCE_CAPABILITY)


class PlanModeAgentRule(ToolCallReviewer):
    """Plan 模式下 Agent 工具只能派发 Explore 或 Plan 只读子代理，委派自建 Agent 也被拒绝。"""

    async def review(self, call: ToolCall, request: ReviewRequest) -> Decision:
        if (
            request.plan_content is not None
            and call.tool_name == "Agent"
            and call.arguments.get("agent_type") not in PLAN_MODE_AGENT_TYPES
        ):
            return Decision.deny(
                "Plan Mode只能派发Explore或Plan只读子代理。",
                arguments=call.arguments,
                source=SOURCE_PLAN_MODE_AGENT,
            )
        return Decision.allow(call.arguments, source=SOURCE_PLAN_MODE_AGENT)


class InteractionToolRule(ToolCallReviewer):
    """交互工具先按 Plan 状态校验，合法时挂起等待用户；其余工具放行。交互工具不再走权限规则。"""

    async def review(self, call: ToolCall, request: ReviewRequest) -> Decision:
        interaction = INTERACTION_TOOL_KINDS.get(call.tool_name)
        if interaction is None:
            return Decision.allow(call.arguments, source=SOURCE_INTERACTION)
        plan_content = request.plan_content
        denied: str | None = None
        if call.tool_name == "EnterPlanMode" and plan_content is not None:
            denied = "当前会话已经处于Plan Mode。"
        elif call.tool_name == "ExitPlanMode":
            if plan_content is None:
                denied = "当前会话未处于Plan Mode，不能调用ExitPlanMode。"
            elif not plan_content.strip():
                denied = "计划正文为空。请先使用WritePlan保存计划，再请求退出。"
        if denied is not None:
            return Decision.deny(denied, arguments=call.arguments, source=SOURCE_INTERACTION)
        return Decision.ask(
            interaction,
            arguments=call.arguments,
            reason=f"交互工具需要用户响应: {call.tool_name}",
            source=SOURCE_INTERACTION,
        )


class PermissionRule(ToolCallReviewer):
    """按工作区与会话的权限规则判定 allow / ask / deny；ask 挂起为 tool_approval。

    无规则命中时的默认判定取自工具能力策略，策略与 CapabilityRule 同样按策略全集查好后传入。
    """

    def __init__(
        self,
        permission_service: PermissionService,
        policy_source: ToolPolicyLookup,
    ) -> None:
        self._permission_service = permission_service
        self._policy_source = policy_source

    async def review(self, call: ToolCall, request: ReviewRequest) -> Decision:
        behavior = await self._permission_service.evaluate(
            request.session_id,
            call.tool_name,
            call.arguments,
            agent_type=request.agent_type,
            workspace_id=request.workspace_id,
            policy=_policy_for(request, self._policy_source, call.tool_name),
        )
        if behavior == "allow":
            return Decision.allow(call.arguments, source=SOURCE_PERMISSION)
        if behavior == "ask":
            return Decision.ask(
                "tool_approval",
                arguments=call.arguments,
                reason="权限策略要求用户审批",
                source=SOURCE_PERMISSION,
            )
        return Decision.deny(
            "权限策略拒绝执行该工具调用。",
            arguments=call.arguments,
            reason="权限策略拒绝执行该工具调用",
            source=SOURCE_PERMISSION,
        )


class NoApprovalChannelRule(ToolCallReviewer):
    """给没有审批通道的调用方（子代理）包一组规则：内层的挂起合并为拒绝，拒绝文本换成子代理文案。

    内层规则给出的内部原因原样保留，用于日志和被拒摘要；needs_approval 标出由挂起合并而来的拒绝。
    """

    def __init__(self, *rules: ToolCallReviewer) -> None:
        self._inner = PolicyReviewer(rules)

    @property
    def rules(self) -> tuple[ToolCallReviewer, ...]:
        return self._inner.rules

    async def review(self, call: ToolCall, request: ReviewRequest) -> Decision:
        decision = await self._inner.review(call, request)
        if decision.behavior == "allow":
            return decision
        if decision.behavior == "ask":
            message = (
                f"error: 工具 {call.tool_name} 需要用户审批,而子代理无审批通道,"
                "本次调用已被拒绝。请改用只读方式完成,或把该操作留给主代理。"
            )
            return Decision.deny(
                message,
                arguments=decision.arguments,
                reason=decision.reason,
                source=decision.source,
                needs_approval=True,
            )
        return Decision.deny(
            f"error: 工具 {call.tool_name} 已被权限策略禁止,本次调用未执行。",
            arguments=decision.arguments,
            reason=decision.reason,
            source=decision.source,
        )
