from dataclasses import dataclass, field
from typing import Any

from app.core.events import (
    Actor,
    BlockType,
    StreamEvent,
    ToolInfo,
    Usage,
)

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



_BLOCK_TYPE_MAP = {
    "text": BlockType.TEXT,
    "thinking": BlockType.THINKING,
    "tool_use": BlockType.TOOL_USE,
}


@dataclass
class _OpenBlock:
    """翻译过程中正在累积的内容块状态。"""

    index: int
    block_type: BlockType
    tool_id: str = ""
    tool_name: str = ""
    json_buffer: str = ""


class AnthropicStreamTranslator:
    """把 Anthropic 原始流翻译成协议 StreamEvent 序列。

    有状态:跨一个 turn 内多次 LLM 调用沿用同一个翻译器,block index 持续递增,
    tool_use 的 input JSON 在 content_block_stop 时累积完整后一次性产出(不逐字发)。
    turn_start / turn_end / tool_result 由调用方(runner)负责,翻译器只管中间内容。
    """

    def __init__(self, actor: Actor, session_id: int):
        self._actor = actor
        self._session_id = session_id
        self._open: dict[int, _OpenBlock] = {}
        self._last_usage: Usage | None = None
        self._stop_reason: str | None = None

    @property
    def last_usage(self) -> Usage | None:
        """最近一次 message_final 解析到的用量(供 turn_end 使用)。"""
        return self._last_usage

    @property
    def stop_reason(self) -> str | None:
        """最近一次 message_final 的停止原因(供 turn_end 使用)。"""
        return self._stop_reason


    def translate(self, chunk: LLMRawChunk) -> list[StreamEvent]:
        """翻译单个原始 chunk 为零或多个协议事件。"""
        ctype = chunk.get("type")
        if ctype == "content_block_start":
            return self._on_block_start(chunk)
        if ctype == "content_block_delta":
            return self._on_block_delta(chunk)
        if ctype == "content_block_stop":
            return self._on_block_stop(chunk)
        if ctype == "message_final":
            self._on_message_final(chunk)
        # ping / message_start / message_delta / signature 等噪声不产出协议事件。
        return []

    def _on_block_start(self, chunk: LLMRawChunk) -> list[StreamEvent]:
        index = int(chunk.get("index", 0))
        cb = chunk.get("content_block") or {}
        raw_type = cb.get("type", "text")
        block_type = _BLOCK_TYPE_MAP.get(raw_type, BlockType.TEXT)
        self._open[index] = _OpenBlock(
            index=index,
            block_type=block_type,
            tool_id=cb.get("id", "") or "",
            tool_name=cb.get("name", "") or "",
        )
        return [
            StreamEvent.block_start(self._actor, self._session_id, index, block_type)
        ]

    def _on_block_delta(self, chunk: LLMRawChunk) -> list[StreamEvent]:
        index = int(chunk.get("index", 0))
        delta = chunk.get("delta") or {}
        dtype = delta.get("type")
        if dtype == "text_delta":
            return [
                StreamEvent.text_delta(
                    self._actor, self._session_id, index, delta.get("text", "")
                )
            ]
        if dtype == "thinking_delta":
            return [
                StreamEvent.thinking_delta(
                    self._actor, self._session_id, index, delta.get("thinking", "")
                )
            ]
        if dtype == "input_json_delta":
            # 累积 tool_use 的入参 JSON,等 block_stop 一次性产出。
            block = self._open.get(index)
            if block is not None:
                block.json_buffer += delta.get("partial_json", "")
            return []
        # signature_delta 等:噪声,不产出。
        return []


    def _on_block_stop(self, chunk: LLMRawChunk) -> list[StreamEvent]:
        import json as _json

        index = int(chunk.get("index", 0))
        block = self._open.pop(index, None)
        if block is None:
            return [StreamEvent.block_stop(self._actor, self._session_id, index)]
        if block.block_type is BlockType.TOOL_USE:
            # tool_use 块:入参累积完毕,一次性产出完整 tool_use 事件(不发 block_stop)。
            try:
                tool_input = _json.loads(block.json_buffer) if block.json_buffer else {}
            except _json.JSONDecodeError:
                tool_input = {}
            tool = ToolInfo(id=block.tool_id, name=block.tool_name, input=tool_input)
            return [
                StreamEvent.tool_use(self._actor, self._session_id, index, tool)
            ]
        return [StreamEvent.block_stop(self._actor, self._session_id, index)]

    def _on_message_final(self, chunk: LLMRawChunk) -> None:
        """记录 usage / stop_reason,供 runner 构造 turn_end。"""
        self._stop_reason = chunk.get("stop_reason")
        usage = chunk.get("usage")
        if isinstance(usage, dict):
            self._last_usage = Usage(
                input_tokens=int(usage.get("input_tokens", 0) or 0),
                output_tokens=int(usage.get("output_tokens", 0) or 0),
                cache_creation_input_tokens=int(
                    usage.get("cache_creation_input_tokens", 0) or 0
                ),
                cache_read_input_tokens=int(
                    usage.get("cache_read_input_tokens", 0) or 0
                ),
            )





