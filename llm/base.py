"""LLM 客户端统一接口。"""

from abc import ABC, abstractmethod
from collections.abc import AsyncIterator
from typing import Any

from pydantic import BaseModel

from llm.types import LLMStreamEvent, PromptBlock, ToolSpec


class BaseLLMClient(ABC):
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
    ) -> AsyncIterator[LLMStreamEvent]:
        """流式调用，产出中立 LLMStreamEvent；provider 原始格式在实现内部消化，不外露。

        入参全部是中立类型，编码为本家 wire 形状由实现内部私有完成
        （_encode_messages / _encode_system / _encode_tools），调用方不参与。

        messages 契约：中立消息形状，而非任何 provider 的 wire 形状。三类角色：
        - {"role": "user", "content": "纯文本"}
        - {"role": "assistant", "content": [中立块数组]}，块以 kind 判别
          （见 llm.types 的 TextBlock / ToolUseBlock）
        - {"role": "tool", "content": ToolResultMessage.to_content_dict()}

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
