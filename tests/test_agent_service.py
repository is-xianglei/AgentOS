"""应用层 AgentService：用假的 AgentRunner / WorkflowRunner 验证分发与参数透传。

运行时按调用现建（工厂注入），这里同时断言每次调用各建一个运行时、只建该建的那一种。
"""

from dataclasses import dataclass, field
from types import SimpleNamespace
from typing import Any
from uuid import UUID, uuid4

import pytest

from harness.app.agent_service import AgentService, ExecutionHandle

pytestmark = pytest.mark.anyio

_HANDLE = ExecutionHandle(turn_id=uuid4(), session_id=10, start_offset=3, start_cursor="0-0")
_REQUEST_ID = uuid4()
_SUSPENSION_ID = uuid4()
_TURN_ID = uuid4()


@dataclass
class _Calls:
    """按发生顺序记下运行时的构造与入口调用。"""

    log: list[tuple[str, tuple[Any, ...], dict[str, Any]]] = field(default_factory=list)

    def ops(self) -> list[str]:
        return [op for op, _, _ in self.log]


class _FakeAgentRunner:
    def __init__(self, calls: _Calls) -> None:
        self._calls = calls
        calls.log.append(("agent.init", (), {}))

    async def start(self, *args: Any, **kwargs: Any) -> ExecutionHandle:
        self._calls.log.append(("agent.start", args, kwargs))
        return _HANDLE

    async def start_resume(self, *args: Any, **kwargs: Any) -> ExecutionHandle:
        self._calls.log.append(("agent.start_resume", args, kwargs))
        return _HANDLE

    async def cancel_interaction(self, *args: Any, **kwargs: Any) -> Any:
        self._calls.log.append(("agent.cancel_interaction", args, kwargs))
        return SimpleNamespace(id=args[1], session_id=args[0], turn_id=_TURN_ID, status="cancelled")


class _FakeWorkflowRunner:
    def __init__(self, calls: _Calls) -> None:
        self._calls = calls
        calls.log.append(("workflow.init", (), {}))

    async def start(self, *args: Any, **kwargs: Any) -> ExecutionHandle:
        self._calls.log.append(("workflow.start", args, kwargs))
        return _HANDLE


def _service(calls: _Calls) -> AgentService:
    async def _cancel_turn(session_id: int, turn_id: UUID) -> Any:
        calls.log.append(("cancel_turn", (session_id, turn_id), {}))
        return SimpleNamespace(turn_id=turn_id, status="interrupted", session_status="idle")

    return AgentService(
        agent_runner=lambda: _FakeAgentRunner(calls),
        workflow_runner=lambda: _FakeWorkflowRunner(calls),
        cancel_turn=_cancel_turn,
    )


@pytest.mark.parametrize(
    ("kwargs", "expected"),
    [
        ({}, {"agent_id": None}),
        ({"agent_id": 3}, {"agent_id": 3}),
        ({"agent_id": 3, "model_options": {}}, {"agent_id": 3}),
        (
            {"model_options": {"mode_slug": "turbo"}},
            {"agent_id": None, "model_options": {"mode_slug": "turbo"}},
        ),
    ],
)
async def test_chat_without_workflow_dispatches_agent_runner(
    kwargs: dict[str, Any], expected: dict[str, Any]
) -> None:
    calls = _Calls()

    handle = await _service(calls).chat(10, "你好", **kwargs)

    assert handle is _HANDLE
    assert calls.log == [("agent.init", (), {}), ("agent.start", (10, "你好"), expected)]


@pytest.mark.parametrize(
    ("model_options", "expected"),
    [
        (None, {"workflow_id": 8}),
        ({}, {"workflow_id": 8}),
        ({"thinking_effort": "low"}, {"workflow_id": 8, "model_options": {"thinking_effort": "low"}}),
    ],
)
async def test_chat_with_workflow_dispatches_workflow_runner(
    model_options: dict[str, Any] | None, expected: dict[str, Any]
) -> None:
    calls = _Calls()

    handle = await _service(calls).chat(None, "写周报", workflow_id=8, model_options=model_options)

    assert handle is _HANDLE
    assert calls.log == [("workflow.init", (), {}), ("workflow.start", (None, "写周报"), expected)]


async def test_chat_rejects_agent_and_workflow_together_before_building_runner() -> None:
    calls = _Calls()

    with pytest.raises(ValueError, match="同一次请求不能同时指定Agent和Workflow"):
        await _service(calls).chat(None, "你好", agent_id=1, workflow_id=8)

    assert calls.log == []


async def test_each_call_builds_its_own_runner() -> None:
    calls = _Calls()
    service = _service(calls)

    await service.chat(10, "一")
    await service.chat(10, "二", workflow_id=8)
    await service.chat(10, "三")

    assert calls.ops() == [
        "agent.init",
        "agent.start",
        "workflow.init",
        "workflow.start",
        "agent.init",
        "agent.start",
    ]


async def test_resume_passes_response_to_agent_runner_unchanged() -> None:
    calls = _Calls()
    payload = {"decision": "allow_once", "updated_input": None}

    handle = await _service(calls).resume(10, _REQUEST_ID, "tool_approval", payload)

    assert handle is _HANDLE
    assert calls.log == [
        ("agent.init", (), {}),
        ("agent.start_resume", (10, _REQUEST_ID, "tool_approval", payload), {}),
    ]
    assert calls.log[1][1][3] is payload


async def test_cancel_interaction_returns_runner_result() -> None:
    calls = _Calls()

    suspension = await _service(calls).cancel_interaction(10, _SUSPENSION_ID)

    assert (suspension.id, suspension.session_id, suspension.status) == (
        _SUSPENSION_ID,
        10,
        "cancelled",
    )
    assert calls.log == [
        ("agent.init", (), {}),
        ("agent.cancel_interaction", (10, _SUSPENSION_ID), {}),
    ]


async def test_cancel_turn_uses_injected_canceller_without_runner() -> None:
    calls = _Calls()

    termination = await _service(calls).cancel_turn(10, _TURN_ID)

    assert (termination.turn_id, termination.status, termination.session_status) == (
        _TURN_ID,
        "interrupted",
        "idle",
    )
    assert calls.log == [("cancel_turn", (10, _TURN_ID), {})]


def test_runtime_classes_satisfy_runner_ports() -> None:
    """AgentRuntime / WorkflowRuntime 的入口签名与端口一致：参数名、顺序与仅限关键字参数。"""
    import inspect

    from harness.app.agent_service import AgentRunner, WorkflowRunner
    from runtime.agent import AgentRuntime
    from runtime.workflow import WorkflowRuntime

    def _shape(func: Any) -> list[tuple[str, Any]]:
        return [(p.name, p.kind) for p in inspect.signature(func).parameters.values()]

    for name in ("start", "start_resume", "cancel_interaction"):
        assert _shape(getattr(AgentRuntime, name)) == _shape(getattr(AgentRunner, name)), name
    assert _shape(WorkflowRuntime.start) == _shape(WorkflowRunner.start)
