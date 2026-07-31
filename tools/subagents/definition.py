"""SubAgent 类型与规格定义。

主代理通过 Agent 工具按 agent_type 分派到不同 SubAgent,每种 SubAgent 有自己的
system_prompt、工具权限、模型档位、轮次上限与 shell 访问级别。SubAgentSpec 是
这套配置的类型化载体:字段即全部能力,不做类继承,也没有独立执行器。

agent_type 取值与参考实现严格一致(含大小写与连字符),便于跨系统对照与
主代理路由:general-purpose / Explore / Plan / verification。
"""

from __future__ import annotations

import tempfile
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import TYPE_CHECKING

from core.config import settings
from tools.shell_policy import ShellAccess

if TYPE_CHECKING:
    from tools.registry import ToolRegistry


def _blank_to_none(value: str | None) -> str | None:
    """把 None / 空串 / 纯空白统一归一成 None。"""
    if value is None:
        return None
    stripped = value.strip()
    return stripped or None


class AgentType(str, Enum):
    """内置 SubAgent 类型。值与 Agent 工具入参一致,大小写敏感。"""

    GENERAL_PURPOSE = "general-purpose"
    EXPLORE = "Explore"
    PLAN = "Plan"
    VERIFICATION = "verification"


# 全局 SubAgent 禁用规则:任何 SubAgent 都不得再派生 SubAgent 或组建团队,
# 防止递归 spawn 导致资源失控。这一条先于各 spec 自身的声明生效。
GLOBAL_SUBAGENT_DENIED_TOOLS: tuple[str, ...] = (
    "Agent",
    "TeamCreate",
    "TeamSpawn",
    "AskUserQuestion",
    "EnterPlanMode",
    "ExitPlanMode",
    "WritePlan",
)


@dataclass(frozen=True)
class SubAgentSpec:
    """单个 SubAgent 的运行规格:人格、工具权限、模型、轮次与 shell 访问级别。

    allowed_tools 为 None 表示「除黑名单外全部可用」;为元组时表示白名单。
    黑名单优先于白名单,且全局禁用规则先于两者生效。
    """

    agent_type: AgentType
    system_prompt: str
    # 给主代理看的路由依据,独立于 system_prompt,会进 Agent 工具描述。
    when_to_use: str = ""
    allowed_tools: tuple[str, ...] | None = None
    disallowed_tools: tuple[str, ...] = ()
    # None 表示继承主会话模型;非空表示该类型固定档位(如速度优先)。
    model: str | None = None
    # ReAct 轮次上限。verification 需跑构建 + 测试 + 对抗探测,须显著放宽。
    max_rounds: int = 6
    # shell 访问级别,由 ToolService 在执行边界强制,不依赖提示词自律。
    shell_access: ShellAccess = ShellAccess.READ_ONLY
    # 是否跳过注入父会话的 Memory 正文。只读检索不需要历史偏好,注入反而稀释任务。
    omit_inherited_memory: bool = False
    # 核心约束的二次注入,拼在 system_prompt 末尾,抵抗长上下文中的约束遗忘。
    critical_reminder: str = ""

    def effective_denied_tools(self) -> tuple[str, ...]:
        """合并全局禁用规则与自身黑名单。"""
        merged = dict.fromkeys(GLOBAL_SUBAGENT_DENIED_TOOLS + self.disallowed_tools)
        return tuple(merged)

    def resolve_tools(self, registry: ToolRegistry) -> ToolRegistry:
        """基于全量工具注册表,产出该 SubAgent 实际可用的工具集。

        最终集合是「全局工具集 ∩ 自身白名单 - 全局禁用 - 自身黑名单」,
        permission 裁决在执行时另行进行,二者是叠加而非替代关系。
        """
        denied = self.effective_denied_tools()
        if self.allowed_tools is not None:
            allowed = set(self.allowed_tools) - set(denied)
            return registry.only(*allowed)
        return registry.without(*denied)

    def resolve_tools_names(self) -> tuple[str, ...]:
        """白名单模式下的实际工具名,用于给主代理展示能力范围。"""
        if self.allowed_tools is None:
            return ()
        denied = set(self.effective_denied_tools())
        return tuple(name for name in self.allowed_tools if name not in denied)

    def resolve_model(self, override: str | None = None) -> str | None:
        """按优先级定出本次调用使用的模型,None 表示继承 LLMClient 的会话模型。

        优先级:显式入参 > spec 声明 > 环境兜底(subagent_default_model)> 继承。
        环境键取名 default 就按兜底处理,放最低优先级:否则一配上去会把 Explore
        的速度档位一并顶掉,那与"默认值"的语义相反。要固定某类型改其 spec 或传 override。
        .env 里留空的键会被读成空串,一律归一成 None,避免把 "" 当模型名发给 API。
        """
        candidates = (
            _blank_to_none(override),
            _blank_to_none(self.model),
            _blank_to_none(settings.subagent_default_model),
        )
        return next((value for value in candidates if value is not None), None)

    def shell_tmp_root(self) -> Path | None:
        """TMP_WRITABLE 档位的可写临时目录;其余档位无可写目录。"""
        if self.shell_access is not ShellAccess.TMP_WRITABLE:
            return None
        return Path(tempfile.gettempdir()) / f"agentos-{self.agent_type.value}"
