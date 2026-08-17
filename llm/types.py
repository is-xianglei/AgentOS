from dataclasses import dataclass, field
from enum import Enum
from typing import Any

from core.events import (
    BlockType,
    ToolInfo,
    Usage,
)


@dataclass(frozen=True)
class ToolUse:
    """一次工具调用请求(从模型返回的 tool_use 块解析而来)。

    frozen=True 使其不可变,类似 Java 的 record。字段有明确类型,
    访问用 tool_use.name 而非 tool_use["name"]。
    """

    id: str
    name: str
    input: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class ToolResultMessage:
    """tool 角色消息的内容结构。

    落库为 session_messages.content 的 JSON,跨 AgentRuntime(写)与
    SessionService(读)使用。
    """

    tool_use_id: str
    tool_name: str
    input_args: dict[str, Any]
    output: str
    is_error: bool = False

    def to_content_dict(self) -> dict[str, Any]:
        """序列化为落库的 content dict。"""
        return {
            "tool_use_id": self.tool_use_id,
            "tool_name": self.tool_name,
            "input_args": self.input_args,
            "output": self.output,
            "is_error": self.is_error,
        }

    @classmethod
    def from_content_dict(cls, content: dict[str, Any]) -> ToolResultMessage:
        """从落库的 content dict 还原。"""
        return cls(
            tool_use_id=content["tool_use_id"],
            tool_name=content.get("tool_name", ""),
            input_args=content.get("input_args") or {},
            output=content["output"],
            is_error=bool(content.get("is_error", False)),
        )


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
    tool_uses: list[ToolUse] = field(default_factory=list)
    usage: Usage | None = None
    stop_reason: str | None = None
    # 遗留承载:assistant 消息落库仍是 provider 形状的 content 块数组,
    # 由客户端原样带出给 runtime 落库。消息体中立化属另一改造半径,本期不做;
    # 不得再新增第二个同类透传字段。
    provider_content: list[dict[str, Any]] = field(default_factory=list)


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
