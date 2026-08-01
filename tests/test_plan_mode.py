"""Plan Mode 工具、服务与运行时切换契约测试。"""

from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from pydantic import ValidationError

from core.errors import AgentException
from core.events import ORCHESTRATOR_ACTOR
from llm.types import ToolUse
# 先初始化 tools 包，避免直接导入 runtime 时触发包级循环导入。
from tools.registry import build_tool_registry
from plan.service import PlanService
from runtime.agent import AgentRuntime
from tools.builtin.plan_mode import (
    EnterPlanModeInput,
    ExitPlanModeInput,
    WritePlanInput,
)


def _service(repo: object) -> PlanService:
    service = object.__new__(PlanService)
    service.repo = repo
    return service


class TestPlanModeInput:
    def test_EnterPlanMode只接受空对象(self) -> None:
        assert EnterPlanModeInput.model_validate({}).model_dump() == {}
        with pytest.raises(ValidationError):
            EnterPlanModeInput.model_validate({"reason": "测试"})

    def test_WritePlan拒绝空白和超长正文(self) -> None:
        with pytest.raises(ValidationError):
            WritePlanInput.model_validate({"plan": "   "})
        with pytest.raises(ValidationError):
            WritePlanInput.model_validate({"plan": "x" * 500_001})

    def test_ExitPlanMode校验allowedPrompts并输出camelCase(self) -> None:
        parsed = ExitPlanModeInput.model_validate(
            {"allowedPrompts": [{"tool": "Bash", "prompt": "  运行测试  "}]}
        )

        assert parsed.allowed_prompts is not None
        assert parsed.allowed_prompts[0].prompt == "运行测试"
        assert parsed.model_dump(by_alias=True) == {
            "allowedPrompts": [{"tool": "Bash", "prompt": "运行测试"}]
        }

    @pytest.mark.parametrize(
        "payload",
        [
            {"allowedPrompts": [{"tool": "Write", "prompt": "写文件"}]},
            {"allowedPrompts": [{"tool": "Bash", "prompt": " "}]},
            {"allowedPrompts": [{"tool": "Bash", "prompt": "测试", "extra": True}]},
        ],
    )
    def test_ExitPlanMode拒绝非法语义权限声明(self, payload: dict[str, object]) -> None:
        with pytest.raises(ValidationError):
            ExitPlanModeInput.model_validate(payload)


@pytest.mark.anyio
class TestPlanService:
    async def test_进入时创建独立active计划(self) -> None:
        created = SimpleNamespace(status="active")
        repo = SimpleNamespace(
            get_active=AsyncMock(return_value=None),
            create=AsyncMock(return_value=created),
        )
        turn_id = uuid4()

        result = await _service(repo).enter(1, turn_id)

        assert result is created
        repo.create.assert_awaited_once_with(1, turn_id, "default")

    async def test_重复进入PlanMode被拒绝(self) -> None:
        repo = SimpleNamespace(get_active=AsyncMock(return_value=SimpleNamespace()))

        with pytest.raises(AgentException, match="已经处于Plan Mode"):
            await _service(repo).enter(1, uuid4())

    async def test_WritePlan仅在active状态写入并规范化正文(self) -> None:
        active = SimpleNamespace(content="")
        saved = SimpleNamespace(content="# 计划")
        repo = SimpleNamespace(
            get_active=AsyncMock(return_value=active),
            update_content=AsyncMock(return_value=saved),
        )

        result = await _service(repo).write(1, "  # 计划  ")

        assert result is saved
        repo.get_active.assert_awaited_once_with(1, for_update=True)
        repo.update_content.assert_awaited_once_with(active, "# 计划")

    async def test_WritePlan在非active状态和空白正文时被拒绝(self) -> None:
        repo = SimpleNamespace(get_active=AsyncMock(return_value=None))
        service = _service(repo)

        with pytest.raises(AgentException, match="不能为空"):
            await service.write(1, "   ")
        with pytest.raises(AgentException, match="未处于Plan Mode"):
            await service.write(1, "# 计划")

    async def test_批准退出会持久化计划声明但不创建权限规则(self) -> None:
        active = SimpleNamespace(content="# 原计划")
        approved = SimpleNamespace(content="# 用户编辑计划", status="approved")
        repo = SimpleNamespace(
            get_active=AsyncMock(return_value=active),
            approve=AsyncMock(return_value=approved),
        )
        turn_id = uuid4()
        allowed_prompts = [{"tool": "Bash", "prompt": "运行测试"}]

        result = await _service(repo).approve_exit(
            1,
            turn_id,
            9,
            allowed_prompts,
            "补充验收",
            "# 用户编辑计划",
        )

        assert result is approved
        repo.approve.assert_awaited_once_with(
            active,
            turn_id,
            9,
            allowed_prompts,
            "补充验收",
            "# 用户编辑计划",
        )


@pytest.mark.anyio
async def test_空计划调用ExitPlanMode时不会创建人工交互() -> None:
    runtime = object.__new__(AgentRuntime)
    runtime.tool_registry = build_tool_registry()
    runtime.plan_service = SimpleNamespace(
        get_active=AsyncMock(return_value=SimpleNamespace(content="   "))
    )
    runtime.permission_service = SimpleNamespace(evaluate=AsyncMock(return_value="allow"))
    runtime._record_tool_response = AsyncMock()
    runtime._suspend_for_interaction = AsyncMock()
    done_ids: list[str] = []

    result = await runtime._execute_tool_uses(
        1,
        SimpleNamespace(id=1),
        SimpleNamespace(id=uuid4()),
        ORCHESTRATOR_ACTOR,
        10,
        [ToolUse("exit", "ExitPlanMode", {})],
        done_ids,
        None,
    )

    assert result is None
    assert done_ids == ["exit"]
    runtime._record_tool_response.assert_awaited_once()
    runtime._suspend_for_interaction.assert_not_awaited()
    runtime.permission_service.evaluate.assert_not_awaited()


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("response", "entered", "is_error"),
    [
        ({"decision": "approve", "feedback": None}, True, False),
        ({"decision": "reject", "feedback": "先直接实现"}, False, True),
    ],
)
async def test_进入PlanMode的批准与拒绝分支(
    response: dict[str, object],
    entered: bool,
    is_error: bool,
) -> None:
    runtime = object.__new__(AgentRuntime)
    runtime.plan_service = SimpleNamespace(enter=AsyncMock())
    runtime._record_tool_response = AsyncMock()
    turn = SimpleNamespace(id=uuid4(), session_id=1)
    record = SimpleNamespace(id=3)

    await runtime._apply_enter_plan_mode(
        turn,
        ORCHESTRATOR_ACTOR,
        ToolUse("enter", "EnterPlanMode", {}),
        record,
        response,
    )

    assert runtime.plan_service.enter.await_count == int(entered)
    assert runtime._record_tool_response.await_args.kwargs == {
        "is_error": is_error,
        "record": record,
    }


@pytest.mark.anyio
async def test_拒绝退出后保持active并保存反馈和编辑计划() -> None:
    runtime = object.__new__(AgentRuntime)
    runtime.user_id = 9
    active = SimpleNamespace(content="# 修改后计划", status="active")
    runtime.plan_service = SimpleNamespace(reject_exit=AsyncMock(return_value=active))
    runtime._record_tool_response = AsyncMock()
    turn = SimpleNamespace(id=uuid4(), session_id=1)
    record = SimpleNamespace(id=4)

    await runtime._apply_exit_plan_mode(
        turn,
        ORCHESTRATOR_ACTOR,
        ToolUse("exit", "ExitPlanMode", {}),
        record,
        SimpleNamespace(request_payload={"allowedPrompts": []}),
        {
            "decision": "reject",
            "feedback": "补上回滚方案",
            "edited_plan": "# 修改后计划",
        },
    )

    runtime.plan_service.reject_exit.assert_awaited_once_with(
        1,
        "补上回滚方案",
        "# 修改后计划",
    )
    assert active.status == "active"
    assert runtime._record_tool_response.await_args.kwargs["is_error"] is True


@pytest.mark.anyio
async def test_批准退出后持久化allowedPrompts且不改权限规则() -> None:
    runtime = object.__new__(AgentRuntime)
    runtime.user_id = 9
    approved = SimpleNamespace(content="# 最终计划", status="approved")
    runtime.plan_service = SimpleNamespace(approve_exit=AsyncMock(return_value=approved))
    runtime.permission_service = SimpleNamespace(add_always_allow=AsyncMock())
    runtime._record_tool_response = AsyncMock()
    turn = SimpleNamespace(id=uuid4(), session_id=1)
    record = SimpleNamespace(id=4)
    allowed_prompts = [{"tool": "Bash", "prompt": "运行测试"}]

    await runtime._apply_exit_plan_mode(
        turn,
        ORCHESTRATOR_ACTOR,
        ToolUse("exit", "ExitPlanMode", {"allowedPrompts": allowed_prompts}),
        record,
        SimpleNamespace(request_payload={"allowedPrompts": allowed_prompts}),
        {
            "decision": "approve",
            "feedback": None,
            "edited_plan": None,
        },
    )

    runtime.plan_service.approve_exit.assert_awaited_once_with(
        1,
        turn.id,
        9,
        allowed_prompts,
        None,
        None,
    )
    runtime.permission_service.add_always_allow.assert_not_awaited()
    assert approved.status == "approved"
    assert runtime._record_tool_response.await_args.kwargs["is_error"] is False
