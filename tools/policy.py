"""工具能力策略。

工具注册表只描述代码中存在的工具；本模块描述不同运行身份可以绑定和调用哪些工具。
第一阶段使用代码策略，后续可将策略同步到工具目录表。
"""

from collections.abc import Iterable
from dataclasses import dataclass
from typing import Literal

ToolProfile = Literal[
    "lead",
    "user_agent",
    "workflow_node",
    "subagent",
    "teammate",
    "orchestrator",
]

# 主 Agent 使用工作区级 Skill 目录；其余运行身份必须携带由父运行时裁剪后的
# Skill 白名单。这样即使某个子运行器忘记传入 allowed_skill_names，也不会默认
# 获得整个工作区的 Skill 资产。
_SKILL_ALLOWLIST_PROFILES: frozenset[ToolProfile] = frozenset(
    {"user_agent", "workflow_node", "subagent", "teammate", "orchestrator"}
)


@dataclass(frozen=True)
class ToolPolicy:
    """单个工具的能力边界。"""

    name: str
    category: str
    bindable_profiles: frozenset[ToolProfile]
    callable_profiles: frozenset[ToolProfile]
    requires_approval: bool = False
    # Skill 工具还需要调用方提供已绑定的 skill 名称集合；仅靠 profile
    # 放行不足以阻止用户 Agent 通过旧配置或直接调用访问未绑定资产。
    requires_bound_skill: bool = False

    def can_bind(self, profile: ToolProfile) -> bool:
        return profile in self.bindable_profiles

    def can_call(self, profile: ToolProfile) -> bool:
        return profile in self.callable_profiles


class ToolCapabilityResolver:
    """根据执行档位解析工具能力。

    ``ToolRegistry`` 只描述代码里存在的工具，本类只处理能力策略，避免把
    注册、绑定和执行权限再次混在一起。解析结果保持输入顺序并去重，方便
    运行时直接交给 ``ToolRegistry.only``。
    """

    def resolve_names(
        self,
        tool_names: Iterable[str],
        profile: ToolProfile,
        *,
        strict: bool = False,
    ) -> tuple[str, ...]:
        resolved: list[str] = []
        denied: list[str] = []
        for name in dict.fromkeys(tool_names):
            if get_tool_policy(name).can_call(profile):
                resolved.append(name)
            else:
                denied.append(name)
        if strict and denied:
            raise ValueError(
                f"工具能力策略拒绝 profile={profile}: {', '.join(denied)}"
            )
        return tuple(resolved)

    def resolve_bindable_names(
        self,
        tool_names: Iterable[str],
        profile: ToolProfile,
        *,
        strict: bool = False,
    ) -> tuple[str, ...]:
        """解析可写入 Agent 配置的工具名。

        ``callable_profiles`` 描述运行时是否可以使用能力；``bindable_profiles``
        描述用户或父节点是否可以把能力写入配置。两者必须分开，否则
        ``SkillRun`` 这类由已授权 Skill 自动注入的工具会被误认为普通用户可绑定。
        """
        resolved: list[str] = []
        denied: list[str] = []
        for name in dict.fromkeys(tool_names):
            if get_tool_policy(name).can_bind(profile):
                resolved.append(name)
            else:
                denied.append(name)
        if strict and denied:
            raise ValueError(
                f"工具绑定策略拒绝 profile={profile}: {', '.join(denied)}"
            )
        return tuple(resolved)

    def denied_names(
        self, tool_names: Iterable[str], profile: ToolProfile
    ) -> tuple[str, ...]:
        return tuple(
            name
            for name in dict.fromkeys(tool_names)
            if not get_tool_policy(name).can_call(profile)
        )


_ALL_PROFILES: frozenset[ToolProfile] = frozenset(
    {
        "lead",
        "user_agent",
        "workflow_node",
        "subagent",
        "teammate",
        "orchestrator",
    }
)
_LEAD_ONLY: frozenset[ToolProfile] = frozenset({"lead", "orchestrator"})
_LEAD_AND_TEAM: frozenset[ToolProfile] = frozenset(
    {"lead", "orchestrator", "teammate"}
)


def _policy(
    name: str,
    category: str,
    *,
    bindable_profiles: frozenset[ToolProfile] = _ALL_PROFILES,
    callable_profiles: frozenset[ToolProfile] = _ALL_PROFILES,
    requires_approval: bool = False,
    requires_bound_skill: bool = False,
) -> ToolPolicy:
    return ToolPolicy(
        name=name,
        category=category,
        bindable_profiles=bindable_profiles,
        callable_profiles=callable_profiles,
        requires_approval=requires_approval,
        requires_bound_skill=requires_bound_skill,
    )


def _build_policies() -> dict[str, ToolPolicy]:
    policies = {
        # 普通业务与只读工具:用户自建 Agent 可以显式绑定。
        name: _policy(name, "leaf", bindable_profiles=_ALL_PROFILES)
        for name in (
            # 工具注册表中的运行时名称区分大小写，EchoTool.name 为小写
            # ``echo``；策略键必须与它完全一致，否则目录同步后的用户
            # Agent 绑定会在解析阶段被误判为未知工具。
            "echo",
            "Read",
            "Glob",
            "Grep",
            "Weather",
            "TaskCreate",
            "TaskGet",
            "TaskUpdate",
            "TaskList",
        )
    }
    policies.update(
        {
            # 有副作用的工具仍需经过 PermissionService 的调用级审批。
            "Write": _policy(
                "Write",
                "mutation",
                bindable_profiles=_ALL_PROFILES,
                requires_approval=True,
            ),
            "Edit": _policy(
                "Edit",
                "mutation",
                bindable_profiles=_ALL_PROFILES,
                requires_approval=True,
            ),
            "Bash": _policy(
                "Bash",
                "mutation",
                # Shell 是高风险能力，普通用户 Agent 必须由更高层 profile
                # 显式授予；旧绑定在运行时也会再次被拒绝。
                bindable_profiles=frozenset(
                    {
                        "lead",
                        "orchestrator",
                        "workflow_node",
                        "subagent",
                        "teammate",
                    }
                ),
                callable_profiles=frozenset(
                    {
                        "lead",
                        "orchestrator",
                        "workflow_node",
                        "subagent",
                        "teammate",
                    }
                ),
                requires_approval=True,
            ),
            # Skill 工具由已绑定 Skill 自动注入；不允许普通 Agent 伪造基础工具绑定。
            "Skill": _policy(
                "Skill",
                "leaf",
                bindable_profiles=frozenset(
                    {"lead", "orchestrator", "workflow_node", "subagent", "teammate"}
                ),
                requires_bound_skill=True,
            ),
            "SkillResource": _policy(
                "SkillResource",
                "leaf",
                bindable_profiles=frozenset(
                    {"lead", "orchestrator", "workflow_node", "subagent", "teammate"}
                ),
                requires_bound_skill=True,
            ),
            "SkillRun": _policy(
                "SkillRun",
                "mutation",
                # 运行时会在用户 Agent 绑定 Skill 后自动补齐 SkillRun；直接
                # 把 SkillRun 填进 tool_ids 则拒绝，避免绕过 Skill 资产边界。
                bindable_profiles=frozenset(
                    {"lead", "orchestrator", "workflow_node", "subagent", "teammate"}
                ),
                callable_profiles=_ALL_PROFILES,
                requires_approval=True,
                requires_bound_skill=True,
            ),
            # 团队消息只允许专门的 teammate 上下文或主编排者使用。
            "TeamList": _policy(
                "TeamList",
                "team_message",
                bindable_profiles=_LEAD_AND_TEAM,
                callable_profiles=_LEAD_AND_TEAM,
            ),
            "SendMessage": _policy(
                "SendMessage",
                "team_message",
                bindable_profiles=_LEAD_AND_TEAM,
                callable_profiles=_LEAD_AND_TEAM,
            ),
            "ReadInbox": _policy(
                "ReadInbox",
                "team_message",
                bindable_profiles=_LEAD_AND_TEAM,
                callable_profiles=_LEAD_AND_TEAM,
            ),
            "ListMessages": _policy(
                "ListMessages",
                "team_message",
                bindable_profiles=_LEAD_AND_TEAM,
                callable_profiles=_LEAD_AND_TEAM,
            ),
            # 以下是控制平面能力，只属于主 Agent/Orchestrator。
            "AskUserQuestion": _policy(
                "AskUserQuestion",
                "interaction",
                bindable_profiles=_LEAD_ONLY,
                callable_profiles=_LEAD_ONLY,
            ),
            "EnterPlanMode": _policy(
                "EnterPlanMode",
                "interaction",
                bindable_profiles=_LEAD_ONLY,
                callable_profiles=_LEAD_ONLY,
            ),
            "ExitPlanMode": _policy(
                "ExitPlanMode",
                "interaction",
                bindable_profiles=_LEAD_ONLY,
                callable_profiles=_LEAD_ONLY,
            ),
            "WritePlan": _policy(
                "WritePlan",
                "interaction",
                bindable_profiles=_LEAD_ONLY,
                callable_profiles=_LEAD_ONLY,
            ),
            "Agent": _policy(
                "Agent",
                "orchestration",
                bindable_profiles=_LEAD_ONLY,
                callable_profiles=_LEAD_ONLY,
            ),
            "TeamCreate": _policy(
                "TeamCreate",
                "orchestration",
                bindable_profiles=_LEAD_ONLY,
                callable_profiles=_LEAD_ONLY,
            ),
            "TeamSpawn": _policy(
                "TeamSpawn",
                "orchestration",
                bindable_profiles=_LEAD_ONLY,
                callable_profiles=_LEAD_ONLY,
            ),
        }
    )
    return policies


TOOL_POLICIES: dict[str, ToolPolicy] = _build_policies()


def get_tool_policy(tool_name: str) -> ToolPolicy:
    """返回工具策略；未知工具默认不允许用户 Agent 绑定。"""
    return TOOL_POLICIES.get(
        tool_name,
        ToolPolicy(
            name=tool_name,
            category="unknown",
            bindable_profiles=frozenset(),
            callable_profiles=frozenset(),
        ),
    )


def can_bind_tool(tool_name: str, profile: ToolProfile) -> bool:
    return get_tool_policy(tool_name).can_bind(profile)


def can_call_tool(tool_name: str, profile: ToolProfile) -> bool:
    return get_tool_policy(tool_name).can_call(profile)


def requires_skill_allowlist(profile: ToolProfile) -> bool:
    """判断当前运行身份是否必须显式携带 Skill 白名单。"""
    return profile in _SKILL_ALLOWLIST_PROFILES

