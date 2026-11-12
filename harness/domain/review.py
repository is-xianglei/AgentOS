"""工具调用审核：审核结论 Decision、一次审核的上下文 ReviewRequest，以及静态能力检查。

审核员（应用层端口 ToolCallReviewer）对模型发起的每个工具调用给出 allow / ask / deny 三态结论，
规则怎么组合由适配层决定。check_capability 是纯函数，审核规则和工具执行层的兜底检查共用它。
"""

from dataclasses import dataclass
from typing import Any, Literal, Protocol, get_args

from core.errors import AgentException
from harness.domain.budget import ToolCallBudget
from harness.domain.tool import ToolPolicy, ToolProfile, requires_skill_allowlist

Behavior = Literal["allow", "ask", "deny"]

# ask 时需要的人工交互类型，取值与 interaction_requests.kind 一致。
InteractionKind = Literal["tool_approval", "user_question", "enter_plan_mode", "exit_plan_mode"]
_INTERACTION_KINDS: frozenset[str] = frozenset(get_args(InteractionKind))

# Plan 模式下仍可调用的工具：只读探索、规划访谈、写计划和请求退出。
# 运行时按它裁剪本轮可见工具，审核规则按它拒绝其余工具，两处必须一致。
PLAN_MODE_ALLOWED_TOOLS: frozenset[str] = frozenset(
    {
        "Read",
        "Glob",
        "Grep",
        "Agent",
        "Skill",
        "SkillResource",
        "AskUserQuestion",
        "WritePlan",
        "ExitPlanMode",
    }
)

# 内置规则链写进 Decision.source 的来源名；其他审核员（如 LLM 审核员）使用自己的来源名。
SOURCE_BUDGET = "budget"
SOURCE_PLAN_MODE_TOOLS = "plan_mode_tools"
SOURCE_ARGUMENTS = "arguments"
SOURCE_CAPABILITY = "capability"
SOURCE_PLAN_MODE_AGENT = "plan_mode_agent"
SOURCE_INTERACTION = "interaction"
SOURCE_PERMISSION = "permission"


@dataclass(frozen=True)
class Decision:
    """一次工具调用的审核结论，经 allow / deny / ask 三个工厂方法构造。

    arguments 是审核后的入参：入参规范化之后判定的规则拿到的是规范化后的入参，放行时按它执行，
    拒绝和挂起时也按它写工具结果与续跑状态。message 是拒绝时回给模型的工具结果文本；
    reason 是内部原因，用于日志和被拒摘要，两者不一定相同；不适用时为空串。
    interaction 是挂起时的交互类型。source 是作出判定的规则。
    needs_approval 只出现在拒绝结论上：规则要求人工审批，但调用方没有审批通道，于是合并为拒绝。
    """

    behavior: Behavior
    arguments: dict[str, Any]
    source: str
    message: str = ""
    reason: str = ""
    interaction: InteractionKind | None = None
    needs_approval: bool = False

    def __post_init__(self) -> None:
        if not self.source:
            raise AgentException.message("审核结论缺少判定来源")
        if self.behavior == "allow":
            if self.message or self.reason or self.interaction is not None or self.needs_approval:
                raise AgentException.message("放行结论不能携带拒绝文本、原因或交互类型")
        elif self.behavior == "deny":
            if not self.message or not self.reason:
                raise AgentException.message("拒绝结论必须同时给出回给模型的文本和内部原因")
            if self.interaction is not None:
                raise AgentException.message("拒绝结论不能携带交互类型")
        elif self.behavior == "ask":
            if self.interaction not in _INTERACTION_KINDS:
                raise AgentException.message(
                    "挂起结论的交互类型未知", {"interaction": self.interaction}
                )
            if not self.reason:
                raise AgentException.message("挂起结论缺少内部原因")
            if self.message or self.needs_approval:
                raise AgentException.message("挂起结论不能携带拒绝文本")
        else:
            raise AgentException.message("未知的审核结论", {"behavior": self.behavior})

    @classmethod
    def allow(cls, arguments: dict[str, Any], *, source: str) -> Decision:
        """放行，按 arguments 执行。"""
        return cls(behavior="allow", arguments=arguments, source=source)

    @classmethod
    def deny(
        cls,
        message: str,
        *,
        arguments: dict[str, Any],
        source: str,
        reason: str = "",
        needs_approval: bool = False,
    ) -> Decision:
        """拒绝，message 作为错误工具结果回给模型；reason 缺省时与 message 相同。"""
        if not reason:
            reason = message
        return cls(
            behavior="deny",
            arguments=arguments,
            source=source,
            message=message,
            reason=reason,
            needs_approval=needs_approval,
        )

    @classmethod
    def ask(
        cls,
        interaction: InteractionKind,
        *,
        arguments: dict[str, Any],
        reason: str,
        source: str,
    ) -> Decision:
        """需要人工交互：本轮挂起，等用户响应后恢复。"""
        return cls(
            behavior="ask",
            arguments=arguments,
            source=source,
            reason=reason,
            interaction=interaction,
        )

    def require_interaction(self) -> InteractionKind:
        """取挂起结论的交互类型；其他结论没有交互类型，调用属编码错误。"""
        if self.interaction is None:
            raise AgentException.message("只有挂起结论才有交互类型", {"behavior": self.behavior})
        return self.interaction


# 交互工具与它们挂起时的交互类型：主 Agent 遇到它们时挂起，等用户响应后恢复。
INTERACTION_TOOL_KINDS: dict[str, InteractionKind] = {
    "AskUserQuestion": "user_question",
    "EnterPlanMode": "enter_plan_mode",
    "ExitPlanMode": "exit_plan_mode",
}


class ToolInputValidator(Protocol):
    """按工具名校验并规范化入参；工具不存在或入参不合法时抛中文 AgentException。ToolSet 天然满足。"""

    def validate_input(self, name: str, data: dict[str, Any]) -> dict[str, Any]: ...


class ToolPolicyLookup(Protocol):
    """按工具名查能力策略；查不到时返回不可调用的未知工具策略。ToolSet 天然满足。"""

    def policy_for(self, name: str) -> ToolPolicy: ...


@dataclass(frozen=True)
class ReviewRequest:
    """审核一个工具调用所需的上下文，只含领域值。

    agent_type 是权限规则匹配定向规则用的身份：主 Agent 用档位，子代理用运行标识。
    budget 是本次运行共享的工具调用额度，审核会从中扣减。plan_content 为 None 表示不在 Plan 模式；
    调用方在每个工具审核前重新读取，因为同一批里前面的 Enter/ExitPlanMode 恢复后会改变它。
    tools 是本轮可见的工具集，规范化入参的规则用它；不规范化入参的规则链可以不传。
    policies 是本回合按名查策略的策略全集（不按可见集裁剪）；为 None 时审核员用构造时注入的平台工具集。
    """

    session_id: int
    profile: ToolProfile
    agent_type: str
    budget: ToolCallBudget
    workspace_id: int | None = None
    allowed_skill_names: frozenset[str] | None = None
    plan_content: str | None = None
    tools: ToolInputValidator | None = None
    policies: ToolPolicyLookup | None = None


def check_capability(
    tool_name: str,
    policy: ToolPolicy,
    profile: ToolProfile,
    arguments: dict[str, Any],
    allowed_skill_names: frozenset[str] | None,
) -> str | None:
    """静态能力检查：档位能否调用该工具，以及 Skill 类工具是否只访问已绑定的 skill。

    返回拒绝原因，放行时返回 None。不访问数据库，审核规则和工具执行层的兜底检查共用它。
    """
    if not policy.can_call(profile):
        return f"工具能力策略拒绝执行: tool={tool_name}, profile={profile}"
    if policy.requires_bound_skill and requires_skill_allowlist(profile):
        # Skill 的参数名是 name，SkillResource / SkillRun 用 skill_name。
        # lead 使用工作区级目录；其他运行身份必须由父运行时显式下发白名单。
        arg_name = "name" if tool_name == "Skill" else "skill_name"
        requested_skill = arguments.get(arg_name)
        if (
            allowed_skill_names is None
            or not isinstance(requested_skill, str)
            or requested_skill not in allowed_skill_names
        ):
            return (
                "工具能力策略拒绝执行: 当前运行身份只能访问已绑定的 skill "
                f"(skill={requested_skill!r})"
            )
    return None
