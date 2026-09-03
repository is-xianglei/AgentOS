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

from llm.factory import resolve_scenario_model
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
    # None 表示走档位场景解析;非空表示该类型固定模型(优先级高于场景档位)。
    model: str | None = None
    # 模型档位场景:"subagent"(默认档,继承客户端模型)或 "subagent_fast"(速度档)。
    # 在 resolve_model() 惰性解析,不在导入期读配置,避免 provider 未配置时崩掉导入。
    model_scenario: str = "subagent"
    # ReAct 轮次上限。verification 需跑构建 + 测试 + 对抗探测,须显著放宽。
    max_rounds: int = 6
    # shell 访问级别,由 ToolService 在执行边界强制,不依赖提示词自律。
    shell_access: ShellAccess = ShellAccess.READ_ONLY
    # 是否跳过注入父会话的 Memory 正文。只读检索不需要历史偏好,注入反而稀释任务。
    omit_inherited_memory: bool = False
    # 核心约束的二次注入,拼在 system_prompt 末尾,抵抗长上下文中的约束遗忘。
    critical_reminder: str = ""
    # 动态构造的 spec(如 workflow 成员)的运行标识;内置 spec 留空,回落枚举值。
    # agent_type 对这类 spec 只表示能力档位,不再充当标识,避免权限规则与事件串名。
    display_name: str = ""

    @property
    def runtime_name(self) -> str:
        """运行标识:落运行记录、发事件、判权限、打日志都用它。
        内置 SubAgent 留空即回落枚举值,行为与改造前完全一致。
        """
        return self.display_name or self.agent_type.value

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
        """按优先级定出本次调用使用的模型,None 表示继承客户端的档位模型。

        优先级:显式入参 > spec 固定声明 > 场景档位解析(仅速度档)。
        默认档返回 None:SubAgentRunner 的客户端已按 subagent 场景解析过模型
        ({provider}_subagent_model → {provider}_model),无需重复解析。
        速度档经工厂按 {provider}_subagent_fast_model → {provider}_model 两层回落,
        在调用时惰性解析而非导入期,避免 provider 未配置时崩掉模块导入。
        .env 里留空的键会被读成空串,一律归一成 None,避免把 "" 当模型名发给 API。
        """
        explicit = _blank_to_none(override) or _blank_to_none(self.model)
        if explicit is not None:
            return explicit
        if self.model_scenario != "subagent":
            return resolve_scenario_model(self.model_scenario)
        return None

    def shell_tmp_root(self) -> Path | None:
        """TMP_WRITABLE 档位的可写临时目录;其余档位无可写目录。"""
        if self.shell_access is not ShellAccess.TMP_WRITABLE:
            return None
        return Path(tempfile.gettempdir()) / f"agentos-{self.runtime_name}"
