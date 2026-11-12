"""子代理角色：子代理规格 SubAgentSpec，以及把规格与继承的记忆组装成 Context 的 SubAgent。

主代理通过 Agent 工具按 agent_type 分派到不同子代理，每种子代理有自己的系统提示、工具权限、
模型档位、轮次上限与 shell 访问级别。SubAgentSpec 是这套配置的类型化载体：字段即全部能力，
不做类继承，也没有独立执行器；内置的四种规格要读配置，由应用层 harness/app/subagent_catalog.py 构造。

agent_type 取值与参考实现严格一致（含大小写与连字符），便于跨系统对照与主代理路由：
general-purpose / Explore / Plan / verification。
"""

from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Protocol, Self

from harness.domain.context import Context, ToolSpec
from harness.domain.messages import Message, UserMessage
from harness.domain.prompts.memory import MEMORY_TRUST_RULES
from harness.domain.prompts.teammate import IDENTITY_BLOCK_PREFIX
from harness.domain.tool import ScopedGrant, ShellAccess, ToolProfile


class AgentType(str, Enum):
    # docstring 会作为 $defs.AgentType.description 进 Agent 工具的 JSON Schema，逐字保持原样。
    """内置 SubAgent 类型。值与 Agent 工具入参一致,大小写敏感。"""

    GENERAL_PURPOSE = "general-purpose"
    EXPLORE = "Explore"
    PLAN = "Plan"
    VERIFICATION = "verification"


# 全局子代理禁用规则：任何子代理都不得再派生子代理或组建团队，
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


class ToolSelection(Protocol):
    """按工具名裁剪的工具集合；应用层的 ToolSet 天然满足。"""

    def only(self, *names: str) -> Self: ...

    def without(self, *names: str) -> Self: ...


@dataclass(frozen=True)
class SubAgentSpec:
    """单个子代理的运行规格：人格、工具权限、模型、轮次与 shell 访问级别。

    allowed_tools 为 None 表示「除黑名单外全部可用」；为元组时表示白名单。
    黑名单优先于白名单，且全局禁用规则先于两者生效。
    """

    agent_type: AgentType
    system_prompt: str
    # 给主代理看的路由依据，独立于 system_prompt，会进 Agent 工具描述。
    when_to_use: str = ""
    allowed_tools: tuple[str, ...] | None = None
    disallowed_tools: tuple[str, ...] = ()
    # None 表示走档位场景解析；非空表示该类型固定模型（优先级高于场景档位）。
    model: str | None = None
    # 模型档位场景："subagent"（默认档，继承客户端模型）或 "subagent_fast"（速度档）。
    # 按场景解析模型名要读配置，由适配层 resolve_subagent_model() 在调用时完成。
    model_scenario: str = "subagent"
    # ReAct 轮次上限。verification 需跑构建 + 测试 + 对抗探测，须显著放宽。
    max_rounds: int = 6
    # shell 访问级别，由 ToolService 在执行边界强制，不依赖提示词自律。
    shell_access: ShellAccess = ShellAccess.READ_ONLY
    # 是否跳过注入父会话的 Memory 正文。只读检索不需要历史偏好，注入反而稀释任务。
    omit_inherited_memory: bool = False
    # 核心约束的二次注入，拼在 system_prompt 末尾，抵抗长上下文中的约束遗忘。
    critical_reminder: str = ""
    # 动态构造的 spec（如 workflow 成员）的运行标识；内置 spec 留空，回落枚举值。
    # agent_type 对这类 spec 只表示能力档位，不再充当标识，避免权限规则与事件串名。
    display_name: str = ""
    # 数据库 Agent 主键；动态 spec（workflow 成员、被委派的自建 Agent）传入，供事件与运行记录
    # 归到具体 Agent。内置 spec 留空，事件形状与运行记录保持原样。
    agent_id: int | None = None
    # 工具裁决档位：数据库 Agent 构造的 spec 显式声明 workflow_node 或 delegate。
    # 留空时沿用旧规则：带 agent_id 视为 workflow_node，否则为 subagent，见 SubAgentRunner.run。
    tool_profile: ToolProfile | None = None

    @property
    def runtime_name(self) -> str:
        """运行标识：落运行记录、发事件、判权限、打日志都用它。
        内置子代理留空即回落枚举值，行为与改造前完全一致。
        """
        return self.display_name or self.agent_type.value

    def effective_denied_tools(self) -> tuple[str, ...]:
        """合并全局禁用规则与自身黑名单。"""
        merged = dict.fromkeys(GLOBAL_SUBAGENT_DENIED_TOOLS + self.disallowed_tools)
        return tuple(merged)

    def resolve_tools[T: ToolSelection](self, registry: T) -> T:
        """基于全量工具集，产出该子代理实际可用的工具集。

        最终集合是「全局工具集 ∩ 自身白名单 - 全局禁用 - 自身黑名单」，
        permission 裁决在执行时另行进行，二者是叠加而非替代关系。
        """
        denied = self.effective_denied_tools()
        if self.allowed_tools is not None:
            allowed = set(self.allowed_tools) - set(denied)
            return registry.only(*allowed)
        return registry.without(*denied)

    def inherit_scoped(
        self,
        grant: ScopedGrant | None,
        parent_names: Iterable[str] | None,
    ) -> ScopedGrant | None:
        """子代理、队友从父执行继承作用域工具（如 MCP）：没有工具白名单（general-purpose）时
        继承父执行可见的，有白名单（Explore、Plan、verification）时一个都不可见。

        全部作用域工具仍随 grant 下传，子执行据此查策略，审核结论与被裁掉的内置工具一致；
        parent_names 是父执行本轮生效的工具名单（含 Plan Mode 裁剪），None 表示不再按它收窄。
        """
        if grant is None:
            return None
        if self.allowed_tools is not None:
            return grant.narrowed(())
        if parent_names is None:
            return grant
        return grant.narrowed(parent_names)

    def resolve_tools_names(self) -> tuple[str, ...]:
        """白名单模式下的实际工具名，用于给主代理展示能力范围。"""
        if self.allowed_tools is None:
            return ()
        denied = set(self.effective_denied_tools())
        return tuple(name for name in self.allowed_tools if name not in denied)

    def shell_tmp_root(self, temp_dir: Path) -> Path | None:
        """TMP_WRITABLE 档位的可写临时目录；其余档位无可写目录。

        temp_dir 是系统临时目录，由调用方读取进程状态后传入。
        """
        if self.shell_access is not ShellAccess.TMP_WRITABLE:
            return None
        return temp_dir / f"agentos-{self.runtime_name}"


def prepend_memory_prefix(
    messages: Sequence[Message], memory_prefix: str | None
) -> tuple[Message, ...]:
    """把继承的记忆正文拼到第一条普通用户消息前面，跳过队友历史首条的身份块。

    只作用于本次请求的消息副本，不改写调用方持有的历史（队友历史会原样落库）。
    """
    if not memory_prefix:
        return tuple(messages)
    result = list(messages)
    for index, message in enumerate(result):
        if isinstance(message, UserMessage) and not message.text.startswith(
            IDENTITY_BLOCK_PREFIX
        ):
            result[index] = UserMessage(text=f"{memory_prefix}\n\n{message.text}")
            break
    return tuple(result)


def with_memory_rules(base_prompt: str, memory_prefix: str | None) -> str:
    """继承了记忆正文时，在系统提示末尾声明 Memory 的不可信历史参考边界。"""
    if not memory_prefix:
        return base_prompt
    return f"{base_prompt.strip()}\n\n{MEMORY_TRUST_RULES}"


@dataclass(frozen=True)
class SubAgent:
    """一次性子代理角色：规格 + 父回合继承的记忆正文。

    memory_prefix 为 None 表示不注入记忆（规格声明不继承，或父回合没有召回结果）；
    父回合的记忆目录不进子代理。
    """

    spec: SubAgentSpec
    memory_prefix: str | None = None

    def instruction(self) -> str:
        """系统提示：规格提示词，继承记忆时追加 Memory 规则，最后是关键约束的二次注入。"""
        prompt = with_memory_rules(self.spec.system_prompt, self.memory_prefix)
        if self.spec.critical_reminder:
            # 关键约束放在系统提示末尾：近端位置比开头更不容易被长上下文冲淡。
            prompt = f"{prompt}\n\n{self.spec.critical_reminder}"
        return prompt

    def context(self, messages: Sequence[Message], tools: Sequence[ToolSpec]) -> Context[None]:
        """本轮调用的 Context：记忆正文只拼进请求副本里的第一条用户消息。"""
        return Context(
            instruction=self.instruction(),
            messages=prepend_memory_prefix(messages, self.memory_prefix),
            tools=tuple(tools),
        )


def render_routing_catalog(specs: Iterable[SubAgentSpec]) -> str:
    """给主代理的路由说明：每种类型的用途与实际工具范围，进 Agent 工具描述。"""
    lines: list[str] = []
    for spec in specs:
        if spec.allowed_tools is None:
            tools = "除全局禁用外全部工具"
        else:
            tools = "、".join(spec.resolve_tools_names())
        lines.append(f"- {spec.agent_type.value}: {spec.when_to_use} 可用工具: {tools}。")
    return "\n".join(lines)
