from collections.abc import AsyncIterator

from anthropic.types import ToolParam
from anthropic import AsyncAnthropic
from core.config import settings
from llm.types import LLMRawChunk


class LLMClient:
    def __init__(self):
        self.model = settings.anthropic_model
        self.api_key = settings.anthropic_api_key
        self.base_url = settings.anthropic_base_url

    async def stream(
        self,
        messages: list[dict],
        system_prompt: str | None = None,
        tools: list[ToolParam] | None = None,
    ) -> AsyncIterator[LLMRawChunk]:

        client = AsyncAnthropic(base_url=self.base_url, api_key=self.api_key)

        async with client.messages.stream(
            max_tokens=131072,
            model=self.model,
            system=system_prompt or "",
            messages=messages,
            tools=tools or [],
        ) as stream:
            async for event in stream:
                yield (
                    event.model_dump(mode="json", warnings=False)
                    if hasattr(event, "model_dump")
                    else {"type": str(event)}
                )

            final = await stream.get_final_message()

            usage = None
            if hasattr(final, "usage") and final.usage is not None:
                usage = final.usage.model_dump(mode="json", warnings=False)
            yield {
                "type": "message_final",
                "stop_reason": final.stop_reason,
                "usage": usage,
                "content": [
                    block.model_dump(mode="json", warnings=False)
                    for block in final.content
                ],
            }

