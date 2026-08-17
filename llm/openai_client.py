"""OpenAI 客户端实现。"""

import logging
from collections.abc import AsyncIterator
from typing import Any, cast

from openai import NOT_GIVEN, AsyncOpenAI
from openai.types.chat import ChatCompletionFunctionToolParam, ChatCompletionMessageParam
from pydantic import BaseModel

from core.config import settings
from llm.base import BaseLLMClient
from llm.types import LLMEventKind, LLMStreamEvent, PromptBlock, ToolSpec

logger = logging.getLogger(__name__)


class OpenAIClient(BaseLLMClient):
    def __init__(
        self,
        model: str,
        api_key: str | None = None,
        base_url: str | None = None,
        max_retries: int = 2,
        timeout: float | None = None,
    ):
        self.model = model
        self.api_key = api_key or settings.openai_api_key
        self.base_url = base_url or settings.openai_base_url
        self.max_retries = max_retries
        self.timeout = timeout

    @property
    def provider(self) -> str:
        return "openai"

    @staticmethod
    def _encode_system(system_prompt: str | list[PromptBlock] | None) -> str:
        """把中立 system 描述编成 OpenAI 的纯文本 system 消息内容。

        OpenAI 无 prompt cache 断点概念,cache_breakpoint 标志直接忽略,
        多块按块间空行拼接成纯文本(与 Anthropic 侧块拼接语义一致)。
        """
        if system_prompt is None:
            return ""
        if isinstance(system_prompt, str):
            return system_prompt
        return "\n\n".join(block.text for block in system_prompt)

    @staticmethod
    def _encode_tools(tools: list[ToolSpec] | None) -> list[ChatCompletionFunctionToolParam]:
        """把中立工具描述编成 OpenAI 嵌套的 function tool 形状。"""
        return [
            ChatCompletionFunctionToolParam(
                type="function",
                function={
                    "name": spec.name,
                    "description": spec.description,
                    "parameters": spec.input_schema,
                },
            )
            for spec in (tools or [])
        ]

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
        """使用 OpenAI Structured Outputs API。"""
        effective_timeout = timeout if timeout is not None else self.timeout
        client = AsyncOpenAI(
            api_key=self.api_key,
            base_url=self.base_url,
            max_retries=self.max_retries,
            # 显式 None 会被 SDK 视为「禁用超时」,转 NOT_GIVEN 保留 SDK 默认超时。
            timeout=effective_timeout if effective_timeout is not None else NOT_GIVEN,
        )

        # 构造消息（OpenAI 要求 system 是首条消息）
        openai_messages: list[dict[str, Any]] = []
        if system_prompt:
            openai_messages.append({"role": "system", "content": system_prompt})
        openai_messages.extend(messages)

        # 调用 parse() 方法（Structured Outputs）
        response = await client.chat.completions.parse(
            model=model or self.model,
            # 接口层统一用 dict[str, Any] 承载动态 JSON，此处收窄为 SDK 的 TypedDict。
            messages=cast("list[ChatCompletionMessageParam]", openai_messages),
            response_format=output_format,
            max_completion_tokens=max_tokens,
            temperature=0,
        )

        parsed = response.choices[0].message.parsed
        if parsed is None:
            raise ValueError(
                f"结构化输出缺失，finish_reason={response.choices[0].finish_reason}"
            )
        return parsed

    async def stream(
        self,
        messages: list[dict[str, Any]],
        system_prompt: str | list[PromptBlock] | None = None,
        tools: list[ToolSpec] | None = None,
        model: str | None = None,
    ) -> AsyncIterator[LLMStreamEvent]:
        """流式调用（待实现）。"""
        raise NotImplementedError("OpenAI 流式在后续 Phase 实现")
        # 不可达的 yield 使本方法成为 async generator，与基类签名保持一致。
        yield LLMStreamEvent(kind=LLMEventKind.TURN_RESULT)
