"""AgentStep 单步执行契约测试。"""

from collections.abc import AsyncIterator
from typing import Any

import pytest

from core.events import BlockType, ORCHESTRATOR_ACTOR, ToolInfo
from llm.types import LLMEventKind, LLMStreamEvent, LLMTurnResult, ToolUse
from runtime.step import AgentStep, StepRequest
from runtime.stream_assembler import TurnEventAssembler
from tools.registry import ToolRegistry


class _FakeLLM:
    def __init__(self, events: list[LLMStreamEvent]) -> None:
        self.events = events
        self.calls: list[dict[str, Any]] = []

    def stream(self, messages, system_prompt=None, tools=None, model=None) -> AsyncIterator:
        self.calls.append(
            {
                "messages": messages,
                "system_prompt": system_prompt,
                "tools": tools,
                "model": model,
            }
        )

        async def generate():
            for event in self.events:
                yield event

        return generate()


def _text_events(result: LLMTurnResult) -> list[LLMStreamEvent]:
    return [
        LLMStreamEvent(LLMEventKind.BLOCK_START, block_ref=0, block_type=BlockType.TEXT),
        LLMStreamEvent(LLMEventKind.TEXT_DELTA, block_ref=0, text=result.text),
        LLMStreamEvent(LLMEventKind.BLOCK_STOP, block_ref=0),
        LLMStreamEvent(LLMEventKind.TURN_RESULT, result=result),
    ]


@pytest.mark.anyio
async def test_agent_step_assembles_stream_and_neutral_blocks() -> None:
    result = LLMTurnResult(text="完成")
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
            tool_registry=ToolRegistry([]),
            model="test-model",
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
    tool_use = ToolUse(id="call-1", name="Read", input={"path": "a.py"})
    result = LLMTurnResult(text="", tool_uses=[tool_use])
    events = [
        LLMStreamEvent(LLMEventKind.BLOCK_START, block_ref=0, block_type=BlockType.TOOL_USE),
        LLMStreamEvent(
            LLMEventKind.TOOL_USE,
            block_ref=0,
            tool=ToolInfo(id="call-1", name="Read", input={"path": "a.py"}),
        ),
        LLMStreamEvent(LLMEventKind.TURN_RESULT, result=result),
    ]
    llm = _FakeLLM(events)

    async def emit(_event):
        return None

    actual = await AgentStep(
        llm,
        TurnEventAssembler(ORCHESTRATOR_ACTOR, 1, turn_id=None),
        emit,
    ).run(StepRequest([], None, ToolRegistry([])))

    assert actual.tool_uses == [tool_use]
    assert actual.assistant_blocks == [
        {"kind": "tool_use", "id": "call-1", "name": "Read", "input": {"path": "a.py"}}
    ]
