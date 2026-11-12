"""作用域工具（M0b）：绑定选择、回合解析、回合配置、主回合与子代理的传递，全部用内存假来源。

假来源经 ``replace(default_harness(), scoped_tools=(假来源,))`` 注入组装根，不碰网络和数据库：
会话用内存存储，工具调用记录、交互暂停点、权限规则查询换成内存替身，审核规则链、网关、
ToolService 与回合配置走真实代码。
"""

import asyncio
from collections.abc import AsyncIterator
from dataclasses import dataclass, field, replace
from types import SimpleNamespace
from typing import Any
from uuid import UUID, uuid4

import pytest

from agent.service import AgentRuntimeConfig
from core.config import settings
from core.errors import AgentException
from harness.app.ports import (
    ModelEventKind,
    ModelStreamEvent,
    ModelTurnResult,
    ToolSourceFault,
)
from harness.app.tool_resolver import ResolvedScope, TurnToolResolver
from harness.bootstrap import IsolatedSubAgentLauncher, builtin_harness, default_harness
from harness.domain.messages import ToolCall, ToolResult
from harness.domain.roles.subagent import AgentType, SubAgentSpec
from harness.domain.tool import (
    ScopedGrant,
    ToolBinding,
    ToolOutput,
    ToolScope,
    select_scoped,
)
from hooks import HookRegistry
from permission.repository import PermissionRepository
from runtime import agent as agent_module
from runtime import lead_loop as lead_loop_module
from runtime import turn_config as turn_config_module
from runtime.subagent import SubAgentRunner
from runtime.turn_config import configure_turn
from tests.fixtures.harness_factory import make_agent_runtime, make_subagent_runner
from tests.fixtures.in_memory_session_store import InMemorySessionStore
from tests.fixtures.scoped_tools import FakeScopedSource, FakeScopedTool
from tools import service as tool_service_module
from tools.gateway import ToolGateway
from tools.repository import ToolCatalogRepository, ToolRepository

pytestmark = pytest.mark.anyio

_SESSION_ID = 7
_USER_ID = 3
_WORKSPACE_ID = 2
_SCOPE = ToolScope(workspace_id=_WORKSPACE_ID, user_id=_USER_ID)
_PLATFORM = default_harness().tools
# 主回合平时可见的平台工具：去掉两个 Plan 工具，顺序同进程级工具集。
_LEAD_ROUND_NAMES = [tool.name for tool in _PLATFORM.without("ExitPlanMode", "WritePlan").tools]


class _ScriptedLLM:
    """模型替身：按脚本依次给出终局结果，记下每次请求的 Context。"""

    provider = "anthropic"
    model = "claude-scoped"

    def __init__(self, *results: ModelTurnResult) -> None:
        self._results = list(results)
        self.requests: list[Any] = []

    async def stream(self, context: Any, settings: Any) -> AsyncIterator[ModelStreamEvent]:
        self.requests.append(context)
        yield ModelStreamEvent(ModelEventKind.TURN_RESULT, result=self._results.pop(0))


class _NoMemory:
    """记忆替身：不召回、不写入。"""

    async def recall(self, turn: Any) -> None:
        return None

    async def inherited(self, turn: Any) -> None:
        return None

    async def on_turn_completed(self, turn: Any) -> None:
        return None

    async def after_commit(self, ticket: Any) -> None:
        return None


class _Db:
    """数据库会话替身：只计数事务动作，不连真实数据库。"""

    def __init__(self) -> None:
        self.ops: list[str] = []

    async def commit(self) -> None:
        self.ops.append("commit")

    async def flush(self) -> None:
        self.ops.append("flush")

    async def rollback(self) -> None:
        self.ops.append("rollback")


class _RecordingBus:
    def __init__(self) -> None:
        self.events: list[Any] = []

    async def emit(self, event: Any) -> dict[str, Any]:
        self.events.append(event)
        return {}

    def tool_results(self) -> list[Any]:
        return [event.tool for event in self.events if getattr(event, "tool", None) is not None]


@dataclass
class _Interactions:
    """交互服务替身：只支持一个暂停点和一项请求，状态流转与真实实现一致。"""

    suspension: Any = None
    request: Any = None

    async def create_suspension_with_request(
        self, session_id: int, turn_id: UUID, continuation: Any, policy: Any, create: Any
    ) -> tuple[Any, Any]:
        self.suspension = SimpleNamespace(
            id=uuid4(),
            session_id=session_id,
            turn_id=turn_id,
            status="pending",
            continuation_payload=continuation,
        )
        self.request = SimpleNamespace(
            id=uuid4(),
            suspension_id=self.suspension.id,
            session_id=session_id,
            kind=create.kind,
            request_payload=create.request_payload,
            tool_call_id=create.tool_call_id,
            tool_use_id=create.tool_use_id,
            schema_version=create.schema_version,
            status="pending",
            response_payload=None,
        )
        return self.suspension, self.request

    async def get_request_required(self, request_id: UUID, *, session_id: int | None = None):
        assert request_id == self.request.id
        return self.request

    async def resolve_request(
        self, request_id: UUID, response: dict[str, Any], responded_by: Any, **_: Any
    ) -> tuple[Any, Any]:
        self.request.response_payload = response
        self.request.status = "resolved"
        self.suspension.status = "resuming"
        return self.request, self.suspension

    async def lock_suspension_required(self, suspension_id: UUID, *, session_id: int | None = None):
        return self.suspension

    async def mark_suspension_resolved(self, suspension_id: UUID) -> Any:
        self.suspension.status = "resolved"
        return self.suspension


@dataclass
class _World:
    """一次测试的内存现场：会话存储、工具调用记录与交互暂停点。"""

    store: InMemorySessionStore = field(default_factory=InMemorySessionStore)
    tool_calls: dict[int, Any] = field(default_factory=dict)
    interactions: _Interactions = field(default_factory=_Interactions)


class _FakeAgentDefinitionService:
    """自建 Agent 只绑定 Read；默认 Agent 没有可委派目录。"""

    def __init__(self, db: object) -> None:
        pass

    async def list_delegate_catalog(self, workspace_id: int) -> tuple[()]:
        return ()

    async def get_runtime_config(self, workspace_id: int, agent_id: int) -> AgentRuntimeConfig:
        return AgentRuntimeConfig(
            agent_id=agent_id,
            system_prompt="你是审稿人",
            model_name=None,
            tool_runtime_names=("Read",),
            skill_ids=(),
            skill_names=(),
            name="审稿人",
            description=None,
        )


def _install(monkeypatch: pytest.MonkeyPatch, world: _World, rules: dict[str, str]) -> None:
    """换掉 Agent 定义、Hook、任务基线、Skill 目录、队友、Plan、调用记录与权限规则的数据来源。

    rules 是按工具名给出的权限规则（allow / ask / deny）；没有规则的工具按策略默认值判定。
    """

    async def _tool_start(self: Any, session_id: int, tool_name: str, input_args: Any, **_: Any):
        record = SimpleNamespace(
            id=len(world.tool_calls) + 1,
            session_id=session_id,
            tool_name=tool_name,
            input_args=input_args,
            status="running",
            output_data=None,
            error_message=None,
        )
        world.tool_calls[record.id] = record
        return record

    async def _tool_get(self: Any, tool_call_id: int) -> Any:
        return world.tool_calls.get(tool_call_id)

    def _set_status(status: str, field_name: str | None = None):
        async def _apply(self: Any, record: Any, value: Any = None) -> Any:
            record.status = status
            if field_name is not None:
                setattr(record, field_name, value)
            return record

        return _apply

    async def _no_catalog(self: Any, runtime_name: str) -> None:
        return None

    async def _find_match(self: Any, scope: str, session_id: Any, tool_name: str, *_: Any):
        behavior = rules.get(tool_name)
        if scope != "session" or behavior is None:
            return None
        return SimpleNamespace(behavior=behavior)

    monkeypatch.setattr(turn_config_module, "AgentDefinitionService", _FakeAgentDefinitionService)
    monkeypatch.setattr(agent_module, "get_hook_registry", HookRegistry)
    monkeypatch.setattr(tool_service_module, "get_hook_registry", HookRegistry)
    monkeypatch.setattr(
        agent_module, "TaskService", lambda db, bus: SimpleNamespace(emit_snapshot=_async(None))
    )
    monkeypatch.setattr(
        agent_module, "TeamService", lambda db: SimpleNamespace(list_members=_async([]))
    )
    monkeypatch.setattr(lead_loop_module.SkillService, "get_catalog", _async(""))
    monkeypatch.setattr(ToolRepository, "start", _tool_start)
    monkeypatch.setattr(ToolRepository, "get", _tool_get)
    monkeypatch.setattr(ToolRepository, "succeed", _set_status("succeeded", "output_data"))
    monkeypatch.setattr(ToolRepository, "fail", _set_status("failed", "error_message"))
    monkeypatch.setattr(ToolRepository, "mark_awaiting_interaction", _set_status("awaiting"))
    monkeypatch.setattr(ToolRepository, "reject_awaiting", _set_status("rejected", "error_message"))
    monkeypatch.setattr(ToolCatalogRepository, "get_by_runtime_name", _no_catalog)
    monkeypatch.setattr(PermissionRepository, "find_match", _find_match)


def _async(value: Any):
    async def _return(*args: Any, **kwargs: Any) -> Any:
        return value

    return _return


def _main_runtime(
    world: _World,
    llm: _ScriptedLLM,
    *sources: FakeScopedSource,
    bus: _RecordingBus | None = None,
):
    """经组装根装配主运行时：作用域来源、会话存储、模型与记忆换成替身，其余走真实实现。"""
    harness = replace(default_harness(), scoped_tools=sources)
    runtime = make_agent_runtime(
        _Db(),
        user_id=_USER_ID,
        workspace_id=_WORKSPACE_ID,
        harness=harness,
        llm=llm,
        memory=_NoMemory(),
        sessions=world.store,
    )
    runtime.interaction_service = world.interactions  # type: ignore[assignment]
    runtime.plan_service = SimpleNamespace(get_active=_async(None))  # type: ignore[assignment]
    runtime.bus = bus or _RecordingBus()  # type: ignore[assignment]
    return runtime


async def _start_turn(world: _World, content: str, *, mode_slug: str = "standard") -> Any:
    """请求阶段的准入：经内存存储建会话与轮次。"""
    session = world.store.sessions.get(_SESSION_ID)
    if session is None:
        session = world.store.create_session(
            _SESSION_ID, workspace_id=_WORKSPACE_ID, user_id=_USER_ID
        )
    session.mode_slug = mode_slug
    session = await world.store.prepare_for_message(_SESSION_ID, content, _USER_ID, _WORKSPACE_ID)
    turn, _ = await world.store.start_turn(session, _USER_ID, _WORKSPACE_ID, content)
    return turn


def _tool_names(context: Any) -> list[str]:
    return [spec.name for spec in context.tools]


def _tool_messages(world: _World) -> list[dict[str, Any]]:
    messages = world.store.session_messages(_SESSION_ID)
    return [content for role, content in messages if role == "tool"]


# ---------- 领域层：select_scoped 与 ToolBinding ----------

_GH_ISSUE = FakeScopedTool("mcp__github__create_issue", source="mcp:github")
_GH_PR = FakeScopedTool("mcp__github__list_prs", source="mcp:github")
_JIRA = FakeScopedTool("mcp__jira__search", source="mcp:jira")
_ALL_SCOPED = (_GH_ISSUE, _GH_PR, _JIRA)


@pytest.mark.parametrize(
    ("binding", "expected"),
    [
        (ToolBinding.all(), [_GH_ISSUE, _GH_PR, _JIRA]),
        (ToolBinding.none(), []),
        (ToolBinding.servers({}), []),
        (ToolBinding.servers({"mcp:github": None}), [_GH_ISSUE, _GH_PR]),
        (ToolBinding.servers({"mcp:github": ["mcp__github__list_prs"]}), [_GH_PR]),
        (
            ToolBinding.servers({"mcp:jira": None, "mcp:github": ["mcp__github__create_issue"]}),
            [_GH_ISSUE, _JIRA],
        ),
        # 白名单里的名字不属于该来源，或来源不存在，都选不出东西。
        (ToolBinding.servers({"mcp:github": ["mcp__jira__search"]}), []),
        (ToolBinding.servers({"mcp:gitlab": None}), []),
        # 空白名单表示该来源一个都不给，不等于全部。
        (ToolBinding.servers({"mcp:github": []}), []),
    ],
)
def test_select_scoped_by_binding(binding: ToolBinding, expected: list[Any]) -> None:
    assert list(select_scoped(_ALL_SCOPED, binding)) == expected


def test_select_scoped_keeps_input_order_and_skips_builtin_tools() -> None:
    builtin = _PLATFORM.get("Read")
    tools = (_JIRA, builtin, _GH_ISSUE)

    assert select_scoped(tools, ToolBinding.servers({"mcp:jira": None, "mcp:github": None})) == (
        _JIRA,
        _GH_ISSUE,
    )
    # all() 原样返回，调用方只传作用域工具。
    assert select_scoped(_ALL_SCOPED, ToolBinding.all()) == _ALL_SCOPED


def test_tool_binding_is_value_object() -> None:
    first = ToolBinding.servers({"mcp:jira": None, "mcp:github": ["b", "a"]})
    second = ToolBinding.servers({"mcp:github": ("a", "b"), "mcp:jira": None})

    assert first == second and hash(first) == hash(second)
    assert ToolBinding.all() != ToolBinding.none()
    assert _PLATFORM.get("Read").source is None
    assert ScopedGrant(tools=_ALL_SCOPED, visible=frozenset({_JIRA.name})).visible_tools() == (
        _JIRA,
    )


# ---------- 应用层：TurnToolResolver ----------


async def test_resolver_without_sources_returns_platform_only() -> None:
    resolver = TurnToolResolver(_PLATFORM, (), total_timeout=0.01)

    resolved = await resolver.resolve(_SCOPE)

    assert resolved == ResolvedScope.platform_only(_PLATFORM)
    assert resolved.policies is _PLATFORM
    assert (resolved.scoped, resolved.faults) == ((), ())


async def test_resolver_merges_sources_sorted_after_platform_tools() -> None:
    fault = ToolSourceFault(
        source="mcp:jira", server_id=9, kind="auth_required", message="需要先授权"
    )
    first = FakeScopedSource(_JIRA, _GH_PR)
    second = FakeScopedSource(_GH_ISSUE, faults=(fault,), name="mcp-private")
    resolver = TurnToolResolver(_PLATFORM, (first, second), total_timeout=1)

    resolved = await resolver.resolve(_SCOPE)

    # 作用域工具按工具名排序，与来源的返回顺序无关。
    assert resolved.scoped == (_GH_ISSUE, _GH_PR, _JIRA)
    assert [tool.name for tool in resolved.policies.tools] == [
        *(tool.name for tool in _PLATFORM.tools),
        _GH_ISSUE.name,
        _GH_PR.name,
        _JIRA.name,
    ]
    assert resolved.policies.policy_for(_JIRA.name) is _JIRA.policy
    assert resolved.faults == (fault,)
    assert first.scopes == [_SCOPE] and second.scopes == [_SCOPE]


@pytest.mark.parametrize(
    ("tools", "message"),
    [
        ((FakeScopedTool("Read"),), "作用域工具与平台工具重名"),
        ((_JIRA, FakeScopedTool(_JIRA.name)), "作用域工具重名"),
    ],
)
async def test_resolver_rejects_duplicate_names(tools: tuple[Any, ...], message: str) -> None:
    resolver = TurnToolResolver(_PLATFORM, (FakeScopedSource(*tools),), total_timeout=1)

    with pytest.raises(AgentException) as exc_info:
        await resolver.resolve(_SCOPE)

    assert exc_info.value.message == message


async def test_failed_or_slow_source_becomes_fault_and_others_still_load(
    caplog: pytest.LogCaptureFixture,
) -> None:
    healthy = FakeScopedSource(_JIRA, name="healthy")
    broken = FakeScopedSource(error=RuntimeError("连接串 secret"), name="broken")
    slow = FakeScopedSource(_GH_ISSUE, delay=5, name="slow")
    resolver = TurnToolResolver(_PLATFORM, (healthy, broken, slow), total_timeout=0.05)

    resolved = await resolver.resolve(_SCOPE)

    assert resolved.scoped == (_JIRA,)
    assert [(fault.source, fault.kind, fault.server_id) for fault in resolved.faults] == [
        ("broken", "unavailable", None),
        ("slow", "timeout", None),
    ]
    # 故障文本只给用户看，不带异常细节；未完成的来源已被取消，不留后台任务。
    assert all("secret" not in fault.message for fault in resolved.faults)
    assert slow.cancelled is True
    assert {record.getMessage() for record in caplog.records} >= {
        "作用域工具来源解析失败，本回合缺少它的工具，来源=broken，错误类型=RuntimeError",
        "作用域工具来源解析超时，本回合缺少它的工具，来源=slow",
    }


async def test_resolver_cancels_sources_when_turn_is_cancelled() -> None:
    slow = FakeScopedSource(_JIRA, delay=5)
    resolver = TurnToolResolver(_PLATFORM, (slow,), total_timeout=10)
    task = asyncio.create_task(resolver.resolve(_SCOPE))
    await asyncio.sleep(0.01)

    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert slow.cancelled is True


# ---------- 回合配置：可见集、策略全集与绑定 ----------


async def _configure(session: Any, resolved: ResolvedScope | None, monkeypatch: Any):
    monkeypatch.setattr(turn_config_module, "AgentDefinitionService", _FakeAgentDefinitionService)
    return await configure_turn(
        object(),  # type: ignore[arg-type]
        session,
        user_id=_USER_ID,
        workspace_id=_WORKSPACE_ID,
        models=SimpleNamespace(),  # type: ignore[arg-type]
        tools=_PLATFORM,
        subagents=None,
        injected=_ScriptedLLM(),  # type: ignore[arg-type]
        resolved=resolved,
    )


async def _resolved(*tools: Any) -> ResolvedScope:
    resolver = TurnToolResolver(_PLATFORM, (FakeScopedSource(*tools),), total_timeout=1)
    return await resolver.resolve(_SCOPE)


def _session(**overrides: Any) -> Any:
    values = {"agent_id": None, "mode_slug": "standard", "model_name": None}
    return SimpleNamespace(**{**values, **overrides})


async def test_lead_sees_scoped_tools_after_builtin_tools(monkeypatch: pytest.MonkeyPatch) -> None:
    resolved = await _resolved(_JIRA, _GH_ISSUE)

    configuration = await _configure(_session(), resolved, monkeypatch)

    names = [tool.name for tool in configuration.tools.tools]
    assert names == [*(tool.name for tool in _PLATFORM.tools), _GH_ISSUE.name, _JIRA.name]
    assert configuration.policies is resolved.policies
    assert configuration.scoped is resolved
    assert [tool.name for tool in configuration.gateway.registry.tools] == names
    assert configuration.gateway.policies is resolved.policies
    assert configuration.gateway.scoped == ScopedGrant(
        tools=(_GH_ISSUE, _JIRA), visible=frozenset({_GH_ISSUE.name, _JIRA.name})
    )
    # 逐轮裁剪沿用 tools_for_mode：Plan 模式的只读白名单把作用域工具剔除。
    assert [tool.name for tool in configuration.tools_for_mode(False).tools][-2:] == [
        _GH_ISSUE.name,
        _JIRA.name,
    ]
    assert _JIRA.name not in configuration.tools_for_mode(True).names


@pytest.mark.parametrize(
    "session",
    [_session(mode_slug="turbo"), _session(agent_id=11)],
    ids=["turbo", "user_agent"],
)
async def test_turbo_and_user_agent_do_not_see_scoped_tools(
    session: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    resolved = await _resolved(_JIRA)

    configuration = await _configure(session, resolved, monkeypatch)

    assert _JIRA.name not in configuration.tools.names
    assert _JIRA.name not in configuration.gateway.registry.names
    # 不可见不等于查不到：策略全集仍含作用域工具，子执行据此查策略。
    assert configuration.policies.policy_for(_JIRA.name) is _JIRA.policy
    assert configuration.gateway.scoped == ScopedGrant(tools=(_JIRA,), visible=frozenset())


async def test_request_path_uses_platform_only(monkeypatch: pytest.MonkeyPatch) -> None:
    configuration = await _configure(_session(), None, monkeypatch)

    assert configuration.scoped == ResolvedScope.platform_only(_PLATFORM)
    assert configuration.policies is _PLATFORM
    assert configuration.tools.names == _PLATFORM.names
    assert configuration.gateway.scoped is None


# ---------- 主回合端到端：解析 → 发给模型 → 审核 → 执行 ----------


def _call(tool: FakeScopedTool, call_id: str = "call-1") -> ToolCall:
    return ToolCall(id=call_id, tool_name=tool.name, arguments={"query": "北京"})


def _scripted(*tool_calls: ToolCall) -> _ScriptedLLM:
    return _ScriptedLLM(
        ModelTurnResult(tool_calls=list(tool_calls), stop_reason="tool_use"),
        ModelTurnResult(text="完成。", stop_reason="end_turn"),
    )


async def test_lead_turn_sends_scoped_tools_after_builtin_and_runs_them(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    world = _World()
    _install(monkeypatch, world, {})
    search = FakeScopedTool("mcp__fake__search")
    source = FakeScopedSource(search)
    llm = _scripted(_call(search))
    turn = await _start_turn(world, "查一下")

    await _main_runtime(world, llm, source)._produce(_SESSION_ID, turn.id, "查一下")

    # 每回合按可信身份解析一次，回合内多轮共用。
    assert source.scopes == [_SCOPE]
    assert [_tool_names(context) for context in llm.requests] == [
        [*_LEAD_ROUND_NAMES, search.name],
        [*_LEAD_ROUND_NAMES, search.name],
    ]
    ((arguments, ctx),) = search.calls
    assert arguments == {"query": "北京"}
    assert ctx.profile == "lead"
    # 作用域工具经网关随工具上下文下发，留给 Agent 工具交给子执行。
    assert ctx.scoped == ScopedGrant(tools=(search,), visible=frozenset({search.name}))
    assert _tool_messages(world)[-1]["output"] == f"{search.name} 的结果"
    assert world.store.turns[turn.id].status == "completed"


async def test_scoped_tool_error_result_reaches_model_as_is(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    world = _World()
    _install(monkeypatch, world, {})
    failing = FakeScopedTool("mcp__fake__fail", output=ToolOutput.error("远程报错：参数不合法"))
    bus = _RecordingBus()
    llm = _scripted(_call(failing))
    turn = await _start_turn(world, "试试")

    runtime = _main_runtime(world, llm, FakeScopedSource(failing), bus=bus)
    await runtime._produce(_SESSION_ID, turn.id, "试试")

    stored = _tool_messages(world)[-1]
    assert (stored["output"], stored["is_error"]) == ("远程报错：参数不合法", True)
    (event,) = bus.tool_results()
    assert (event.output, event.is_error) == ("远程报错：参数不合法", True)
    # 下一轮发给模型的工具结果原样带着 is_error。
    tool_result = llm.requests[1].messages[-1]
    assert isinstance(tool_result, ToolResult)
    assert (tool_result.content, tool_result.is_error) == ("远程报错：参数不合法", True)
    # 执行失败的结果记为失败调用（M1c，设计 5.4 节），失败文本存进 error_message。
    assert world.tool_calls[1].status == "failed"
    assert world.tool_calls[1].error_message == "远程报错：参数不合法"


async def test_approval_required_scoped_tool_suspends_and_resumes_after_approval(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    world = _World()
    _install(monkeypatch, world, {})
    guarded = FakeScopedTool("mcp__fake__delete", requires_approval=True)
    source = FakeScopedSource(guarded)
    llm = _scripted(_call(guarded))
    turn = await _start_turn(world, "删掉它")
    bus = _RecordingBus()

    await _main_runtime(world, llm, source, bus=bus)._produce(_SESSION_ID, turn.id, "删掉它")

    # 审核按策略全集查到 requires_approval=True，挂起为 tool_approval；工具还没执行。
    request = world.interactions.request
    assert request.kind == "tool_approval"
    assert request.request_payload["tool"]["name"] == guarded.name
    assert request.request_payload["reason"] == "危险工具默认需审批"
    assert guarded.calls == []
    assert world.store.turns[turn.id].status == "awaiting_interaction"
    assert world.tool_calls[1].status == "awaiting"

    # 请求路径保存审批响应：只用平台工具配置回合，不解析作用域工具。
    approver = _main_runtime(world, llm, source)
    await approver.prepare_interaction_response(
        _SESSION_ID, request.id, "tool_approval", {"decision": "allow_once"}
    )
    assert source.scopes == [_SCOPE]
    assert approver.configuration.scoped == ResolvedScope.platform_only(_PLATFORM)

    # 后台恢复重新解析一次，再执行已批准的调用并续跑到回合完成。
    await _main_runtime(world, llm, source)._produce_resume(_SESSION_ID, request.id)

    assert source.scopes == [_SCOPE, _SCOPE]
    assert len(guarded.calls) == 1
    assert world.tool_calls[1].status == "succeeded"
    assert _tool_messages(world)[-1]["output"] == f"{guarded.name} 的结果"
    assert world.interactions.suspension.status == "resolved"
    assert world.store.turns[turn.id].status == "completed"
    assert [_tool_names(context)[-1] for context in llm.requests] == [guarded.name] * 2


async def test_turbo_turn_does_not_send_scoped_tools(monkeypatch: pytest.MonkeyPatch) -> None:
    world = _World()
    _install(monkeypatch, world, {})
    llm = _ScriptedLLM(ModelTurnResult(text="好的。", stop_reason="end_turn"))
    turn = await _start_turn(world, "快速回答", mode_slug="turbo")

    await _main_runtime(world, llm, FakeScopedSource(_JIRA))._produce(
        _SESSION_ID, turn.id, "快速回答"
    )

    assert [_tool_names(context) for context in llm.requests] == [[]]
    assert world.store.turns[turn.id].status == "completed"


# ---------- 子代理：经 scoped 参数继承父执行的作用域工具 ----------


class _RunRepo:
    async def create(self, session_id: int, member_name: str, prompt: str, **_: Any) -> Any:
        return SimpleNamespace(id=11)

    async def succeed(self, run: Any, report: str) -> Any:
        return run

    async def fail(self, run: Any, error_message: str) -> Any:
        return run


def _general_spec() -> SubAgentSpec:
    """无工具白名单的子代理规格（同 general-purpose）。"""
    return SubAgentSpec(agent_type=AgentType.GENERAL_PURPOSE, system_prompt="子代理人格")


def _subagent_runner(monkeypatch: pytest.MonkeyPatch, llm: _ScriptedLLM) -> SubAgentRunner:
    """经组装根装配真实子代理运行器：真实审核规则链、网关与 ToolService，模型与记录换成替身。"""
    world = _World()
    _install(monkeypatch, world, {})
    runner = make_subagent_runner(_Db(), bus=_RecordingBus(), llm=llm, memory=_NoMemory())
    runner.team_service.run_repo = _RunRepo()  # type: ignore[assignment]
    return runner


async def test_subagent_sees_only_visible_scoped_tools(monkeypatch: pytest.MonkeyPatch) -> None:
    visible = FakeScopedTool("mcp__fake__visible")
    hidden = FakeScopedTool("mcp__fake__hidden")
    llm = _ScriptedLLM(
        ModelTurnResult(tool_calls=[_call(visible)], stop_reason="tool_use"),
        ModelTurnResult(text="子代理报告。", stop_reason="end_turn"),
    )
    runner = _subagent_runner(monkeypatch, llm)
    grant = ScopedGrant(tools=(hidden, visible), visible=frozenset({visible.name}))

    report = await runner.run(
        _SESSION_ID,
        "去查",
        _general_spec(),
        parent_allowed_tool_names=frozenset({"Read", "Grep", visible.name}),
        scoped=grant,
    )

    assert report == "子代理报告。"
    # 可见集：平台工具按规格与父名单裁剪后在前，visible 内的作用域工具在后。
    assert _tool_names(llm.requests[0]) == ["Read", "Grep", visible.name]
    assert len(visible.calls) == 1 and hidden.calls == []
    assert visible.calls[0][1].profile == "subagent"


async def test_subagent_without_scoped_keeps_platform_tools_only(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    llm = _ScriptedLLM(ModelTurnResult(text="报告。", stop_reason="end_turn"))
    runner = _subagent_runner(monkeypatch, llm)

    await runner.run(
        _SESSION_ID, "去查", _general_spec(), parent_allowed_tool_names=frozenset({"Read"})
    )

    assert _tool_names(llm.requests[0]) == ["Read"]


@pytest.mark.parametrize(
    ("builtin_name", "requires_approval"),
    [("Glob", False), ("Write", True)],
    ids=["no_approval", "requires_approval"],
)
async def test_hidden_scoped_call_is_reviewed_like_hidden_builtin(
    monkeypatch: pytest.MonkeyPatch, builtin_name: str, requires_approval: bool
) -> None:
    """父有、子不可见的调用：作用域工具与同等策略的内置工具，审核结论与回给模型的结果一致。"""
    hidden = FakeScopedTool("mcp__fake__hidden", requires_approval=requires_approval)
    builtin_call = ToolCall(id="call-b", tool_name=builtin_name, arguments={"query": "北京"})
    scoped_call = ToolCall(id="call-s", tool_name=hidden.name, arguments={"query": "北京"})
    llm = _ScriptedLLM(
        ModelTurnResult(tool_calls=[builtin_call, scoped_call], stop_reason="tool_use"),
        ModelTurnResult(text="报告。", stop_reason="end_turn"),
    )
    runner = _subagent_runner(monkeypatch, llm)
    bus: _RecordingBus = runner.bus  # type: ignore[assignment]
    executed: list[str] = []

    async def _gateway_run(self: ToolGateway, session_id: int, tool_name: str, *_: Any, **__: Any):
        executed.append(tool_name)
        return ToolOutput.ok("网关结果")

    # 网关的 run 换成记录替身：只比较审核结论与是否交给网关，不真正执行内置工具。
    monkeypatch.setattr(ToolGateway, "run", _gateway_run)

    await runner.run(
        _SESSION_ID,
        "去查",
        _general_spec(),
        parent_allowed_tool_names=frozenset({"Read"}),
        scoped=ScopedGrant(tools=(hidden,), visible=frozenset()),
    )

    assert _tool_names(llm.requests[0]) == ["Read"]
    builtin_result, scoped_result = bus.tool_results()
    assert scoped_result.is_error == builtin_result.is_error
    assert scoped_result.output == builtin_result.output.replace(builtin_name, hidden.name)
    if requires_approval:
        # 需审批：子代理没有审批通道，两者都被合并为拒绝，都不执行。
        assert scoped_result.is_error is True
        assert "需要用户审批" in scoped_result.output
        assert executed == []
    else:
        # 不需审批：能力检查按策略全集放行，两者都交给网关，与内置工具的现有行为一致。
        assert scoped_result.is_error is False
        assert executed == [builtin_name, hidden.name]


# ---------- 组装根 ----------


def test_harness_defaults_to_no_scoped_sources(monkeypatch: pytest.MonkeyPatch) -> None:
    # M1c 起 MCP 开关打开时默认装配带 MCP 来源，这里按关闭 MCP 构造。
    monkeypatch.setattr(settings, "mcp_enabled", False)
    harness = builtin_harness()
    runtime = make_agent_runtime(object(), user_id=_USER_ID, workspace_id=_WORKSPACE_ID)

    assert harness.scoped_tools == ()
    assert isinstance(runtime.tool_resolver, TurnToolResolver)
    assert runtime.tool_resolver._sources == ()
    assert runtime.configuration.scoped == ResolvedScope.platform_only(harness.tools)


def test_agent_runtime_gets_resolver_over_injected_sources() -> None:
    source = FakeScopedSource(_JIRA)
    harness = replace(default_harness(), scoped_tools=(source,))

    runtime = make_agent_runtime(
        object(), user_id=_USER_ID, workspace_id=_WORKSPACE_ID, harness=harness
    )
    rebuilt = runtime._rebuild(object())

    assert runtime.tool_resolver._sources == (source,)
    assert runtime.tool_resolver._platform is harness.tools
    assert rebuilt.tool_resolver._sources == (source,)


async def test_isolated_launcher_forwards_scoped(monkeypatch: pytest.MonkeyPatch) -> None:
    class _Scope:
        async def __aenter__(self) -> object:
            return object()

        async def __aexit__(self, *exc_info: object) -> bool:
            return False

    received: list[Any] = []

    async def _run(self: SubAgentRunner, *args: Any, scoped: Any = None, **kwargs: Any) -> str:
        received.append(scoped)
        return "报告"

    monkeypatch.setattr(SubAgentRunner, "run", _run)
    launcher = IsolatedSubAgentLauncher(replace(default_harness(), session_factory=_Scope))
    grant = ScopedGrant(tools=(_JIRA,), visible=frozenset({_JIRA.name}))

    report = await launcher.run(
        _SESSION_ID,
        "去查",
        _general_spec(),
        bus=None,
        parent_turn_id=None,
        user_id=_USER_ID,
        workspace_id=_WORKSPACE_ID,
        parent_allowed_tool_names=None,
        allowed_skill_names=None,
        scoped=grant,
    )

    assert report == "报告"
    assert received == [grant]
