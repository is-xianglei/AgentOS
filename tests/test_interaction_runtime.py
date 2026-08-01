"""人工交互运行时的挂起、重放、取消与错误隔离测试。"""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest

from core.errors import AgentException
from core.event_bus import StreamBus
from core.events import ORCHESTRATOR_ACTOR
from interaction.schemas import ToolApprovalResponsePayload
from llm.types import ToolResultMessage, ToolUse
# 先初始化 tools 包，避免 session.repository 导入 tools.models 时触发包级循环导入。
from tools.registry import build_tool_registry
from interaction.service import InteractionService
from runtime.agent import AgentRuntime, SuspendedInteraction, TurnLoopResult
from session.service import SessionService


def _question_input() -> dict[str, object]:
    return {
        "questions": [
            {
                "question": "选择哪种方案？",
                "header": "方案",
                "options": [
                    {"label": "方案A", "description": "采用方案A"},
                    {"label": "方案B", "description": "采用方案B"},
                ],
                "multiSelect": False,
            }
        ]
    }


def _tool_runtime(active_plan: object | None = None) -> AgentRuntime:
    runtime = object.__new__(AgentRuntime)
    runtime.tool_registry = build_tool_registry()
    runtime.plan_service = SimpleNamespace(get_active=AsyncMock(return_value=active_plan))
    runtime.permission_service = SimpleNamespace(evaluate=AsyncMock(return_value="allow"))
    runtime._record_tool_response = AsyncMock()
    runtime._execute_one = AsyncMock(return_value="ok")
    runtime._suspend_for_interaction = AsyncMock()
    return runtime


@pytest.mark.anyio
async def test_非法交互工具参数作为工具错误返回而非击穿Turn() -> None:
    runtime = _tool_runtime()
    done_ids: list[str] = []

    result = await runtime._execute_tool_uses(
        1,
        SimpleNamespace(id=1),
        SimpleNamespace(id=uuid4()),
        ORCHESTRATOR_ACTOR,
        10,
        [ToolUse(id="tool-1", name="AskUserQuestion", input={"questions": []})],
        done_ids,
        None,
    )

    assert result is None
    assert done_ids == ["tool-1"]
    runtime._record_tool_response.assert_awaited_once()
    assert runtime._record_tool_response.await_args.kwargs["is_error"] is True
    runtime.permission_service.evaluate.assert_not_awaited()
    runtime._suspend_for_interaction.assert_not_awaited()


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("tool_use", "active_plan", "expected_kind"),
    [
        (ToolUse("ask", "AskUserQuestion", _question_input()), None, "user_question"),
        (ToolUse("enter", "EnterPlanMode", {}), None, "enter_plan_mode"),
        (
            ToolUse("exit", "ExitPlanMode", {}),
            SimpleNamespace(content="# 计划"),
            "exit_plan_mode",
        ),
    ],
)
async def test_强制交互工具不会被权限allow绕过(
    tool_use: ToolUse,
    active_plan: object | None,
    expected_kind: str,
) -> None:
    runtime = _tool_runtime(active_plan)
    suspended = SuspendedInteraction(1, uuid4(), uuid4(), expected_kind, {}, 1)
    runtime._suspend_for_interaction.return_value = suspended

    result = await runtime._execute_tool_uses(
        1,
        SimpleNamespace(id=1),
        SimpleNamespace(id=uuid4()),
        ORCHESTRATOR_ACTOR,
        10,
        [tool_use],
        [],
        None,
    )

    assert result is suspended
    runtime.permission_service.evaluate.assert_not_awaited()
    assert runtime._suspend_for_interaction.await_args.args[8] == expected_kind


@pytest.mark.anyio
async def test_PlanMode在运行时拒绝普通写工具() -> None:
    runtime = _tool_runtime(SimpleNamespace(content="# 计划"))
    done_ids: list[str] = []

    result = await runtime._execute_tool_uses(
        1,
        SimpleNamespace(id=1),
        SimpleNamespace(id=uuid4()),
        ORCHESTRATOR_ACTOR,
        10,
        [ToolUse("bash", "Bash", {"command": "pwd"})],
        done_ids,
        None,
    )

    assert result is None
    assert done_ids == ["bash"]
    runtime._record_tool_response.assert_awaited_once()
    runtime.permission_service.evaluate.assert_not_awaited()
    runtime._execute_one.assert_not_awaited()


@pytest.mark.anyio
async def test_响应校验失败不会把原Turn标成失败() -> None:
    runtime = object.__new__(AgentRuntime)
    runtime.db = SimpleNamespace(rollback=AsyncMock())
    runtime.bus = StreamBus()
    runtime.prepare_interaction_response = AsyncMock(
        side_effect=AgentException.message("人工交互响应无效")
    )
    runtime._mark_abnormal_end = AsyncMock()

    await runtime._produce_resume(1, uuid4(), "user_question", {"decision": "submit"})
    events = [event async for event in runtime.bus.stream()]

    runtime.db.rollback.assert_awaited_once()
    runtime._mark_abnormal_end.assert_not_awaited()
    assert events[0]["type"] == "error"
    assert events[0]["error"] == {
        "code": "INTERACTION_RESPONSE_INVALID",
        "message": "人工交互响应无效",
        "retriable": True,
        "fatal": True,
    }


@pytest.mark.anyio
async def test_顺序交互重放只在提交后重发当前SSE且不重复执行() -> None:
    runtime = object.__new__(AgentRuntime)
    order: list[str] = []

    async def record_commit() -> None:
        order.append("commit")

    async def record_emit(_interaction: SuspendedInteraction) -> None:
        order.append("emit")

    turn_id = uuid4()
    session = SimpleNamespace(id=1, title="测试", status="awaiting_interaction")
    turn = SimpleNamespace(
        id=turn_id,
        session_id=1,
        status="awaiting_interaction",
        user_id=9,
        workspace_id=7,
    )
    suspension = SimpleNamespace(id=uuid4(), turn_id=turn_id, status="pending")
    request = SimpleNamespace(
        id=uuid4(),
        suspension_id=suspension.id,
        kind="user_question",
        response_payload={"decision": "submit", "answers": {}},
    )
    snapshot = SuspendedInteraction(1, suspension.id, uuid4(), "enter_plan_mode", {}, 1)
    runtime.user_id = 9
    runtime.workspace_id = 7
    runtime.db = SimpleNamespace(commit=AsyncMock(side_effect=record_commit))
    runtime.bus = StreamBus()
    runtime.session_service = SimpleNamespace(
        lock_required=AsyncMock(return_value=session),
        get_turn_required=AsyncMock(return_value=turn),
    )
    runtime.interaction_service = SimpleNamespace(
        get_request_required=AsyncMock(return_value=request),
        lock_suspension_required=AsyncMock(return_value=suspension),
    )
    runtime._get_pending_interaction = AsyncMock(return_value=snapshot)
    runtime._emit_interaction = AsyncMock(side_effect=record_emit)
    runtime._apply_interaction_and_continue = AsyncMock()

    await runtime._produce_resume(
        1,
        uuid4(),
        "user_question",
        {"decision": "submit", "answers": {}},
        response_prepared=True,
    )

    assert order == ["commit", "emit"]
    runtime._apply_interaction_and_continue.assert_not_awaited()


@pytest.mark.anyio
async def test_终态暂停点重放不会再次应用continuation() -> None:
    runtime = object.__new__(AgentRuntime)
    turn_id = uuid4()
    suspension_id = uuid4()
    session = SimpleNamespace(id=1, title="测试", status="idle")
    turn = SimpleNamespace(
        id=turn_id,
        session_id=1,
        status="completed",
        user_id=9,
        workspace_id=7,
    )
    request = SimpleNamespace(
        id=uuid4(),
        suspension_id=suspension_id,
        kind="user_question",
        response_payload={"decision": "submit", "answers": {}},
    )
    suspension = SimpleNamespace(
        id=suspension_id,
        turn_id=turn_id,
        status="resolved",
    )
    runtime.user_id = 9
    runtime.workspace_id = 7
    runtime.db = SimpleNamespace(commit=AsyncMock(), rollback=AsyncMock())
    runtime.bus = StreamBus()
    runtime.session_service = SimpleNamespace(
        lock_required=AsyncMock(return_value=session),
        get_turn_required=AsyncMock(return_value=turn),
    )
    runtime.interaction_service = SimpleNamespace(
        get_request_required=AsyncMock(return_value=request),
        lock_suspension_required=AsyncMock(return_value=suspension),
    )
    runtime._apply_interaction_and_continue = AsyncMock()
    runtime._mark_abnormal_end = AsyncMock()

    await runtime._produce_resume(
        1,
        request.id,
        "user_question",
        request.response_payload,
        response_prepared=True,
    )
    events = [event async for event in runtime.bus.stream()]

    assert [event["type"] for event in events] == ["session_ready", "turn_end"]
    assert events[-1]["stop_reason"] == "interaction_resolved"
    runtime._apply_interaction_and_continue.assert_not_awaited()
    runtime._mark_abnormal_end.assert_not_awaited()


@pytest.mark.anyio
async def test_取消已提交后SSE断开不会覆盖终态() -> None:
    runtime = object.__new__(AgentRuntime)
    turn_id = uuid4()
    suspension_id = uuid4()
    session = SimpleNamespace(
        id=1,
        title="测试",
        status="awaiting_interaction",
    )
    turn = SimpleNamespace(
        id=turn_id,
        session_id=1,
        status="awaiting_interaction",
        user_id=9,
        workspace_id=7,
    )
    request = SimpleNamespace(
        id=uuid4(),
        suspension_id=suspension_id,
        kind="user_question",
        response_payload={"decision": "cancel", "answers": {}},
    )
    suspension = SimpleNamespace(
        id=suspension_id,
        turn_id=turn_id,
        status="resuming",
    )
    runtime.user_id = 9
    runtime.workspace_id = 7
    runtime.db = SimpleNamespace(commit=AsyncMock(), rollback=AsyncMock())
    runtime.bus = SimpleNamespace(
        emit=AsyncMock(side_effect=asyncio.CancelledError),
        close=AsyncMock(),
    )
    runtime.session_service = SimpleNamespace(
        lock_required=AsyncMock(return_value=session),
        get_turn_required=AsyncMock(return_value=turn),
    )
    runtime.interaction_service = SimpleNamespace(
        get_request_required=AsyncMock(return_value=request),
        lock_suspension_required=AsyncMock(return_value=suspension),
        cancel_suspension=AsyncMock(
            return_value=(SimpleNamespace(id=suspension_id), [])
        ),
    )
    runtime._finish_cancelled_interaction = AsyncMock()
    runtime._mark_abnormal_end = AsyncMock()

    with pytest.raises(asyncio.CancelledError):
        await runtime._produce_resume(
            1,
            request.id,
            "user_question",
            request.response_payload,
            response_prepared=True,
        )

    runtime.db.rollback.assert_awaited_once()
    runtime._mark_abnormal_end.assert_not_awaited()


@pytest.mark.anyio
async def test_首次挂起已提交后SSE断开不会破坏暂停点(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime = object.__new__(AgentRuntime)
    session = SimpleNamespace(id=1, title="测试", status="running")
    turn = SimpleNamespace(id=uuid4())
    interaction = SuspendedInteraction(
        1,
        uuid4(),
        uuid4(),
        "user_question",
        {},
        1,
    )
    runtime.user_id = 9
    runtime.workspace_id = 7
    runtime.db = SimpleNamespace(commit=AsyncMock(), rollback=AsyncMock())
    runtime.bus = SimpleNamespace(emit=AsyncMock(), close=AsyncMock())
    runtime.session_service = SimpleNamespace(
        prepare_for_message=AsyncMock(return_value=session),
        start_turn=AsyncMock(return_value=(turn, SimpleNamespace())),
    )
    runtime._apply_user_prompt_hooks = AsyncMock(return_value=None)
    runtime._run_llm_loop = AsyncMock(
        return_value=TurnLoopResult(
            suspended=True,
            interaction=interaction,
        )
    )
    runtime._emit_interaction = AsyncMock(side_effect=asyncio.CancelledError)
    runtime._mark_abnormal_end = AsyncMock()
    task_service = SimpleNamespace(emit_snapshot=AsyncMock())
    monkeypatch.setattr(
        "runtime.agent.TaskService",
        lambda *args, **kwargs: task_service,
    )

    with pytest.raises(asyncio.CancelledError):
        await runtime._produce(None, "请给出方案")

    assert runtime.db.commit.await_count == 2
    runtime.db.rollback.assert_awaited_once()
    runtime._mark_abnormal_end.assert_not_awaited()


def test_错误工具结果加载模型上下文时保留is_error() -> None:
    message = SimpleNamespace(
        id=1,
        role="tool",
        content=ToolResultMessage(
            tool_use_id="tool-1",
            tool_name="AskUserQuestion",
            input_args={},
            output="用户取消了问题交互。",
            is_error=True,
        ).to_content_dict(),
    )

    context = object.__new__(SessionService)._messages_to_context([message])

    assert context == [
        {
            "role": "user",
            "content": [
                {
                    "type": "tool_result",
                    "tool_use_id": "tool-1",
                    "content": "用户取消了问题交互。",
                    "is_error": True,
                }
            ],
        }
    ]


def _resolved_approval(response_payload: dict[str, object]) -> SimpleNamespace:
    return SimpleNamespace(
        id=uuid4(),
        suspension_id=uuid4(),
        session_id=1,
        kind="tool_approval",
        status="resolved",
        response_payload=response_payload,
        request_payload={},
    )


@pytest.mark.anyio
async def test_相同响应可在下一项顺序交互期间幂等重放() -> None:
    normalized = ToolApprovalResponsePayload.model_validate(
        {"decision": "allow_once"}
    ).model_dump(mode="json", by_alias=True)
    request = _resolved_approval(normalized)
    suspension = SimpleNamespace(id=request.suspension_id, status="pending")
    repo = SimpleNamespace(
        get_request=AsyncMock(return_value=request),
        get_suspension_for_update=AsyncMock(return_value=suspension),
        lock_pending_request=AsyncMock(),
    )
    service = object.__new__(InteractionService)
    service.repo = repo

    actual_request, actual_suspension = await service.resolve_request(
        request.id,
        {"decision": "allow_once"},
        responded_by=1,
        session_id=1,
        expected_kind="tool_approval",
    )

    assert actual_request is request
    assert actual_suspension is suspension
    repo.lock_pending_request.assert_not_awaited()


@pytest.mark.anyio
async def test_已解决请求使用不同响应重放时返回409() -> None:
    normalized = ToolApprovalResponsePayload.model_validate(
        {"decision": "allow_once"}
    ).model_dump(mode="json", by_alias=True)
    request = _resolved_approval(normalized)
    service = object.__new__(InteractionService)
    service.repo = SimpleNamespace(
        get_request=AsyncMock(return_value=request),
        get_suspension_for_update=AsyncMock(
            return_value=SimpleNamespace(id=request.suspension_id, status="resuming")
        ),
    )

    with pytest.raises(AgentException) as exc_info:
        await service.resolve_request(
            request.id,
            {"decision": "deny"},
            responded_by=1,
            session_id=1,
            expected_kind="tool_approval",
        )

    assert exc_info.value.status_code == 409


@pytest.mark.anyio
async def test_取消交互会补齐错误工具结果并中止Turn() -> None:
    runtime = object.__new__(AgentRuntime)
    record = SimpleNamespace(
        id=5,
        status="awaiting_interaction",
        tool_name="AskUserQuestion",
        input_args=_question_input(),
    )
    runtime.tool_service = SimpleNamespace(
        get_interaction_call_required=AsyncMock(return_value=record)
    )
    runtime._record_tool_response = AsyncMock()
    repo = SimpleNamespace(update_status=AsyncMock())
    runtime.session_service = SimpleNamespace(
        mark_turn_status=AsyncMock(),
        repo=repo,
    )
    session = SimpleNamespace(id=1)
    turn = SimpleNamespace(id=uuid4())
    request = SimpleNamespace(tool_call_id=5, tool_use_id="ask-1")

    await runtime._finish_cancelled_interaction(
        session,
        turn,
        [request],
        "用户取消了问题交互。",
    )

    runtime._record_tool_response.assert_awaited_once()
    tool_use = runtime._record_tool_response.await_args.args[3]
    assert tool_use.id == "ask-1"
    assert runtime._record_tool_response.await_args.kwargs == {
        "is_error": True,
        "record": record,
    }
    runtime.session_service.mark_turn_status.assert_awaited_once_with(turn, "interrupted")
    repo.update_status.assert_awaited_once_with(session, "idle")
