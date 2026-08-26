from dataclasses import dataclass, field
from enum import Enum
from typing import Any

from core.errors import AgentException
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


# assistant 消息中立块的判别键取值。刻意用 kind 而非 type:两家 wire 形状的
# type 取值恰好同名(都叫 tool_use)、入参键也同名(都叫 input),沿用 type 会让
# 残留的 provider 形状数据被静默误读并一路潜伏到运行时;换 kind 让它当场暴露。
_KIND_TEXT = "text"
_KIND_TOOL_USE = "tool_use"


@dataclass(frozen=True)
class TextBlock:
    """assistant 消息的中立文本块。"""

    text: str

    def to_dict(self) -> dict[str, Any]:
        """序列化为落库/上下文的中立形状。"""
        return {"kind": _KIND_TEXT, "text": self.text}


@dataclass(frozen=True)
class ToolUseBlock:
    """assistant 消息的中立工具调用块。

    与 ToolUse 的区别:ToolUse 是流式终局的运行时载体(供 runtime 执行工具),
    本类是消息体的持久化/上下文形状,由出站翻译编成各家 wire。
    """

    id: str
    name: str
    input: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        """序列化为落库/上下文的中立形状。"""
        return {
            "kind": _KIND_TOOL_USE,
            "id": self.id,
            "name": self.name,
            "input": self.input,
        }


type AssistantBlock = TextBlock | ToolUseBlock


def build_assistant_blocks(text: str, tool_uses: list[ToolUse]) -> list[dict[str, Any]]:
    """从单次调用终局组装 assistant 消息的中立块数组。

    文本块在前、工具调用块在后:块间顺序不承载语义(两家 wire 都按角色而非块序
    解释消息),故不保留 provider 下发时的交错顺序。空文本不产出空块。
    """
    blocks: list[dict[str, Any]] = []
    if text:
        blocks.append(TextBlock(text=text).to_dict())
    blocks.extend(
        ToolUseBlock(id=tool_use.id, name=tool_use.name, input=tool_use.input).to_dict()
        for tool_use in tool_uses
    )
    return blocks


def parse_assistant_blocks(content: Any) -> list[AssistantBlock]:
    """把 assistant 消息 content 解析为类型化中立块。

    str 形态(纯文本回复)归一为单个文本块,空串归一为空列表。
    形状非法或 kind 未知一律抛中文 AgentException:无静默兜底,
    残留的 provider wire 形状(带 type 而非 kind)在此当场暴露。
    """
    if isinstance(content, str):
        return [TextBlock(text=content)] if content else []
    if not isinstance(content, list):
        raise AgentException.message(
            "assistant 消息 content 必须是中立块数组或字符串",
            {"content_type": type(content).__name__},
        )
    blocks: list[AssistantBlock] = []
    for raw in content:
        if not isinstance(raw, dict):
            raise AgentException.message(
                "assistant 消息块必须是字典", {"block_type": type(raw).__name__}
            )
        kind = raw.get("kind")
        if kind == _KIND_TEXT:
            blocks.append(TextBlock(text=raw.get("text", "")))
        elif kind == _KIND_TOOL_USE:
            blocks.append(
                ToolUseBlock(
                    id=raw["id"],
                    name=raw["name"],
                    input=raw.get("input") or {},
                )
            )
        else:
            raise AgentException.message(
                "assistant 消息块的 kind 未知或缺失",
                {"kind": kind, "keys": sorted(raw.keys())},
            )
    return blocks


def parse_user_text(content: Any) -> str:
    """校验并取出 user 消息的中立文本内容。

    中立协议下 user 消息只承载纯文本(工具结果走独立的 role="tool" 消息),
    故 list 形态一律视为残留的 provider wire 形状并当场抛错。
    """
    if not isinstance(content, str):
        raise AgentException.message(
            "user 消息 content 必须是字符串", {"content_type": type(content).__name__}
        )
    return content


def parse_tool_result(content: Any) -> ToolResultMessage:
    """把 tool 消息 content 解析为 ToolResultMessage,形状非法时抛中文异常。"""
    if not isinstance(content, dict):
        raise AgentException.message(
            "tool 消息 content 必须是中立 ToolResultMessage 字典",
            {"content_type": type(content).__name__},
        )
    try:
        return ToolResultMessage.from_content_dict(content)
    except KeyError as exc:
        raise AgentException.message(
            "tool 消息缺少必需字段", {"field": str(exc.args[0]) if exc.args else ""}
        ) from exc


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
