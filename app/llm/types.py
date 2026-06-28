from dataclasses import dataclass, field
from typing import Any

LLMRawChunk = dict[str, Any]


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

    def to_content_dict(self) -> dict[str, Any]:
        """序列化为落库的 content dict。"""
        return {
            "tool_use_id": self.tool_use_id,
            "tool_name": self.tool_name,
            "input_args": self.input_args,
            "output": self.output,
        }

    @classmethod
    def from_content_dict(cls, content: dict[str, Any]) -> "ToolResultMessage":
        """从落库的 content dict 还原。"""
        return cls(
            tool_use_id=content["tool_use_id"],
            tool_name=content.get("tool_name", ""),
            input_args=content.get("input_args") or {},
            output=content["output"],
        )




def chunk_type(chunk: LLMRawChunk) -> str | None:
    """读取 Anthropic 原始事件的顶层类型。"""
    value = chunk.get("type")
    return value if isinstance(value, str) else None


def chunk_text(chunk: LLMRawChunk) -> str:
    """从原始 content_block_delta 中取出文本增量,非文本事件返回空串。"""
    delta = chunk.get("delta")
    if (
            isinstance(delta, dict)
            and delta.get("type") == "text_delta"
            and isinstance(delta.get("text"), str)
    ):
        return delta["text"]
    return ""


def extract_tool_uses(content: list[dict[str, Any]] | None) -> list[ToolUse]:
    """从完整消息内容中提取 tool_use 块,返回类型化的 ToolUse 列表。"""
    return [
        ToolUse(id=block["id"], name=block["name"], input=block.get("input") or {})
        for block in (content or [])
        if isinstance(block, dict) and block.get("type") == "tool_use"
    ]


def text_from_content(content: list[dict[str, Any]] | None) -> str:
    """从完整消息内容中拼接文本块。"""
    return "".join(
        block.get("text", "")
        for block in (content or [])
        if isinstance(block, dict) and block.get("type") == "text"
    )

