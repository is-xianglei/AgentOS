"""ReAct 循环骨架单元测试：用假策略验证骨架的控制流，不涉及任何运行时与 feature 域。"""

from dataclasses import dataclass, field

import pytest

from harness.app.loop import (
    LoopExit,
    LoopPolicy,
    LoopRound,
    ModelRequest,
    StepResult,
    run_react_loop,
)
from harness.app.ports import ModelSettings, ModelTurnResult
from harness.domain.context import Context
from harness.domain.messages import ToolCall

pytestmark = pytest.mark.anyio

_SETTINGS = ModelSettings(model_id="fake-model")


def _reply(text: str = "", calls: tuple[str, ...] = ()) -> StepResult:
    tool_calls = [ToolCall(id=call_id, tool_name="echo") for call_id in calls]
    return StepResult(
        llm_result=ModelTurnResult(text=text, tool_calls=tool_calls),
        assistant_blocks=[],
    )


@dataclass
class _ScriptedStep:
    """按脚本依次返回回复，并记下每次收到的上下文。"""

    replies: list[StepResult]
    contexts: list[Context[None]] = field(default_factory=list)

    async def run(self, context: Context[None], settings: ModelSettings) -> StepResult:
        self.contexts.append(context)
        return self.replies.pop(0)


class _RecordingPolicy(LoopPolicy[str]):
    """三个假策略的共同部分：按骨架调用顺序记下每个钩子；工具阶段默认继续，耗尽返回截断。"""

    def __init__(self) -> None:
        self.calls: list[str] = []

    async def before_round(self, loop_round: LoopRound) -> None:
        self.calls.append(f"before:{loop_round.index}:{loop_round.is_last}")

    async def load_context(self, loop_round: LoopRound) -> ModelRequest:
        self.calls.append(f"load:{loop_round.index}")
        return ModelRequest(context=Context(instruction=None, messages=()), settings=_SETTINGS)

    async def record_assistant(self, loop_round: LoopRound, reply: StepResult) -> None:
        self.calls.append(f"record:{loop_round.index}")

    async def on_no_tool_calls(
        self,
        loop_round: LoopRound,
        reply: StepResult,
    ) -> LoopExit[str] | None:
        self.calls.append(f"no_tools:{loop_round.index}")
        return LoopExit(f"answer:{reply.text}")

    async def execute_tools(self, loop_round: LoopRound, reply: StepResult) -> LoopExit[str] | None:
        self.calls.append(f"tools:{loop_round.index}:{len(reply.tool_calls)}")
        return None

    async def flush(self) -> None:
        self.calls.append("flush")

    async def on_exhausted(self) -> str:
        self.calls.append("exhausted")
        return "truncated"


class _AnswerPolicy(_RecordingPolicy):
    """没有工具调用就以模型文本作答结束。"""


class _TruncatingPolicy(_RecordingPolicy):
    """轮次耗尽时返回截断结果（子代理、队友的形态）。"""


class _SuspendingPolicy(_RecordingPolicy):
    """工具阶段在指定轮次挂起，挂起即中止循环（主 Agent 等待人工交互的形态）。"""

    def __init__(self, suspend_at: int) -> None:
        super().__init__()
        self._suspend_at = suspend_at

    async def execute_tools(self, loop_round: LoopRound, reply: StepResult) -> LoopExit[str] | None:
        await super().execute_tools(loop_round, reply)
        if loop_round.index == self._suspend_at:
            return LoopExit("suspended")
        return None


class _ContinueOncePolicy(_RecordingPolicy):
    """第一次没有工具调用时要求续跑（主 Agent 的 Stop hook 形态），之后正常结束。"""

    def __init__(self) -> None:
        super().__init__()
        self._continued = False

    async def on_no_tool_calls(
        self,
        loop_round: LoopRound,
        reply: StepResult,
    ) -> LoopExit[str] | None:
        if self._continued:
            return await super().on_no_tool_calls(loop_round, reply)
        self._continued = True
        self.calls.append(f"no_tools:{loop_round.index}")
        return None


async def test_no_tool_calls_ends_the_loop() -> None:
    policy = _AnswerPolicy()
    step = _ScriptedStep([_reply(calls=("t1",)), _reply(text="完成")])

    result = await run_react_loop(policy, step, max_rounds=3)

    assert result == "answer:完成"
    assert policy.calls == [
        "before:0:False",
        "load:0",
        "record:0",
        "tools:0:1",
        "flush",
        "before:1:False",
        "load:1",
        "record:1",
        "no_tools:1",
    ]
    assert len(step.contexts) == 2


async def test_exhausting_rounds_goes_to_on_exhausted() -> None:
    policy = _TruncatingPolicy()
    step = _ScriptedStep([_reply(calls=("t1",)), _reply(calls=("t2",))])

    result = await run_react_loop(policy, step, max_rounds=2)

    assert result == "truncated"
    assert policy.calls == [
        "before:0:False",
        "load:0",
        "record:0",
        "tools:0:1",
        "flush",
        "before:1:True",
        "load:1",
        "record:1",
        "tools:1:1",
        "flush",
        "exhausted",
    ]


async def test_suspending_tool_phase_aborts_immediately() -> None:
    policy = _SuspendingPolicy(suspend_at=0)
    step = _ScriptedStep([_reply(calls=("t1", "t2")), _reply(text="不该被调用")])

    result = await run_react_loop(policy, step, max_rounds=3)

    assert result == "suspended"
    # 中止前照常刷新本轮写入，之后不再调模型、不进收尾。
    assert policy.calls == ["before:0:False", "load:0", "record:0", "tools:0:2", "flush"]
    assert len(step.replies) == 1


async def test_continuation_without_tools_counts_as_a_round() -> None:
    policy = _ContinueOncePolicy()
    step = _ScriptedStep([_reply(text="初稿"), _reply(text="终稿")])

    result = await run_react_loop(policy, step, max_rounds=2)

    assert result == "answer:终稿"
    assert policy.calls == [
        "before:0:False",
        "load:0",
        "record:0",
        "no_tools:0",
        "before:1:True",
        "load:1",
        "record:1",
        "no_tools:1",
    ]


async def test_zero_rounds_go_straight_to_on_exhausted() -> None:
    policy = _TruncatingPolicy()
    step = _ScriptedStep([])

    assert await run_react_loop(policy, step, max_rounds=0) == "truncated"
    assert policy.calls == ["exhausted"]


def test_step_result_exposes_text_and_tool_calls() -> None:
    reply = _reply(text="好", calls=("t1",))

    assert reply.text == "好"
    assert [call.id for call in reply.tool_calls] == ["t1"]
