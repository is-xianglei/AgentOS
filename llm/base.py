"""LLM 客户端统一接口。"""

from abc import ABC, abstractmethod
from collections.abc import AsyncIterator
from typing import Any

from pydantic import BaseModel


class BaseLLMClient(ABC):
    """LLM 客户端统一接口。"""

    @property
    @abstractmethod
    def provider(self) -> str:
        """返回该客户端的 provider 标识（anthropic/openai）。"""

    @abstractmethod
    def stream(
        self,
        messages: list[dict],
        system_prompt: str | list[dict[str, Any]] | None = None,
        tools: list[dict[str, Any]] | None = None,
        model: str | None = None,
    ) -> AsyncIterator[dict[str, Any]]:
        """流式调用，返回原始事件流。

        实现应为 async generator（调用即得 AsyncIterator，无需 await），
        故此处不声明 async，避免签名被推断为「返回 AsyncIterator 的协程」。

        事件格式：
        - Anthropic: content_block_start/delta/stop, message_final
        - OpenAI: choices[].delta, finish_reason, usage
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
