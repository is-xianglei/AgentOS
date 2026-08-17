"""OpenAI 客户端实现。"""

import logging
from collections.abc import AsyncIterator
from typing import Any, cast

from openai import NOT_GIVEN, AsyncOpenAI
from openai.types.chat import ChatCompletionMessageParam
from pydantic import BaseModel

from core.config import settings
from llm.base import BaseLLMClient
from llm.types import LLMEventKind, LLMStreamEvent

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
        messages: list[dict],
        system_prompt: str | list[dict[str, Any]] | None = None,
        tools: list[dict[str, Any]] | None = None,
        model: str | None = None,
    ) -> AsyncIterator[LLMStreamEvent]:
        """流式调用（待实现）。"""
        raise NotImplementedError("OpenAI 流式在后续 Phase 实现")
        # 不可达的 yield 使本方法成为 async generator，与基类签名保持一致。
        yield LLMStreamEvent(kind=LLMEventKind.TURN_RESULT)
