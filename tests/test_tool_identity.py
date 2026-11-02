"""工具执行时必须使用运行时身份,不能信任模型提交的身份字段。"""

import json
from types import SimpleNamespace

import pytest

from core.events import ORCHESTRATOR_ACTOR, teammate_actor
from tools import service as tool_service_module
from tools.base import ToolContext
from tools.builtin import task as task_tools
from tools.builtin import team as team_tools
from tools.service import ToolService


pytestmark = pytest.mark.anyio


def _ctx(principal_name: str = "alice") -> ToolContext:
    return ToolContext(session_id=1, db=None, principal_name=principal_name)


async def test_team_spawn_default_uses_canonical_agent_type(monkeypatch) -> None:
    captured = {}

    class FakeTeamService:
        def __init__(self, db):
            pass

        async def spawn_member(self, session_id, **kwargs):
            captured.update(kwargs)
            return SimpleNamespace()

    monkeypatch.setattr(team_tools, "TeamService", FakeTeamService)
    monkeypatch.setattr(team_tools, "_member_data", lambda member: {})

    args = team_tools.TeamSpawnInput(name="worker", role="执行", prompt="处理任务")
    await team_tools.TeamSpawnTool().run(args, _ctx("lead"))

    assert args.agent_type == "general-purpose"
    assert captured["agent_type"] == "general-purpose"
    assert captured["creator_id"] == "lead"


async def test_send_message_ignores_forged_sender(monkeypatch) -> None:
    captured = {}

    class FakeTeamService:
        def __init__(self, db):
            pass

        async def send_message(self, session_id, **kwargs):
            captured.update(kwargs)
            return SimpleNamespace()

    monkeypatch.setattr(team_tools, "TeamService", FakeTeamService)
    monkeypatch.setattr(team_tools, "_message_data", lambda message: {})

    args = team_tools.SendMessageInput(
        sender="mallory", recipient="bob", content="hello"
    )
    await team_tools.SendMessageTool().run(args, _ctx("alice"))

    assert captured["sender"] == "alice"
    assert captured["recipient"] == "bob"


async def test_read_inbox_ignores_forged_recipient(monkeypatch) -> None:
    captured = {}

    class FakeTeamService:
        def __init__(self, db):
            pass

        async def read_inbox(self, session_id, recipient):
            captured["recipient"] = recipient
            return []

    monkeypatch.setattr(team_tools, "TeamService", FakeTeamService)

    args = team_tools.ReadInboxInput(recipient="mallory")
    result = await team_tools.ReadInboxTool().run(args, _ctx("alice"))

    assert json.loads(result) == []
    assert captured["recipient"] == "alice"


async def test_task_create_ignores_forged_owner(monkeypatch) -> None:
    captured = {}

    class FakeTaskService:
        def __init__(self, db, bus=None):
            pass

        async def create(self, **kwargs):
            captured.update(kwargs)
            return SimpleNamespace()

    monkeypatch.setattr(task_tools, "TaskService", FakeTaskService)
    monkeypatch.setattr(task_tools, "_task_data", lambda task: {})

    args = task_tools.TaskCreateInput(subject="任务", owner="mallory")
    await task_tools.TaskCreateTool().run(args, _ctx("alice"))

    assert captured["owner"] == "alice"


async def test_task_update_ignores_forged_owner(monkeypatch) -> None:
    captured = {}

    class FakeTaskService:
        def __init__(self, db, bus=None):
            pass

        async def update(self, **kwargs):
            captured.update(kwargs)
            return SimpleNamespace()

    monkeypatch.setattr(task_tools, "TaskService", FakeTaskService)
    monkeypatch.setattr(task_tools, "_task_data", lambda task: {})

    args = task_tools.TaskUpdateInput(id=1, owner="mallory")
    await task_tools.TaskUpdateTool().run(args, _ctx("alice"))

    assert captured["owner"] == "alice"


async def test_tool_service_injects_actor_identity(monkeypatch) -> None:
    captured = []

    class FakeHooks:
        async def trigger(self, context):
            return []

    class FakeTool:
        async def run_with_dict(self, data, ctx):
            captured.append(ctx.principal_name)
            return "ok"

    class FakeRegistry:
        names = frozenset({"fake"})

        def get(self, name):
            assert name == "fake"
            return FakeTool()

    class FakeDb:
        async def flush(self):
            pass

    class FakeToolRepo:
        async def succeed(self, record, output):
            pass

    monkeypatch.setattr(tool_service_module, "get_hook_registry", lambda: FakeHooks())
    service = ToolService(FakeDb(), FakeRegistry())
    service.tool_repo = FakeToolRepo()

    async def fake_start_call(session_id, tool_name, input_args, **kwargs):
        return SimpleNamespace()

    service.start_call = fake_start_call
    await service.run(1, "fake", {}, actor=ORCHESTRATOR_ACTOR)
    await service.run(1, "fake", {}, actor=teammate_actor("alice", run_id="run-1"))

    assert captured == ["lead", "alice"]


async def test_tool_service_rejects_user_agent_control_plane_call(monkeypatch) -> None:
    denied = []

    class FakeHooks:
        async def trigger(self, context):
            return []

    class FakeDb:
        async def flush(self):
            pass

    class FakeToolRepo:
        async def fail(self, record, output):
            denied.append(output)

    class FakeRegistry:
        names = frozenset({"TeamCreate"})

    monkeypatch.setattr(tool_service_module, "get_hook_registry", lambda: FakeHooks())
    service = ToolService(FakeDb(), FakeRegistry())
    service.tool_repo = FakeToolRepo()

    async def fake_start_call(session_id, tool_name, input_args, **kwargs):
        return SimpleNamespace()

    service.start_call = fake_start_call
    output = await service.run(
        1,
        "TeamCreate",
        {},
        actor=ORCHESTRATOR_ACTOR,
        profile="user_agent",
        principal_name="agent:7",
    )

    assert "能力策略拒绝" in output
    assert denied and "profile=user_agent" in denied[0]


@pytest.mark.parametrize(
    ("allowed_skill_names", "skill_name", "should_run"),
    [
        (None, "private-skill", False),
        (frozenset(), "private-skill", False),
        (frozenset({"other-skill"}), "private-skill", False),
        (frozenset({"private-skill"}), "private-skill", True),
    ],
)
async def test_user_agent_skill_tools_require_bound_skill(
    monkeypatch, allowed_skill_names, skill_name, should_run
) -> None:
    called = []

    class FakeHooks:
        async def trigger(self, context):
            return []

    class FakeDb:
        async def flush(self):
            pass

    class FakeToolRepo:
        async def fail(self, record, output):
            pass

        async def succeed(self, record, output):
            pass

    class FakeTool:
        async def run_with_dict(self, data, ctx):
            called.append(data)
            return "ok"

    class FakeRegistry:
        names = frozenset({"SkillRun"})

        def get(self, name):
            assert name == "SkillRun"
            return FakeTool()

    monkeypatch.setattr(tool_service_module, "get_hook_registry", lambda: FakeHooks())
    service = ToolService(FakeDb(), FakeRegistry())
    service.tool_repo = FakeToolRepo()

    async def fake_start_call(session_id, tool_name, input_args, **kwargs):
        return SimpleNamespace()

    service.start_call = fake_start_call
    output = await service.run(
        1,
        "SkillRun",
        {"skill_name": skill_name, "relative_path": "scripts/check.py"},
        actor=ORCHESTRATOR_ACTOR,
        profile="user_agent",
        principal_name="agent:7",
        allowed_skill_names=allowed_skill_names,
    )

    assert (called != []) is should_run
    if should_run:
        assert output == "ok"
    else:
        assert "已绑定的 skill" in output


@pytest.mark.parametrize("profile", ["subagent", "workflow_node", "teammate", "orchestrator"])
async def test_non_lead_skill_tools_require_explicit_allowlist(monkeypatch, profile) -> None:
    """子运行身份没有 Skill 白名单时不能默认访问整个工作区。"""
    called = []

    class FakeHooks:
        async def trigger(self, context):
            return []

    class FakeDb:
        async def flush(self):
            pass

    class FakeToolRepo:
        async def fail(self, record, output):
            pass

        async def succeed(self, record, output):
            pass

    class FakeTool:
        async def run_with_dict(self, data, ctx):
            called.append(data)
            return "ok"

    class FakeRegistry:
        names = frozenset({"SkillRun"})

        def get(self, name):
            return FakeTool()

    monkeypatch.setattr(tool_service_module, "get_hook_registry", lambda: FakeHooks())
    service = ToolService(FakeDb(), FakeRegistry())
    service.tool_repo = FakeToolRepo()

    async def fake_start_call(session_id, tool_name, input_args, **kwargs):
        return SimpleNamespace()

    service.start_call = fake_start_call
    output = await service.run(
        1,
        "SkillRun",
        {"skill_name": "workspace-secret", "relative_path": "scripts/check.py"},
        actor=ORCHESTRATOR_ACTOR,
        profile=profile,
        principal_name="worker",
    )

    assert not called
    assert "已绑定的 skill" in output

