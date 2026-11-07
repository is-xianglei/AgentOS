"""AgentStep 单步执行契约测试。"""

from collections.abc import AsyncIterator
from typing import Any

import pytest

from core.events import ORCHESTRATOR_ACTOR, BlockType, ToolInfo
from harness.app.ports import ModelEventKind, ModelSettings, ModelStreamEvent, ModelTurnResult
from harness.app.tool_set import ToolSet
from harness.domain.context import Context
from harness.domain.messages import ToolCall
from runtime.step import AgentStep, StepRequest
from runtime.stream_assembler import TurnEventAssembler


class _FakeLLM:
    def __init__(self, events: list[ModelStreamEvent]) -> None:
        self.events = events
        self.calls: list[dict[str, Any]] = []

    def stream(self, context: Context[None], settings: ModelSettings) -> AsyncIterator:
        self.calls.append(
            {
                "messages": context.messages,
                "system_prompt": context.instruction,
                "tools": list(context.tools),
                "model": settings.model_id,
            }
        )

        async def generate():
            for event in self.events:
                yield event

        return generate()


def _text_events(result: ModelTurnResult) -> list[ModelStreamEvent]:
    return [
        ModelStreamEvent(ModelEventKind.BLOCK_START, block_ref=0, block_type=BlockType.TEXT),
        ModelStreamEvent(ModelEventKind.TEXT_DELTA, block_ref=0, text=result.text),
        ModelStreamEvent(ModelEventKind.BLOCK_STOP, block_ref=0),
        ModelStreamEvent(ModelEventKind.TURN_RESULT, result=result),
    ]


@pytest.mark.anyio
async def test_agent_step_assembles_stream_and_neutral_blocks() -> None:
    result = ModelTurnResult(text="完成")
    llm = _FakeLLM(_text_events(result))
    emitted = []

    # emit 需要异步函数，使用显式收集器避免把事件逻辑放入 AgentStep。
    async def emit(event):
        emitted.append(event)

    step = AgentStep(
        llm,
        TurnEventAssembler(ORCHESTRATOR_ACTOR, 1, turn_id=None),
        emit,
    )
    actual = await step.run(
        StepRequest(
            messages=[{"role": "user", "content": "你好"}],
            system_prompt="系统",
            tool_registry=ToolSet(),
            settings=ModelSettings(model_id="test-model"),
        )
    )

    assert actual.llm_result == result
    assert actual.assistant_blocks == [{"kind": "text", "text": "完成"}]
    assert [event.type.value for event in emitted] == [
        "block_start",
        "text_delta",
        "block_stop",
    ]
    assert llm.calls[0]["model"] == "test-model"
    assert llm.calls[0]["tools"] == []


@pytest.mark.anyio
async def test_agent_step_returns_tool_uses_in_result() -> None:
    tool_call = ToolCall(id="call-1", tool_name="Read", arguments={"path": "a.py"})
    result = ModelTurnResult(text="", tool_calls=[tool_call])
    events = [
        ModelStreamEvent(ModelEventKind.BLOCK_START, block_ref=0, block_type=BlockType.TOOL_USE),
        ModelStreamEvent(
            ModelEventKind.TOOL_USE,
            block_ref=0,
            tool=ToolInfo(id="call-1", name="Read", input={"path": "a.py"}),
        ),
        ModelStreamEvent(ModelEventKind.TURN_RESULT, result=result),
    ]
    llm = _FakeLLM(events)

    async def emit(_event):
        return None

    actual = await AgentStep(
        llm,
        TurnEventAssembler(ORCHESTRATOR_ACTOR, 1, turn_id=None),
        emit,
    ).run(StepRequest([], None, ToolSet(), ModelSettings(model_id="")))

    assert actual.tool_calls == [tool_call]
    assert actual.assistant_blocks == [
        {"kind": "tool_use", "id": "call-1", "name": "Read", "input": {"path": "a.py"}}
    ]
