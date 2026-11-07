"""模型客户端测试辅助：按中立 dict 消息发起调用，以及固定取值的思考档位映射表。

生产代码经解析器拿到客户端与 ModelSettings；单测直接构造客户端时用这里的帮手，
避免每个用例都手写 Context 与 ModelSettings。
"""

from collections.abc import AsyncIterator
from typing import Any

from pydantic import BaseModel

from harness.adapters.models.reasoning import ReasoningLevels, ThinkingLevel
from harness.app.ports import Effort, ModelClient, ModelSettings, ModelStreamEvent
from harness.domain.context import Context, PromptBlock, ToolSpec, messages_from_rows


def thinking_off() -> ReasoningLevels:
    """工作区端点：任何档位都关闭思考（对应原 thinking_enabled=False）。"""
    return ReasoningLevels({effort: ThinkingLevel(enabled=False) for effort in Effort})


def thinking_on(value: str | int | None) -> ReasoningLevels:
    """工作区端点：任何档位都开启思考并下发同一取值（对应原 thinking_enabled=True）。"""
    return ReasoningLevels({effort: ThinkingLevel(enabled=True, value=value) for effort in Effort})


def stream_messages(
    client: ModelClient,
    messages: list[dict[str, Any]],
    *,
    instruction: str | list[PromptBlock] | None = None,
    tools: list[ToolSpec] | None = None,
    settings: ModelSettings | None = None,
) -> AsyncIterator[ModelStreamEvent]:
    """以中立 dict 消息发起流式调用；未给出 settings 时用客户端自带的默认模型名。"""
    if isinstance(instruction, list):
        instruction = tuple(instruction)
    context: Context[None] = Context(
        instruction=instruction,
        messages=messages_from_rows(messages),
        tools=tuple(tools or ()),
    )
    return client.stream(context, settings or ModelSettings(model_id=client.model))


async def structured[O: BaseModel](
    client: ModelClient,
    messages: list[dict[str, Any]],
    output_type: type[O],
    *,
    instruction: str | None = None,
    settings: ModelSettings | None = None,
) -> O:
    """以中立 dict 消息发起结构化调用；未给出 settings 时用客户端自带的默认模型名。"""
    context = Context(
        instruction=instruction,
        messages=messages_from_rows(messages),
        output_type=output_type,
    )
    return await client.complete_structured(
        context, settings or ModelSettings(model_id=client.model)
    )
