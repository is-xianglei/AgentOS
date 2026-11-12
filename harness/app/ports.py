"""应用层端口：模型客户端、调用参数、模型解析器、工具来源（进程级与作用域）、工具调用审核员、
子代理启动器，以及中立流事件与调用异常。

全部类型不含任何 provider 的 wire 形状；各家 SDK 的适配在 harness.adapters.models 内部完成。
"""

from abc import ABC, abstractmethod
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from enum import Enum, StrEnum
from typing import TYPE_CHECKING, Any, Literal, Protocol
from uuid import UUID

from pydantic import BaseModel

from core.errors import AgentException
from core.events import BlockType, ToolInfo, Usage
from harness.domain.context import Context
from harness.domain.messages import ThinkingBlock, ToolCall
from harness.domain.review import Decision, ReviewRequest
from harness.domain.roles.subagent import SubAgentSpec
from harness.domain.tool import ScopedGrant, Tool, ToolScope

if TYPE_CHECKING:
    # 仅作类型标注：事件总线的实现依赖 Redis 客户端，端口模块加载期不引入。
    from core.event_bus import StreamBus


class ModelEventKind(str, Enum):
    """中立流事件种类。刻意与 core.events.EventType 分开:
    前者是 provider 出口,后者是对外协议,二者不应互相绑定演进。"""

    BLOCK_START = "block_start"  # 内容块开始
    TEXT_DELTA = "text_delta"  # 文本增量
    THINKING_DELTA = "thinking_delta"  # 思考增量
    TOOL_USE = "tool_use"  # 工具调用(入参已解析完整)
    BLOCK_STOP = "block_stop"  # 内容块结束
    TURN_RESULT = "turn_result"  # 本次调用终局(流的最后一个事件)


@dataclass(frozen=True)
class ModelTurnResult:
    """单次 LLM 调用的终局信息,取代原先 runtime 自行解析 message_final。"""

    text: str = ""
    tool_calls: list[ToolCall] = field(default_factory=list)
    thinking: list[ThinkingBlock] = field(default_factory=list)
    usage: Usage | None = None
    stop_reason: str | None = None


@dataclass(frozen=True)
class ModelStreamEvent:
    """provider 适配后的中立流事件。

    不含 actor / session_id / 全局 index —— 那是 runtime 的装配职责。
    block_ref 是**单次 stream() 调用内**的块序号,由 provider 自行分配、
    从 0 递增即可;跨调用的全局唯一性由 TurnEventAssembler 负责。

    契约:每次 stream() 必须以恰好一个 TURN_RESULT 事件收尾且 result 非空;
    提前耗尽视为 provider 实现错误,由消费方抛中文 AgentException。
    """

    kind: ModelEventKind
    block_ref: int | None = None
    block_type: BlockType | None = None  # 仅 BLOCK_START
    text: str = ""  # 仅 TEXT_DELTA / THINKING_DELTA
    tool: ToolInfo | None = None  # 仅 TOOL_USE
    result: ModelTurnResult | None = None  # 仅 TURN_RESULT


# LLM 调用的中立异常:各家 SDK 异常在客户端内部转换成这里的类型,调用方不认供应商异常。
#
# 只表达调用方需要区分的信息:传输不可用、上游 HTTP 状态码、其余 SDK 失败。
# 不继承 AgentException:后者面向 API 响应,而这里的消息来自上游 SDK,只用于服务端排障。
# SDK 自带的重试用完后才会转换,调用方不应再假设异常来自哪家 SDK。


class ModelCallError(Exception):
    """LLM SDK 调用失败(重试用完之后);__cause__ 保留原始 SDK 异常。

    消息沿用原始 SDK 异常的 str(),保证已有日志和事件文本不因转换而改变。
    status_code 只用于诊断展示:响应校验失败这类非状态错误也可能带状态码,
    是否按 HTTP 状态处理必须看是否为 ModelStatusError,不能看该字段是否为空。
    """

    def __init__(
        self,
        message: str,
        *,
        sdk_error_type: str,
        status_code: int | None = None,
    ) -> None:
        super().__init__(message)
        # 原始 SDK 异常类名(如 APITimeoutError),供 Job 记录和日志排障。
        self.sdk_error_type = sdk_error_type
        self.status_code = status_code


class ModelConnectionError(ModelCallError):
    """未拿到上游响应:连接失败或请求超时。"""


class ModelStatusError(ModelCallError):
    """上游返回了错误状态码;流式中途下发的错误事件也归此类,状态码沿用 SDK 给出的值。"""

    status_code: int

    def __init__(self, message: str, *, sdk_error_type: str, status_code: int) -> None:
        super().__init__(message, sdk_error_type=sdk_error_type, status_code=status_code)


def error_type_name(exc: BaseException) -> str:
    """返回用于排障记录的异常类名:中立 LLM 异常取原始 SDK 类名,其余取自身类名。"""
    if isinstance(exc, ModelCallError):
        return exc.sdk_error_type
    return type(exc).__name__




class Effort(StrEnum):
    """思考强度档位，取值与 sessions.thinking_effort 一致。

    fast 表示不额外思考，而不是关闭思考：各端点按自己的协议决定 fast 下发什么，
    例如原生 OpenAI 协议在未声明 none 映射时保持模型默认行为。
    """

    FAST = "fast"
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    XHIGH = "xhigh"
    MAX = "max"

    @classmethod
    def parse(cls, value: str | None) -> Effort | None:
        """把会话或请求里的档位字符串转成枚举；None 原样返回，未知取值抛中文异常。"""
        if value is None:
            return None
        try:
            return cls(value)
        except ValueError as exc:
            raise AgentException.message("未知的思考强度档位", {"effort": value}) from exc


# 结构化输出未指定 max_tokens 时的上限，沿用原 complete_structured 接口的默认值。
STRUCTURED_DEFAULT_MAX_TOKENS = 256


@dataclass(frozen=True)
class ModelSettings:
    """一次模型调用的参数，按调用传入，客户端实例上不保存任何可变调用状态。

    effort 为 None 表示用端点默认的思考配置：.env 端点沿用 {provider}_thinking_*，
    工作区端点没有独立的默认开关，按 fast 处理。max_tokens 为 None 时流式调用取
    llm_default_max_tokens，结构化调用取 STRUCTURED_DEFAULT_MAX_TOKENS。
    timeout 为 None 表示沿用客户端构造时的 SDK 超时。
    """

    model_id: str
    effort: Effort | None = None
    max_tokens: int | None = None
    timeout: float | None = None

    def require_model_id(self) -> str:
        """取本次调用的模型名；为空属编码错误，抛中文异常而不回落任何默认模型。"""
        if not self.model_id:
            raise AgentException.message("模型调用缺少模型名")
        return self.model_id


class ModelClient(ABC):
    """LLM 客户端统一接口。

    实例只持有端点配置（地址、密钥、重试、超时、思考协议、档位映射表），
    模型名、思考强度、输出上限一律经 ModelSettings 按调用传入，不在实例上修改。
    """

    @property
    @abstractmethod
    def provider(self) -> str:
        """返回该客户端的 provider 标识（anthropic/openai）。"""

    @abstractmethod
    def stream(
        self,
        context: Context[None],
        settings: ModelSettings,
    ) -> AsyncIterator[ModelStreamEvent]:
        """流式调用，产出中立 ModelStreamEvent；provider 原始格式在实现内部消化，不外露。

        context 全部是领域类型，编码为本家 wire 形状由实现内部私有完成
        （_encode_messages / _encode_system / _encode_tools），调用方不参与：
        stream() 有多个调用点，快照与落库一律只存中立形状，翻译放在调用方则每处
        都要记得翻一次，漏一处即是一次 provider 形状不匹配的请求失败。

        实现应为 async generator（调用即得 AsyncIterator，无需 await），
        故此处不声明 async，避免签名被推断为「返回 AsyncIterator 的协程」。

        契约：每次调用必须以恰好一个 TURN_RESULT 事件收尾且 result 非空。
        """

    @abstractmethod
    async def complete_structured[O: BaseModel](
        self,
        context: Context[O],
        settings: ModelSettings,
    ) -> O:
        """结构化输出，返回 context.output_type 的实例。

        校验失败时抛出 ValueError 或 ValidationError。
        """


@dataclass(frozen=True)
class ResolvedModel:
    """模型解析结果：可调用的客户端，加上本场景的默认调用参数。

    provider_record_id 为工作区模型配置 ID，走 .env 时为 None；工作区端点自带模型名，
    调用方据此不再用 Agent 或会话上的模型名覆盖。context_length 为端点声明的上下文窗口。
    """

    client: ModelClient
    settings: ModelSettings
    provider_record_id: str | None = None
    context_length: int | None = None

    @classmethod
    def from_client(cls, client: ModelClient, model_id: str | None = None) -> ResolvedModel:
        """直接使用给定客户端（测试注入的替身或已建好的客户端），不经解析器。

        模型名取显式值，否则取客户端自带的默认模型名；effort 留空即端点默认，
        与注入替身自带思考配置的既有语义一致。替身没有模型名时为空串，成本归因据此留空。
        """
        default_model = getattr(client, "model", None) or ""
        return cls(client=client, settings=ModelSettings(model_id=model_id or default_model))


class SessionModelOptions(Protocol):
    """会话上与模型选择相关的只读字段（SessionRecord 天然满足）。"""

    @property
    def workspace_id(self) -> int | None: ...

    @property
    def user_id(self) -> int | None: ...

    @property
    def model_provider_id(self) -> str | None: ...

    @property
    def extra(self) -> dict[str, Any] | None: ...

    @property
    def thinking_effort(self) -> str: ...


class ModelResolver(Protocol):
    """按场景与会话解析模型：先查工作区模型配置，查不到再用 .env 的全局 provider。"""

    async def resolve(
        self,
        workspace_id: int | None,
        user_id: int | None,
        scenario: str,
        *,
        provider_id: str | None = None,
        effort: Effort | None = None,
    ) -> ResolvedModel:
        """显式指定 provider 与思考强度解析；workspace_id 为 None 时直接走 .env。"""
        ...

    async def for_session(self, session: SessionModelOptions, scenario: str) -> ResolvedModel:
        """按会话保存的模型选项解析：agent 场景始终用会话的供应商，其余场景仅显式指定时沿用。"""
        ...

    async def for_frozen_job(
        self,
        workspace_id: int,
        user_id: int,
        scenario: str,
        *,
        model: str | None,
        provider_id: str | None,
    ) -> ResolvedModel:
        """按后台任务入队时冻结的模型解析：旧任务只存了 .env 模型名，不迁往后来新增的数据库端点。"""
        ...


class ToolProvider(ABC):
    """进程级工具来源：一个 provider 产出一组工具，由 ToolSet 聚合、做重名校验并保持顺序。

    只给内置工具这类对所有人都一样的静态来源用，所以签名同步、不带参数。按工作区和用户
    变化的来源（如 MCP）实现 ScopedToolProvider，每回合异步解析。
    """

    @abstractmethod
    def list_tools(self) -> list[Tool]:
        """按稳定顺序返回本来源的全部工具；顺序决定发给模型的工具顺序，提示缓存依赖它。"""


@dataclass(frozen=True)
class ToolSourceFault:
    """某个作用域来源本回合不可用的原因，只放能给用户看的文本，不放异常细节和凭证。"""

    source: str
    # 前端据此发起授权或跳到管理页；来源整体失败、对不上具体 server 时为 None。
    server_id: int | None
    kind: Literal["unavailable", "auth_required", "timeout", "invalid"]
    message: str

    def to_dict(self) -> dict[str, Any]:
        """tool_sources 事件里的一项故障；不含授权地址、请求头和异常细节。"""
        return {
            "source": self.source,
            "server_id": self.server_id,
            "kind": self.kind,
            "message": self.message,
        }


@dataclass(frozen=True)
class ScopedTools:
    """一个作用域来源为某个身份列出的工具与故障。"""

    tools: tuple[Tool, ...]
    faults: tuple[ToolSourceFault, ...] = ()


class ScopedToolProvider(ABC):
    """作用域工具来源（如 MCP）：按身份异步列出工具，每回合调用一次，结果冻结进回合配置。

    返回该身份能用的全部工具，不按档位和绑定裁剪；单个 server 的超时由实现自己控制。
    """

    @property
    @abstractmethod
    def name(self) -> str:
        """来源标识（如 "mcp"）；整个来源失败或超时时，解析器记进故障的 source。"""

    @abstractmethod
    async def list_tools(self, scope: ToolScope) -> ScopedTools:
        """列出 scope 身份可用的全部作用域工具；整体失败时可以直接抛异常，由解析器记为故障。"""


class ToolCallReviewer(ABC):
    """工具调用审核员：模型发起的每个工具调用先经它审核，得到 allow / ask / deny 结论。

    现有实现是适配层的规则链 PolicyReviewer（预算、Plan 模式、入参、能力、交互工具、权限规则）。
    以后的 LLM 审核员是这个端口的另一种实现，也可以作为规则链里的一条规则接入。
    """

    @abstractmethod
    async def review(self, call: ToolCall, request: ReviewRequest) -> Decision:
        """审核一个工具调用。可能扣减 request.budget 的额度；放行时按 Decision.arguments 执行。"""


class SubAgentLauncher(Protocol):
    """子代理启动器：Agent 工具经它启动一次性子代理，拿回子代理的最终报告。

    工具只认这个端口，不认运行时；实现由组装根提供（独立数据库会话里构造子代理运行器执行），
    事件经调用方传入的 bus 发到父回合的事件流。
    """

    async def run(
        self,
        session_id: int,
        prompt: str,
        spec: SubAgentSpec,
        *,
        bus: StreamBus | None,
        parent_turn_id: UUID | None,
        user_id: int | None,
        workspace_id: int | None,
        parent_allowed_tool_names: frozenset[str] | None,
        allowed_skill_names: frozenset[str] | None,
        scoped: ScopedGrant | None = None,
    ) -> str:
        """按 spec 运行一次子代理并返回报告；scoped 是交给子代理的作用域工具，None 表示不给。
        子代理运行失败时原样上抛。"""
        ...
