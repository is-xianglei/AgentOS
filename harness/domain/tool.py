"""工具领域类型：与来源无关的工具、工具能力策略与工具执行上下文。

``Tool`` 以 JSON Schema（``parameters``）描述参数，自带 ``ToolPolicy``，因此内置工具和
外部来源（如 MCP server 下发的工具）都能被同一套 ToolSet 聚合、按策略裁剪与审核。
内置工具继承 ``PydanticTool``，参数 schema 由 Pydantic 输入模型派生。
"""

from abc import ABC, abstractmethod
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal
from uuid import UUID

from pydantic import BaseModel, ValidationError

from core.errors import AgentException
from harness.domain.context import ToolSpec

if TYPE_CHECKING:
    # 仅作类型标注：领域层加载期不依赖 SQLAlchemy 与事件总线实现。
    from sqlalchemy.ext.asyncio import AsyncSession

    from core.event_bus import StreamBus
    from harness.app.ports import SubAgentLauncher

ToolProfile = Literal[
    "lead",
    "user_agent",
    "workflow_node",
    "subagent",
    "teammate",
    "orchestrator",
    # 默认 Agent 委派给用户自建 Agent 时，被委派方使用的档位。
    "delegate",
]

# 主 Agent 使用工作区级 Skill 目录；其余运行身份必须携带由父运行时裁剪后的
# Skill 白名单。这样即使某个子运行器忘记传入 allowed_skill_names，也不会默认
# 获得整个工作区的 Skill 资产。
_SKILL_ALLOWLIST_PROFILES: frozenset[ToolProfile] = frozenset(
    {"user_agent", "workflow_node", "subagent", "teammate", "orchestrator", "delegate"}
)


def requires_skill_allowlist(profile: ToolProfile) -> bool:
    """判断当前运行身份是否必须显式携带 Skill 白名单。"""
    return profile in _SKILL_ALLOWLIST_PROFILES


class ShellAccess(str, Enum):
    """shell 访问级别。值用于落库与日志,保持稳定。"""

    FULL = "full"
    READ_ONLY = "read_only"
    TMP_WRITABLE = "tmp_writable"


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


# 工具策略的默认档位集合。注意这里故意不含 delegate：新增工具沿用默认值时，
# 不会自动对被委派的自建 Agent 开放，必须在工具自己的 policy 里显式加上。
ALL_PROFILES: frozenset[ToolProfile] = frozenset(
    {
        "lead",
        "user_agent",
        "workflow_node",
        "subagent",
        "teammate",
        "orchestrator",
    }
)
# 默认档位再加上 delegate。delegate 的初始可调范围逐个工具对齐 workflow_node：
# workflow_node 能调用的工具，都改用这个集合声明 callable_profiles。
ALL_PROFILES_AND_DELEGATE: frozenset[ToolProfile] = ALL_PROFILES | {"delegate"}
LEAD_ONLY: frozenset[ToolProfile] = frozenset({"lead", "orchestrator"})
LEAD_AND_TEAM: frozenset[ToolProfile] = frozenset({"lead", "orchestrator", "teammate"})


def tool_policy(
    name: str,
    category: str,
    *,
    bindable_profiles: frozenset[ToolProfile] = ALL_PROFILES,
    callable_profiles: frozenset[ToolProfile] = ALL_PROFILES,
    requires_approval: bool = False,
    requires_bound_skill: bool = False,
) -> ToolPolicy:
    """按默认档位构造工具策略；默认档位不含 delegate，见 ALL_PROFILES。"""
    return ToolPolicy(
        name=name,
        category=category,
        bindable_profiles=bindable_profiles,
        callable_profiles=callable_profiles,
        requires_approval=requires_approval,
        requires_bound_skill=requires_bound_skill,
    )


def unknown_tool_policy(name: str) -> ToolPolicy:
    """按名查不到的工具使用的策略：任何档位都不可绑定、不可调用。"""
    return ToolPolicy(
        name=name,
        category="unknown",
        bindable_profiles=frozenset(),
        callable_profiles=frozenset(),
    )


@dataclass(frozen=True)
class ToolOutput:
    """工具执行结果。is_error 表示执行失败，失败文本仍交给模型自行纠正。"""

    text: str
    is_error: bool = False

    @classmethod
    def ok(cls, text: str) -> ToolOutput:
        """执行成功的结果。"""
        return cls(text=text)

    @classmethod
    def error(cls, text: str) -> ToolOutput:
        """执行失败的结果，文本照样回给模型。"""
        return cls(text=text, is_error=True)


@dataclass
class ToolContext:
    session_id: int
    db: AsyncSession
    bus: StreamBus | None = None
    turn_id: UUID | None = None
    user_id: int | None = None
    workspace_id: int | None = None
    # 工具调用方的可信身份,由 ToolService 根据 actor 注入,不接受模型入参覆盖。
    principal_name: str | None = None
    # 能力档位由运行时显式注入,不从模型输入或显示名称推断。
    profile: ToolProfile | None = None
    allowed_tool_names: frozenset[str] | None = None
    # 调用方本轮实际生效的工具名单(含 Plan Mode 等模式裁剪),由主运行时逐次注入;
    # Agent 工具委派自建 Agent 时据此裁剪被委派方的工具,其余工具不读。
    round_tool_names: frozenset[str] | None = None
    allowed_skill_names: frozenset[str] | None = None
    # shell 访问级别与可写临时目录:由 SubAgent 规格下发,Bash 工具据此自校验。
    # 主代理路径不设置,等价于 FULL。
    shell_access: ShellAccess = ShellAccess.FULL
    shell_tmp_root: Path | None = None
    # 子代理启动器，由运行时经工具网关注入；只有 Agent 工具读取，缺失时 Agent 工具报错而不回落。
    subagents: SubAgentLauncher | None = None


class Tool(ABC):
    """与来源无关的工具：参数以 JSON Schema 描述，自带能力策略。

    ``parameters`` 是发给模型的参数 schema，以它为准；外部来源的工具（如 MCP）
    直接使用对方下发的 schema。``policy`` 决定哪些运行身份可以绑定、调用该工具。
    """

    name: str
    description: str
    parameters: dict[str, Any]
    policy: ToolPolicy

    def to_spec(self) -> ToolSpec:
        """产出中立工具描述；编码成各家 SDK 形状由对应客户端内部完成。"""
        return ToolSpec(
            name=self.name,
            description=self.description,
            input_schema=self.parameters,
        )

    @abstractmethod
    def normalize_arguments(self, arguments: dict[str, Any]) -> dict[str, Any]:
        """按参数 schema 校验并规范化入参，供执行和人工恢复共用；不合法时抛中文异常。"""

    @abstractmethod
    async def execute(self, arguments: dict[str, Any], ctx: ToolContext) -> ToolOutput:
        """执行工具；参数不合法时把错误文本作为结果返回，让模型修正参数后重试。"""


class PydanticTool(Tool):
    """内置工具基类：用 Pydantic 输入模型声明参数，子类实现 ``run``。

    ``parameters`` 由 ``input_model.model_json_schema()`` 派生，校验与规范化也走同一个模型。
    """

    input_model: type[BaseModel]

    @property
    def parameters(self) -> dict[str, Any]:
        return self.input_model.model_json_schema()

    def normalize_arguments(self, arguments: dict[str, Any]) -> dict[str, Any]:
        try:
            value = self.input_model.model_validate(arguments)
        except ValidationError as exc:
            raise AgentException.message(
                "工具参数校验失败",
                {"tool_name": self.name, "errors": exc.errors(include_url=False)},
            ) from exc
        return value.model_dump(by_alias=True, exclude_none=True)

    async def execute(self, arguments: dict[str, Any], ctx: ToolContext) -> ToolOutput:
        # 参数校验失败不抛异常，把错误作为工具结果回给模型，让它修正参数后重试。
        # 内置工具一律包成 ok：is_error 维持原来的 False，模型看到的结果不变。
        try:
            args = self.input_model.model_validate(arguments)
        except ValidationError as err:
            return ToolOutput.ok(f"error: {err}. hint: Please fix the parameters and try again")
        return ToolOutput.ok(await self.run(args, ctx))

    @abstractmethod
    async def run(self, args: BaseModel, ctx: ToolContext) -> str:
        pass
