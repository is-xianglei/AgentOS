"""领域消息：Agent 与模型往来的类型化语言，以及它与落库中立 JSON 的互转。

三类消息组成 ``Message``：用户输入 ``UserMessage``、模型回复 ``AssistantMessage``、
工具执行结果 ``ToolResult``，分别对应 session_messages 里 user / assistant / tool 三种
content 形状（字符串、块数组、字典），经各类型的 ``to_content()`` 与 ``message_from_row()``
往返。本模块只认中立形状，编成各家 provider 的 wire 形状是客户端出站翻译的私有职责。
"""

from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any

from core.errors import AgentException

# assistant 消息中立块的判别键取值。刻意用 kind 而非 type:两家 wire 形状的
# type 取值恰好同名(都叫 tool_use)、入参键也同名(都叫 input),沿用 type 会让
# 残留的 provider 形状数据被静默误读并一路潜伏到运行时;换 kind 让它当场暴露。
_KIND_TEXT = "text"
_KIND_TOOL_USE = "tool_use"
_KIND_THINKING = "thinking"


@dataclass(frozen=True)
class ThinkingBlock:
    """模型的一段思考，既是单次调用终局的载体，也是 assistant 消息的持久化块。

    signature 与 redacted_data 是 provider 下发的不透明取值,与 ToolCall.id
    同一性质:必须原样回传,但不构成 wire 形状泄漏。
    Anthropic 要求思考块原样返回、不得修改重排或过滤,故二者必须落库;
    OpenAI(DeepSeek)侧无签名概念,两者恒为 None。
    redacted_data 承载 Anthropic 的 redacted_thinking 加密块,无可读文本,
    与 signature 恒有其一为 None。
    """

    text: str = ""
    signature: str | None = None
    redacted_data: str | None = None

    def to_dict(self) -> dict[str, Any]:
        """序列化为落库/上下文的中立块。"""
        return {
            "kind": _KIND_THINKING,
            "text": self.text,
            "signature": self.signature,
            "redacted_data": self.redacted_data,
        }

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> ThinkingBlock:
        """从中立块还原;按 kind 分派由调用方负责,此处不再校验。"""
        return cls(
            text=raw.get("text", ""),
            signature=raw.get("signature"),
            redacted_data=raw.get("redacted_data"),
        )


@dataclass(frozen=True)
class TextBlock:
    """assistant 消息的文本块。"""

    text: str

    def to_dict(self) -> dict[str, Any]:
        """序列化为落库/上下文的中立块。"""
        return {"kind": _KIND_TEXT, "text": self.text}

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> TextBlock:
        """从中立块还原。"""
        return cls(text=raw.get("text", ""))


@dataclass(frozen=True)
class ToolCall:
    """模型发起的一次工具调用，既供运行时执行工具，也作为 assistant 消息的持久化块。

    JSON 键名是落库契约，字段名是领域语言，两者有意不同：
    tool_name 落库为 name，arguments 落库为 input，判别键 kind 取值仍为 tool_use。
    """

    id: str
    tool_name: str
    arguments: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        """序列化为落库/上下文的中立块。"""
        return {
            "kind": _KIND_TOOL_USE,
            "id": self.id,
            "name": self.tool_name,
            "input": self.arguments,
        }

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> ToolCall:
        """从中立块还原;缺 id 或 name 时抛 KeyError,由调用方决定如何包装。"""
        return cls(id=raw["id"], tool_name=raw["name"], arguments=raw.get("input") or {})


type AssistantBlock = ThinkingBlock | TextBlock | ToolCall


def _parse_block(raw: Any) -> AssistantBlock:
    """按 kind 把单个中立块还原为类型化块;残留的 provider wire 形状在此当场暴露。"""
    if not isinstance(raw, dict):
        raise AgentException.message(
            "assistant 消息块必须是字典", {"block_type": type(raw).__name__}
        )
    kind = raw.get("kind")
    if kind == _KIND_THINKING:
        return ThinkingBlock.from_dict(raw)
    if kind == _KIND_TEXT:
        return TextBlock.from_dict(raw)
    if kind == _KIND_TOOL_USE:
        return ToolCall.from_dict(raw)
    raise AgentException.message(
        "assistant 消息块的 kind 未知或缺失",
        {"kind": kind, "keys": sorted(raw.keys())},
    )


@dataclass(frozen=True)
class UserMessage:
    """用户消息。中立协议下只承载纯文本,工具结果走独立的 ToolResult。"""

    text: str

    def to_content(self) -> str:
        """序列化为落库/上下文的 content。"""
        return self.text

    @classmethod
    def from_content(cls, content: Any) -> UserMessage:
        """校验并还原 user 消息 content。

        list 形态一律视为残留的 provider wire 形状并当场抛错。
        """
        if not isinstance(content, str):
            raise AgentException.message(
                "user 消息 content 必须是字符串", {"content_type": type(content).__name__}
            )
        return cls(text=content)


@dataclass(frozen=True)
class AssistantMessage:
    """模型回复，按块序列保存。

    视频里的形状是 ``AssistantMessage(text, tool_calls)``，这里改存块序列：思考块必须保持
    模型下发的原始顺序，并带签名原样回传——Anthropic 不允许修改、重排或过滤思考块，
    同一回合的多段思考签名互不相同，合并即失效；只存 text 与 tool_calls 就丢了这部分。
    text、tool_calls、thinking 是从块序列派生的便捷视图。
    """

    blocks: tuple[AssistantBlock, ...]

    @property
    def text(self) -> str:
        """所有文本块按序拼接。"""
        return "".join(block.text for block in self.blocks if isinstance(block, TextBlock))

    @property
    def tool_calls(self) -> tuple[ToolCall, ...]:
        """按序取出全部工具调用块。"""
        return tuple(block for block in self.blocks if isinstance(block, ToolCall))

    @property
    def thinking(self) -> tuple[ThinkingBlock, ...]:
        """按序取出全部思考块。"""
        return tuple(block for block in self.blocks if isinstance(block, ThinkingBlock))

    @classmethod
    def from_turn(
        cls,
        text: str,
        tool_calls: Sequence[ToolCall],
        thinking: Sequence[ThinkingBlock] | None = None,
    ) -> AssistantMessage:
        """从单次调用终局组装 assistant 消息。

        思考块在最前且**保持 provider 下发的原序**:Anthropic 要求带签名的思考块
        原样回传,不得修改、重排或过滤,同一回合的多段思考签名互不相同,合并即失效。
        其后是文本块、工具调用块——这两类之间顺序不承载语义(两家 wire 都按角色而非
        块序解释消息),故不保留其交错顺序。空文本不产出空块;空思考段同样跳过。
        """
        blocks: list[AssistantBlock] = []
        for segment in thinking or []:
            # redacted_thinking 没有可读文本但必须保留,只有文本和加密载荷都为空才跳过。
            if not segment.text and not segment.redacted_data:
                continue
            blocks.append(segment)
        if text:
            blocks.append(TextBlock(text=text))
        blocks.extend(tool_calls)
        return cls(blocks=tuple(blocks))

    def to_content(self) -> list[dict[str, Any]]:
        """序列化为落库/上下文的中立块数组,块序原样保留。"""
        return [block.to_dict() for block in self.blocks]

    @classmethod
    def from_content(cls, content: Any) -> AssistantMessage:
        """把 assistant 消息 content 还原为类型化块序列。

        str 形态(纯文本回复)归一为单个文本块,空串归一为空序列。
        形状非法或 kind 未知一律抛中文 AgentException:无静默兜底,
        残留的 provider wire 形状(带 type 而非 kind)在此当场暴露。
        """
        if isinstance(content, str):
            if not content:
                return cls(blocks=())
            return cls(blocks=(TextBlock(text=content),))
        if not isinstance(content, list):
            raise AgentException.message(
                "assistant 消息 content 必须是中立块数组或字符串",
                {"content_type": type(content).__name__},
            )
        return cls(blocks=tuple(_parse_block(raw) for raw in content))


@dataclass(frozen=True)
class ToolResult:
    """工具执行结果,即 tool 角色消息。

    比视频多带 tool_name、arguments、is_error:落库排障与审计要用。
    tool_name 与 arguments 是 AgentOS 自有字段,两家 provider 协议都无对应位置,不发给模型。
    JSON 键名是落库契约，字段名是领域语言，两者有意不同：tool_call_id 落库为 tool_use_id，
    arguments 落库为 input_args，content 落库为 output。
    """

    tool_call_id: str
    tool_name: str
    arguments: dict[str, Any]
    content: str
    is_error: bool = False

    def to_dict(self) -> dict[str, Any]:
        """序列化为落库的 content 字典。"""
        return {
            "tool_use_id": self.tool_call_id,
            "tool_name": self.tool_name,
            "input_args": self.arguments,
            "output": self.content,
            "is_error": self.is_error,
        }

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> ToolResult:
        """从落库的 content 字典还原;缺 tool_use_id 或 output 时抛 KeyError。"""
        return cls(
            tool_call_id=raw["tool_use_id"],
            tool_name=raw.get("tool_name", ""),
            arguments=raw.get("input_args") or {},
            content=raw["output"],
            is_error=bool(raw.get("is_error", False)),
        )

    def to_content(self) -> dict[str, Any]:
        """序列化为落库/上下文的 tool 消息 content。"""
        return self.to_dict()

    @classmethod
    def from_content(cls, content: Any) -> ToolResult:
        """校验并还原 tool 消息 content,形状非法时抛中文异常。"""
        # 报错文案沿用改名前的类名:属既有错误契约,本阶段不改对外可见行为。
        if not isinstance(content, dict):
            raise AgentException.message(
                "tool 消息 content 必须是中立 ToolResultMessage 字典",
                {"content_type": type(content).__name__},
            )
        try:
            return cls.from_dict(content)
        except KeyError as exc:
            # KeyError 的首个参数就是缺失的键名。
            missing_field = str(exc.args[0]) if exc.args else ""
            raise AgentException.message("tool 消息缺少必需字段", {"field": missing_field}) from exc


type Message = UserMessage | AssistantMessage | ToolResult


def message_from_row(role: str, content: Any) -> Message:
    """按角色把一条落库/上下文消息还原为领域消息,未知角色抛中文异常。

    三种 content 形状:user 为字符串、assistant 为中立块数组、tool 为字典;
    反向序列化统一走各类型的 to_content()。
    """
    if role == "user":
        return UserMessage.from_content(content)
    if role == "assistant":
        return AssistantMessage.from_content(content)
    if role == "tool":
        return ToolResult.from_content(content)
    raise AgentException.message("未知的消息角色", {"role": role})
