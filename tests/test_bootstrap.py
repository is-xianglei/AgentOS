"""组装根测试：装配出的依赖类型、只换一项实现、子代理启动器替换与缺失时的中文错误。

全程不连数据库与模型：数据库会话用占位对象，子代理启动器与会话工厂用替身。
"""

import json
from dataclasses import replace
from types import SimpleNamespace
from typing import Any, Self
from uuid import uuid4

import pytest

from core.errors import AgentException
from harness.adapters.memory.builtin_provider import BuiltinMemoryProvider
from harness.adapters.models.resolver import DatabaseModelResolver
from harness.adapters.reviewers.policy_reviewer import (
    NoApprovalChannelRule,
    PermissionRule,
    PolicyReviewer,
)
from harness.adapters.tools.builtin.agent import AgentTool, AgentToolInput
from harness.app.agent_service import AgentService
from harness.app.tool_set import ToolSet, platform_tools
from harness.bootstrap import (
    IsolatedSubAgentLauncher,
    builtin_harness,
    default_harness,
    platform_tool_set,
)
from harness.domain.memory import MemoryRecall, TurnRef
from harness.domain.roles.subagent import AgentType, SubAgentSpec
from harness.domain.tool import ToolContext
from permission.service import PermissionService
from runtime.agent import AgentRuntime
from runtime.subagent import SubAgentRunner
from runtime.workflow import WorkflowRuntime
from session.service import SessionService

pytestmark = pytest.mark.anyio

_DB: Any = object()


class _FakeMemory:
    """记忆替身：只记录被调用的方法。"""

    def __init__(self) -> None:
        self.calls: list[str] = []

    async def recall(self, turn: TurnRef) -> MemoryRecall | None:
        self.calls.append("recall")
        return None

    async def inherited(self, turn: TurnRef) -> MemoryRecall | None:
        self.calls.append("inherited")
        return None

    async def on_turn_completed(self, turn: TurnRef) -> None:
        self.calls.append("on_turn_completed")

    async def after_commit(self, ticket: Any) -> None:
        self.calls.append("after_commit")


class _FakeLauncher:
    """子代理启动器替身：记录启动参数并返回固定报告。"""

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    async def run(self, session_id: int, prompt: str, spec: SubAgentSpec, **kwargs: Any) -> str:
        self.calls.append({"session_id": session_id, "prompt": prompt, "spec": spec, **kwargs})
        return "替身报告"


# ---------- 装配出的依赖类型 ----------


def test_agent_runtime_dependencies_are_builtin_implementations() -> None:
    runtime = default_harness().agent_runtime(_DB, user_id=1, workspace_id=2)

    assert isinstance(runtime, AgentRuntime)
    assert isinstance(runtime.sessions, SessionService)
    assert isinstance(runtime.memory, BuiltinMemoryProvider)
    assert isinstance(runtime.models, DatabaseModelResolver)
    assert isinstance(runtime.permission_service, PermissionService)
    assert isinstance(runtime.reviewer, PolicyReviewer)
    assert type(runtime.reviewer.rules[-1]) is PermissionRule
    # 审核员的权限规则与运行时共用同一份权限服务。
    assert runtime.reviewer.rules[-1]._permission_service is runtime.permission_service
    assert runtime.tools is platform_tool_set()
    assert isinstance(runtime.subagents, IsolatedSubAgentLauncher)
    # 工具执行网关带着同一个子代理启动器，Agent 工具经它启动子代理。
    assert runtime.configuration.gateway.subagents is runtime.subagents
    assert runtime.compact_service.snapshots is runtime.sessions


def test_subagent_runner_and_workflow_runtime_dependencies() -> None:
    runner = default_harness().subagent_runner(_DB, bus=None)
    workflow = default_harness().workflow_runtime(_DB, user_id=1, workspace_id=2)

    assert isinstance(runner, SubAgentRunner)
    assert isinstance(runner.reviewer, PolicyReviewer)
    assert type(runner.reviewer.rules[-1]) is NoApprovalChannelRule
    assert isinstance(runner.memory, BuiltinMemoryProvider)
    assert isinstance(runner.sessions, SessionService)
    assert isinstance(runner.models, DatabaseModelResolver)
    assert runner.tools is platform_tool_set()
    assert isinstance(workflow, WorkflowRuntime)
    assert isinstance(workflow.sessions, SessionService)
    assert isinstance(workflow.models, DatabaseModelResolver)


def test_agent_service_is_assembled_from_bootstrap() -> None:
    service = default_harness().agent_service(_DB, user_id=1, workspace_id=2)

    assert isinstance(service, AgentService)
    assert isinstance(service._agent_runner(), AgentRuntime)
    assert isinstance(service._workflow_runner(), WorkflowRuntime)


def test_platform_tools_are_registered_by_bootstrap() -> None:
    assert platform_tools() is platform_tool_set()
    assert isinstance(platform_tools(), ToolSet)
    assert "Agent" in platform_tools().names


# ---------- 只换一项实现 ----------


def test_replacing_memory_only_changes_one_bootstrap_field() -> None:
    fake = _FakeMemory()
    harness = replace(builtin_harness(), memory=lambda db: fake)

    runtime = harness.agent_runtime(_DB, user_id=1, workspace_id=2)
    runner = harness.subagent_runner(_DB, bus=None)

    assert runtime.memory is fake
    assert runner.memory is fake
    # 其余依赖仍是内置实现。
    assert isinstance(runtime.sessions, SessionService)
    assert isinstance(runtime.reviewer, PolicyReviewer)
    assert isinstance(runtime.models, DatabaseModelResolver)


async def test_background_rebuild_keeps_only_explicit_overrides() -> None:
    """后台执行在新数据库会话里重建：显式注入的记忆随之传递，默认实现绑定新会话重建。"""
    fake = _FakeMemory()
    background_db: Any = object()

    injected = default_harness().agent_runtime(_DB, user_id=1, workspace_id=2, memory=fake)
    default = default_harness().agent_runtime(_DB, user_id=1, workspace_id=2)
    rebuilt_injected = injected._rebuild(background_db)
    rebuilt_default = default._rebuild(background_db)

    assert rebuilt_injected.memory is fake
    assert rebuilt_injected.db is background_db
    assert rebuilt_default.memory is not default.memory
    assert isinstance(rebuilt_default.memory, BuiltinMemoryProvider)
    assert rebuilt_default.memory.db is background_db
    assert rebuilt_default.sessions.db is background_db


# ---------- 子代理启动器 ----------


async def test_subagent_launcher_can_be_replaced_with_fake() -> None:
    launcher = _FakeLauncher()
    harness = replace(builtin_harness(), subagent_launcher=lambda harness: launcher)
    runtime = harness.agent_runtime(_DB, user_id=1, workspace_id=2)
    turn_id = uuid4()
    ctx = ToolContext(
        session_id=7,
        db=_DB,
        turn_id=turn_id,
        user_id=1,
        workspace_id=2,
        profile="lead",
        allowed_tool_names=frozenset({"Read"}),
        allowed_skill_names=frozenset({"skill-a"}),
        subagents=runtime.configuration.gateway.subagents,
    )

    output = await AgentTool().run(
        AgentToolInput(agent_type=AgentType.EXPLORE, prompt="找代码"), ctx
    )

    assert runtime.subagents is launcher
    assert json.loads(output)["report"] == "替身报告"
    (call,) = launcher.calls
    assert call["session_id"] == 7
    assert call["prompt"] == "找代码"
    assert call["spec"].agent_type is AgentType.EXPLORE
    assert call["parent_turn_id"] == turn_id
    assert (call["user_id"], call["workspace_id"]) == (1, 2)
    assert call["parent_allowed_tool_names"] == frozenset({"Read"})
    assert call["allowed_skill_names"] == frozenset({"skill-a"})


async def test_agent_tool_without_launcher_raises_chinese_error() -> None:
    ctx = ToolContext(session_id=7, db=_DB, profile="lead", allowed_tool_names=frozenset())

    with pytest.raises(AgentException) as exc_info:
        await AgentTool().run(AgentToolInput(prompt="找代码"), ctx)

    assert exc_info.value.message == "Agent 工具缺少子代理启动器，无法启动子代理"


async def test_isolated_launcher_opens_own_session_and_shares_bus(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """内置启动器：每次在独立数据库会话里构造运行器，事件总线沿用调用方的。"""
    opened: list[Any] = []

    class _Scope:
        async def __aenter__(self) -> Self:
            opened.append(self)
            return self

        async def __aexit__(self, *exc_info: object) -> bool:
            return False

    constructed: list[tuple[Any, Any]] = []

    async def _run(self: SubAgentRunner, session_id: int, prompt: str, spec: Any, **kwargs: Any):
        constructed.append((self.db, self.bus))
        return f"{prompt}的报告"

    monkeypatch.setattr(SubAgentRunner, "run", _run)
    harness = replace(builtin_harness(), session_factory=_Scope)
    bus = SimpleNamespace()
    launcher = harness.subagent_launcher(harness)

    report = await launcher.run(
        1,
        "检索",
        SubAgentSpec(agent_type=AgentType.EXPLORE, system_prompt="人格"),
        bus=bus,  # type: ignore[arg-type]
        parent_turn_id=None,
        user_id=None,
        workspace_id=None,
        parent_allowed_tool_names=None,
        allowed_skill_names=None,
    )

    assert report == "检索的报告"
    assert len(opened) == 1
    assert constructed == [(opened[0], bus)]


def test_unregistered_platform_tools_raise_chinese_error(monkeypatch: pytest.MonkeyPatch) -> None:
    import harness.app.tool_set as tool_set_module

    monkeypatch.setattr(tool_set_module, "_platform_tools", None)

    with pytest.raises(AgentException, match="进程级工具集尚未登记"):
        platform_tools()
