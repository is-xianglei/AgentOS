"""统一工具策略门面。

``ToolPolicy`` 负责静态能力边界，``PermissionService`` 负责工作区和会话
级别的动态 allow/ask/deny。两者有不同的职责，但运行时不应该到处自行组合，
因此由本模块提供唯一的决策入口。
"""

from dataclasses import dataclass
from typing import Any

from permission.service import Behavior, PermissionService
from tools.policy import (
    ToolCapabilityResolver,
    ToolProfile,
    get_tool_policy,
    requires_skill_allowlist,
)


@dataclass(frozen=True)
class ToolAccessDecision:
    """一次工具访问的完整决策。

    ``allowed`` 表示最终可以立即执行；``behavior`` 保留动态权限层的三态，
    方便运行时把 ``ask`` 转成挂起，把 ``deny`` 转成 tool_result 错误。
    """

    tool_name: str
    profile: ToolProfile
    allowed: bool
    behavior: Behavior
    requires_approval: bool = False
    reason: str | None = None


class PolicyEngine:
    """静态 ToolPolicy 与动态 PermissionService 的统一外观。"""

    def __init__(self, db: Any):
        self.permission_service = PermissionService(db)
        self.resolver = ToolCapabilityResolver()

    def resolve_bindable_names(
        self,
        tool_names: list[str] | tuple[str, ...] | frozenset[str],
        profile: ToolProfile,
        *,
        strict: bool = False,
    ) -> tuple[str, ...]:
        return self.resolver.resolve_bindable_names(tool_names, profile, strict=strict)

    def resolve_callable_names(
        self,
        tool_names: list[str] | tuple[str, ...] | frozenset[str],
        profile: ToolProfile,
        *,
        strict: bool = False,
    ) -> tuple[str, ...]:
        return self.resolver.resolve_names(tool_names, profile, strict=strict)

    def admit_tool(
        self,
        tool_name: str,
        profile: ToolProfile,
        *,
        input_args: dict[str, Any] | None = None,
        allowed_skill_names: frozenset[str] | None = None,
    ) -> ToolAccessDecision:
        """执行动态权限前的静态能力检查。

        该检查不访问数据库，因此可在工具目录裁剪、模型 tool_use 校验和最终
        ToolService 执行前重复调用。重复调用是有意的防御式边界，而不是新的
        权限来源。
        """
        policy = get_tool_policy(tool_name)
        if not policy.can_call(profile):
            return ToolAccessDecision(
                tool_name,
                profile,
                False,
                "deny",
                reason=f"工具能力策略拒绝执行: tool={tool_name}, profile={profile}",
            )

        if policy.requires_bound_skill and requires_skill_allowlist(profile):
            args = input_args or {}
            arg_name = "name" if tool_name == "Skill" else "skill_name"
            requested_skill = args.get(arg_name)
            if (
                allowed_skill_names is None
                or not isinstance(requested_skill, str)
                or requested_skill not in allowed_skill_names
            ):
                return ToolAccessDecision(
                    tool_name,
                    profile,
                    False,
                    "deny",
                    reason=(
                        "工具能力策略拒绝执行: 当前运行身份只能访问已绑定的 skill "
                        f"(skill={requested_skill!r})"
                    ),
                )

        return ToolAccessDecision(tool_name, profile, True, "allow")

    async def authorize(
        self,
        session_id: int,
        tool_name: str,
        profile: ToolProfile,
        *,
        input_args: dict[str, Any] | None = None,
        allowed_skill_names: frozenset[str] | None = None,
        agent_type: str | None = None,
        workspace_id: int | None = None,
    ) -> ToolAccessDecision:
        """完成静态能力、Skill 资产和动态权限三层判定。"""
        static = self.admit_tool(
            tool_name,
            profile,
            input_args=input_args,
            allowed_skill_names=allowed_skill_names,
        )
        if not static.allowed:
            return static
        behavior = await self.permission_service.evaluate(
            session_id,
            tool_name,
            input_args,
            agent_type=agent_type or profile,
            workspace_id=workspace_id,
        )
        if behavior == "allow":
            return ToolAccessDecision(tool_name, profile, True, behavior)
        if behavior == "ask":
            return ToolAccessDecision(
                tool_name,
                profile,
                False,
                behavior,
                requires_approval=True,
                reason="权限策略要求用户审批",
            )
        return ToolAccessDecision(
            tool_name,
            profile,
            False,
            behavior,
            reason="权限策略拒绝执行该工具调用",
        )

    async def admit_call(
        self,
        session_id: int,
        tool_name: str,
        profile: ToolProfile,
        *,
        input_args: dict[str, Any] | None = None,
        allowed_skill_names: frozenset[str] | None = None,
        agent_type: str | None = None,
        workspace_id: int | None = None,
    ) -> ToolAccessDecision:
        """``authorize`` 的语义别名，便于外部执行适配器使用统一命名。"""
        return await self.authorize(
            session_id,
            tool_name,
            profile,
            input_args=input_args,
            allowed_skill_names=allowed_skill_names,
            agent_type=agent_type,
            workspace_id=workspace_id,
        )
