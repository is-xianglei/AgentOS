"""SubAgent 类型与规格定义。

主代理通过 Agent 工具按 agent_type 分派到不同 SubAgent,每种 SubAgent 有
自己的 system_prompt 和工具权限。SubAgentSpec 是这套配置的类型化载体。
"""
from __future__ import annotations
from dataclasses import dataclass
from enum import Enum
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from app.tools.registry import ToolRegistry


class AgentType(str, Enum):
    """内置 SubAgent 类型。值与 Agent 工具入参一致(小写)。"""

    GENERAL_PURPOSE = "general_purpose"
    EXPLORE = "explore"
    PLAN = "plan"
    VERIFICATION = "verification"


@dataclass(frozen=True)
class SubAgentSpec:
    """单个 SubAgent 的运行规格:人格(system_prompt)+ 工具权限。

    allowed_tools 为 None 表示「除黑名单外全部可用」;为元组时表示白名单。
    disallowed_tools 是始终剔除的工具,默认禁止子代理再创建子代理(Agent)、
    组建团队 / 派生成员(TeamCreate / TeamSpawn)、跑 shell(Bash)。
    注意:通信类工具(SendMessage / ReadInbox / ListMessages / TeamList)不在黑名单内,
    因此 teammate 默认可以收发消息、查看团队,只是不能造人或越权。
    """

    agent_type: AgentType
    system_prompt: str
    allowed_tools: tuple[str, ...] | None = None
    disallowed_tools: tuple[str, ...] = ("Agent", "TeamCreate", "TeamSpawn", "Bash")

    def resolve_tools(self, registry: ToolRegistry) -> ToolRegistry:
        """基于全量工具注册表,产出该 SubAgent 实际可用的工具集。"""
        if self.allowed_tools is not None:
            allowed = set(self.allowed_tools) - set(self.disallowed_tools)
            return registry.only(*allowed)
        return registry.without(*self.disallowed_tools)
