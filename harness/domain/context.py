"""模型调用上下文：一次调用要交给模型的指令、消息、工具与输出类型。

``Context`` 只描述「说什么」，不描述「怎么调」：模型名、思考强度、输出上限等调用参数
在应用层的 ModelSettings 里按调用传入。记忆与项目规范不单列字段，由角色渲染进
instruction 或 messages，模型客户端不需要懂它们的语义。
"""

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from typing import Any

from core.errors import AgentException
from harness.domain.messages import Message, message_from_row


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


@dataclass(frozen=True)
class Context[O]:
    """一次模型调用的输入。

    output_type 为 None 表示流式调用；结构化调用传入 Pydantic 模型类，客户端据此约束输出。
    instruction 传块元组时可携带缓存断点，传字符串即整段纯文本。
    """

    instruction: str | tuple[PromptBlock, ...] | None
    messages: tuple[Message, ...]
    tools: tuple[ToolSpec, ...] = ()
    output_type: type[O] | None = None

    def require_output_type(self) -> type[O]:
        """取结构化输出的目标类型，缺失时抛中文异常。"""
        if self.output_type is None:
            raise AgentException.message("结构化输出缺少目标类型")
        return self.output_type

    def text_instruction(self) -> str | None:
        """取纯文本指令；结构化输出不支持缓存断点块，传块元组时抛中文异常。"""
        if isinstance(self.instruction, tuple):
            raise AgentException.message("结构化输出的指令只支持纯文本")
        return self.instruction


def messages_from_rows(rows: Iterable[Mapping[str, Any]]) -> tuple[Message, ...]:
    """把落库/上下文里的中立消息字典批量还原为领域消息，形状非法时抛中文异常。"""
    return tuple(message_from_row(row.get("role"), row.get("content")) for row in rows)
