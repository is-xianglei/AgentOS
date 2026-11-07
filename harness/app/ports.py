"""应用层端口：模型客户端接口及其中立流事件、工具描述、system prompt 块与调用异常。

全部类型不含任何 provider 的 wire 形状；各家 SDK 的适配在 harness.adapters.models 内部完成。
"""

from abc import ABC, abstractmethod
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

from pydantic import BaseModel

from core.events import BlockType, ToolInfo, Usage
from harness.domain.messages import ThinkingBlock, ToolCall


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


@dataclass(frozen=True)
class ToolSpec:
    """中立工具描述。各家客户端自行编码成本家 tools 参数形状。"""

    name: str
    description: str
    input_schema: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class PromptBlock:
    """中立 system prompt 块。

    cache_breakpoint=True 表示「此块末尾是一个缓存断点」,语义中立;
    Anthropic 编码为 cache_control={"type": "ephemeral"},
    不支持缓存的 provider 直接忽略该标志并拼接纯文本。
    """

    text: str
    cache_breakpoint: bool = False


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


class ModelClient(ABC):
    """LLM 客户端统一接口。"""

    @property
    @abstractmethod
    def provider(self) -> str:
        """返回该客户端的 provider 标识（anthropic/openai）。"""

    @abstractmethod
    def stream(
        self,
        messages: list[dict[str, Any]],
        system_prompt: str | list[PromptBlock] | None = None,
        tools: list[ToolSpec] | None = None,
        model: str | None = None,
    ) -> AsyncIterator[ModelStreamEvent]:
        """流式调用，产出中立 ModelStreamEvent；provider 原始格式在实现内部消化，不外露。

        入参全部是中立类型，编码为本家 wire 形状由实现内部私有完成
        （_encode_messages / _encode_system / _encode_tools），调用方不参与。

        messages 契约：中立消息形状，而非任何 provider 的 wire 形状。三类角色：
        - {"role": "user", "content": "纯文本"}
        - {"role": "assistant", "content": [中立块数组]}，块以 kind 判别
          （见 harness.domain.messages 的 AssistantMessage）
        - {"role": "tool", "content": ToolResult.to_content()}

        出站翻译刻意收在实现内部而非调用方：stream() 有多个调用点（主 Agent、
        SubAgent、Compact），且快照与落库一律只存中立形状，翻译放在调用方则每处
        都要记得翻一次，漏一处即是一次 provider 形状不匹配的请求失败。

        实现应为 async generator（调用即得 AsyncIterator，无需 await），
        故此处不声明 async，避免签名被推断为「返回 AsyncIterator 的协程」。

        契约：每次调用必须以恰好一个 TURN_RESULT 事件收尾且 result 非空。
        """

    @abstractmethod
    async def complete_structured[T: BaseModel](
        self,
        messages: list[dict[str, Any]],
        output_format: type[T],
        *,
        system_prompt: str | None = None,
        model: str | None = None,
        max_tokens: int = 256,
        timeout: float | None = None,
    ) -> T:
        """结构化输出，返回 Pydantic 模型实例。

        校验失败时抛出 ValueError 或 ValidationError。
        """
