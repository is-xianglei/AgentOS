"""人工交互与 Plan Mode 的真实 PostgreSQL 持久化测试。"""

from types import SimpleNamespace
from typing import Any
from uuid import uuid4

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

import database.registry  # noqa: F401
from core.errors import AgentException
from database.engine import engine
from interaction.schemas import InteractionRequestCreate
from interaction.service import InteractionService
from plan.service import PlanService
from runtime.agent import AgentRuntime
from session.service import SessionService
from tools.repository import ToolRepository
from user.models import UserRecord
from workspace.models import WorkspaceRecord


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


@pytest.fixture
async def db() -> Any:
    """每个用例使用外层事务，结束后整体回滚，不污染开发库。"""
    async with engine.connect() as connection:
        transaction = await connection.begin()
        session = AsyncSession(bind=connection, expire_on_commit=False)
        try:
            yield session
        finally:
            await session.close()
            await transaction.rollback()


@pytest.fixture
async def waiting_turn(db: AsyncSession) -> SimpleNamespace:
    suffix = uuid4().hex
    user = UserRecord(
        email=f"interaction-{suffix}@example.invalid",
        username=f"interaction-{suffix[:16]}",
        password="test-only-hash",
    )
    workspace = WorkspaceRecord(
        name=f"interaction-{suffix}",
        slug=f"interaction-{suffix}",
        display_name="人工交互测试",
    )
    db.add_all([user, workspace])
    await db.flush()

    sessions = SessionService(db)
    session = await sessions.create(
        title="人工交互测试",
        user_id=user.id,
        workspace_id=workspace.id,
    )
    turn, _ = await sessions.start_turn(
        session,
        user.id,
        workspace.id,
        "测试人工交互",
    )
    await sessions.repo.update_status(session, "awaiting_interaction")
    await sessions.mark_turn_status(turn, "awaiting_interaction")
    assistant = await sessions.add_message(
        session.id,
        "assistant",
        [{"type": "tool_use", "id": "ask-1", "name": "AskUserQuestion", "input": {}}],
        turn_id=turn.id,
    )
    tool_call = await ToolRepository(db).start(
        session.id,
        "AskUserQuestion",
        {"questions": []},
        message_id=assistant.id,
    )
    await ToolRepository(db).mark_awaiting_interaction(tool_call)
    return SimpleNamespace(
        user=user,
        workspace=workspace,
        session=session,
        turn=turn,
        assistant=assistant,
        tool_call=tool_call,
    )


def _question_payload() -> dict[str, object]:
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


@pytest.mark.anyio
async def test_同一暂停点顺序请求与响应幂等往返(
    db: AsyncSession,
    waiting_turn: SimpleNamespace,
) -> None:
    context = waiting_turn
    service = InteractionService(db)
    suspension, request = await service.create_suspension_with_request(
        context.session.id,
        context.turn.id,
        {
            "assistant_message_id": context.assistant.id,
            "pending": [{"tool_use_id": "ask-1", "tool_name": "AskUserQuestion"}],
        },
        {"mode": "sequential"},
        InteractionRequestCreate(
            kind="user_question",
            request_payload=_question_payload(),
            tool_call_id=context.tool_call.id,
            tool_use_id="ask-1",
        ),
    )
    response = {
        "decision": "submit",
        "answers": {"选择哪种方案？": "方案A"},
        "annotations": {"选择哪种方案？": {"notes": "优先稳定性"}},
    }

    resolved, suspension = await service.resolve_request(
        request.id,
        response,
        context.user.id,
        session_id=context.session.id,
        expected_kind="user_question",
    )
    assert resolved.status == "resolved"
    assert resolved.response_payload["answers"] == {"选择哪种方案？": "方案A"}
    assert suspension.status == "resuming"

    replayed, replay_suspension = await service.resolve_request(
        request.id,
        response,
        context.user.id,
        session_id=context.session.id,
        expected_kind="user_question",
    )
    assert replayed.id == resolved.id
    assert replay_suspension.id == suspension.id

    next_tool_call = await ToolRepository(db).start(
        context.session.id,
        "EnterPlanMode",
        {},
        message_id=context.assistant.id,
    )
    await ToolRepository(db).mark_awaiting_interaction(next_tool_call)
    suspension, next_request = await service.continue_suspension_with_request(
        suspension.id,
        {
            "assistant_message_id": context.assistant.id,
            "pending": [{"tool_use_id": "enter-1", "tool_name": "EnterPlanMode"}],
        },
        InteractionRequestCreate(
            kind="enter_plan_mode",
            request_payload={"message": "是否进入只读Plan Mode？"},
            tool_call_id=next_tool_call.id,
            tool_use_id="enter-1",
        ),
    )
    assert suspension.status == "pending"
    assert next_request.status == "pending"
    ordered_requests = await service.repo.list_requests(suspension.id)
    assert [item.sequence for item in ordered_requests] == [1, 2]

    suspension, requests = await service.cancel_suspension(
        suspension.id,
        context.user.id,
        session_id=context.session.id,
    )
    assert suspension.status == "cancelled"
    assert suspension.resolved_at is not None
    assert [item.status for item in requests] == ["resolved", "cancelled"]


@pytest.mark.anyio
async def test_不同响应重放返回409(
    db: AsyncSession,
    waiting_turn: SimpleNamespace,
) -> None:
    context = waiting_turn
    service = InteractionService(db)
    _, request = await service.create_suspension_with_request(
        context.session.id,
        context.turn.id,
        {"pending": [{"tool_use_id": "ask-1"}]},
        {"mode": "sequential"},
        InteractionRequestCreate(
            kind="user_question",
            request_payload=_question_payload(),
            tool_call_id=context.tool_call.id,
            tool_use_id="ask-1",
        ),
    )
    await service.resolve_request(
        request.id,
        {"decision": "submit", "answers": {"选择哪种方案？": "方案A"}},
        context.user.id,
    )

    with pytest.raises(AgentException) as exc_info:
        await service.resolve_request(
            request.id,
            {"decision": "submit", "answers": {"选择哪种方案？": "方案B"}},
            context.user.id,
        )

    assert exc_info.value.status_code == 409


@pytest.mark.anyio
async def test_PlanMode正文反馈和批准完整持久化(
    db: AsyncSession,
    waiting_turn: SimpleNamespace,
) -> None:
    context = waiting_turn
    service = PlanService(db)
    plan = await service.enter(context.session.id, context.turn.id)
    assert plan.status == "active"
    assert plan.version == 1

    plan = await service.write(context.session.id, "  # 实施计划\n\n1. 编写测试  ")
    assert plan.content == "# 实施计划\n\n1. 编写测试"
    assert plan.version == 2

    plan = await service.reject_exit(
        context.session.id,
        "补充回滚步骤",
        "# 实施计划\n\n1. 编写测试\n2. 验证回滚",
    )
    assert plan.status == "active"
    assert plan.feedback == "补充回滚步骤"
    assert plan.version == 3

    allowed_prompts = [{"tool": "Bash", "prompt": "运行测试"}]
    plan = await service.approve_exit(
        context.session.id,
        context.turn.id,
        context.user.id,
        allowed_prompts,
        None,
        None,
    )
    assert plan.status == "approved"
    assert plan.allowed_prompts == allowed_prompts
    assert plan.approved_by == context.user.id
    assert plan.approved_at is not None
    assert await service.get_active(context.session.id) is None


@pytest.mark.anyio
async def test_取消暂停点原子收尾Session_Turn和工具结果(
    db: AsyncSession,
    waiting_turn: SimpleNamespace,
) -> None:
    context = waiting_turn
    interactions = InteractionService(db)
    suspension, request = await interactions.create_suspension_with_request(
        context.session.id,
        context.turn.id,
        {"pending": [{"tool_use_id": "ask-1", "tool_name": "AskUserQuestion"}]},
        {"mode": "sequential"},
        InteractionRequestCreate(
            kind="user_question",
            request_payload=_question_payload(),
            tool_call_id=context.tool_call.id,
            tool_use_id="ask-1",
        ),
    )

    cancelled = await AgentRuntime(
        db,
        user_id=context.user.id,
        workspace_id=context.workspace.id,
    ).cancel_interaction(context.session.id, suspension.id)

    assert cancelled.status == "cancelled"
    assert request.status == "cancelled"
    assert context.session.status == "idle"
    assert context.turn.status == "interrupted"
    assert context.tool_call.status == "rejected"
    messages = await SessionService(db).list_messages(context.session.id)
    tool_message = messages[-1]
    assert tool_message.role == "tool"
    assert tool_message.content["tool_use_id"] == "ask-1"
    assert tool_message.content["is_error"] is True
