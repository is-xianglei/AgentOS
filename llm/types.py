"""LLM 层的中立类型:流事件、工具与 system prompt 描述,不含任何 provider 的 wire 形状。"""

from dataclasses import dataclass, field
from enum import Enum
from typing import Any

from core.events import BlockType, ToolInfo, Usage
from harness.domain.messages import ThinkingBlock, ToolCall


class LLMEventKind(str, Enum):
    """中立流事件种类。刻意与 core.events.EventType 分开:
    前者是 provider 出口,后者是对外协议,二者不应互相绑定演进。"""

    BLOCK_START = "block_start"  # 内容块开始
    TEXT_DELTA = "text_delta"  # 文本增量
    THINKING_DELTA = "thinking_delta"  # 思考增量
    TOOL_USE = "tool_use"  # 工具调用(入参已解析完整)
    BLOCK_STOP = "block_stop"  # 内容块结束
    TURN_RESULT = "turn_result"  # 本次调用终局(流的最后一个事件)


@dataclass(frozen=True)
class LLMTurnResult:
    """单次 LLM 调用的终局信息,取代原先 runtime 自行解析 message_final。"""

    text: str = ""
    tool_calls: list[ToolCall] = field(default_factory=list)
    thinking: list[ThinkingBlock] = field(default_factory=list)
    usage: Usage | None = None
    stop_reason: str | None = None


@dataclass(frozen=True)
class LLMStreamEvent:
    """provider 适配后的中立流事件。

    不含 actor / session_id / 全局 index —— 那是 runtime 的装配职责。
    block_ref 是**单次 stream() 调用内**的块序号,由 provider 自行分配、
    从 0 递增即可;跨调用的全局唯一性由 TurnEventAssembler 负责。

    契约:每次 stream() 必须以恰好一个 TURN_RESULT 事件收尾且 result 非空;
    提前耗尽视为 provider 实现错误,由消费方抛中文 AgentException。
    """

    kind: LLMEventKind
    block_ref: int | None = None
    block_type: BlockType | None = None  # 仅 BLOCK_START
    text: str = ""  # 仅 TEXT_DELTA / THINKING_DELTA
    tool: ToolInfo | None = None  # 仅 TOOL_USE
    result: LLMTurnResult | None = None  # 仅 TURN_RESULT


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
