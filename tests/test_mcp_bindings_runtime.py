"""MCP 接入 M2 的运行时：各档位按 6.2 节拿到的作用域工具，全部用内存假来源，经 harness_factory 注入。

复用 tests/test_scoped_tools.py 的内存现场（会话存储、调用记录、交互暂停点）。覆盖：
- user_agent 主回合只看到绑定的 server 与白名单内的工具；
- Agent 工具：general-purpose 子代理继承父回合可见的 MCP 工具，Explore 一个都没有；委派取绑定与父回合的交集；
- 子代理路径的 tool_use / tool_result 事件与 tool_calls.source 带来源；
- 队友按成员规格继承；Workflow 成员各按自己的绑定拿到工具，执行体开头只解析一次；
- Explore 调用父回合有、自己不可见的 MCP 工具时，审核结论与内置工具同类情况一致。
"""

from dataclasses import replace
from types import SimpleNamespace
from typing import Any
from uuid import uuid4

import pytest

from agent.service import AgentMcpBinding, AgentRuntimeConfig
from core.events import BlockType, ToolInfo
from harness.app.ports import ModelEventKind, ModelStreamEvent, ModelTurnResult
from harness.app.subagent_catalog import get_subagent_spec
from harness.app.tool_resolver import ResolvedScope, TurnToolResolver
from harness.bootstrap import default_harness
from harness.domain.messages import ToolCall
from harness.domain.roles.subagent import AgentType
from harness.domain.tool import ScopedGrant, ToolOutput
from runtime import agent as agent_module
from runtime import turn_config as turn_config_module
from runtime import workflow as workflow_module
from runtime.subagent import SubAgentRunner
from runtime.workflow import SequentialOrchestrator
from tests.fixtures.harness_factory import make_subagent_runner, make_workflow_runtime
from tests.fixtures.scoped_tools import FakeScopedSource, FakeScopedTool
from tests.test_scoped_tools import (
    _SCOPE,
    _SESSION_ID,
    _USER_ID,
    _WORKSPACE_ID,
    _Db,
    _install,
    _main_runtime,
    _NoMemory,
    _RecordingBus,
    _RunRepo,
    _start_turn,
    _tool_names,
    _World,
)
from workflow.service import WorkflowRunMember

pytestmark = pytest.mark.anyio

_GH_ISSUE = "mcp__github__create_issue"
_GH_PR = "mcp__github__list_prs"
_JIRA = "mcp__jira__search"
_SLACK = "mcp__slack__post"


def _tools() -> dict[str, FakeScopedTool]:
    """每个用例一组新的假 MCP 工具（各自记录调用），按模型名索引。"""
    return {
        name: FakeScopedTool(name, source=f"mcp:{name.split('__')[1]}")
        for name in (_GH_ISSUE, _GH_PR, _JIRA, _SLACK)
    }


def _config(agent_id: int, *bindings: AgentMcpBinding, tools: tuple[str, ...] = ("Read",)):
    return AgentRuntimeConfig(
        agent_id=agent_id,
        system_prompt=f"你是成员{agent_id}",
        model_name=None,
        tool_runtime_names=tools,
        skill_ids=(),
        skill_names=(),
        name=f"成员{agent_id}",
        description=None,
        mcp_bindings=bindings,
    )


def _install_agents(monkeypatch: pytest.MonkeyPatch, *modules: Any, **configs: Any) -> None:
    """把各模块的 AgentDefinitionService 换成按 agent_id 返回 configs 的替身。"""
    by_id = {config.agent_id: config for config in configs.values()}

    class _Agents:
        def __init__(self, db: object) -> None:
            pass

        async def list_delegate_catalog(self, workspace_id: int) -> tuple[()]:
            return ()

        async def get_runtime_config(self, workspace_id: int, agent_id: int) -> AgentRuntimeConfig:
            return by_id[agent_id]

    for module in modules:
        monkeypatch.setattr(module, "AgentDefinitionService", _Agents)


class _StreamLLM:
    """模型替身：按脚本给出结果；工具调用以 TOOL_USE 流事件发出，记下每次请求的 Context。"""

    provider = "anthropic"
    model = "claude-m2"

    def __init__(self, *results: ModelTurnResult) -> None:
        self._results = list(results)
        self.requests: list[Any] = []

    async def stream(self, context: Any, settings: Any):
        self.requests.append(context)
        result = self._results.pop(0)
        for ref, call in enumerate(result.tool_calls):
            info = ToolInfo(id=call.id, name=call.tool_name, input=call.arguments)
            yield ModelStreamEvent(
                ModelEventKind.BLOCK_START, block_ref=ref, block_type=BlockType.TOOL_USE
            )
            yield ModelStreamEvent(ModelEventKind.TOOL_USE, block_ref=ref, tool=info)
        yield ModelStreamEvent(ModelEventKind.TURN_RESULT, result=result)


def _call(name: str, call_id: str = "call-1", arguments: dict[str, Any] | None = None) -> ToolCall:
    return ToolCall(id=call_id, tool_name=name, arguments=arguments or {"query": "北京"})


def _scripted(*calls: ToolCall, text: str = "完成。") -> _StreamLLM:
    first = ModelTurnResult(tool_calls=list(calls), stop_reason="tool_use")
    return _StreamLLM(first, ModelTurnResult(text=text, stop_reason="end_turn"))


def _scoped_names(context: Any) -> list[str]:
    return [name for name in _tool_names(context) if name.startswith("mcp__")]


def _results(bus: _RecordingBus) -> list[Any]:
    """bus 上 tool_result 事件的 ToolInfo，按发出顺序。"""
    return [event.tool for event in bus.events if event.type.value == "tool_result"]


# ---------- user_agent 主回合：只看到绑定的 server 与白名单内的工具 ----------


async def test_user_agent_turn_sees_only_bound_servers_and_tools(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    world = _World()
    _install(monkeypatch, world, {})
    config = _config(
        11,
        AgentMcpBinding(source="mcp:github", tool_names=frozenset({_GH_PR})),
        AgentMcpBinding(source="mcp:jira", tool_names=None),
    )
    _install_agents(monkeypatch, turn_config_module, agent=config)
    world.store.create_session(_SESSION_ID, workspace_id=_WORKSPACE_ID, user_id=_USER_ID)
    world.store.sessions[_SESSION_ID].agent_id = 11
    tools = _tools()
    llm = _scripted(_call(_JIRA))
    turn = await _start_turn(world, "查")

    runtime = _main_runtime(world, llm, FakeScopedSource(*tools.values()))
    await runtime._produce(_SESSION_ID, turn.id, "查")

    # 平台工具按 Agent 的 tool_runtime_names 裁剪，作用域工具按绑定选：slack 未绑定，github 只留白名单。
    assert [_tool_names(context) for context in llm.requests] == [["Read", _GH_PR, _JIRA]] * 2
    ((_, ctx),) = tools[_JIRA].calls
    assert ctx.profile == "user_agent"
    # 网关带着全部作用域工具，可见名单只有绑定选中的，留给子执行按名查策略。
    assert ctx.scoped == ScopedGrant(
        tools=tuple(tools.values()), visible=frozenset({_GH_PR, _JIRA})
    )
    assert world.store.turns[turn.id].status == "completed"


# ---------- Agent 工具：子代理与委派经真实 SubAgentRunner 拿到作用域工具 ----------


class _RunnerLauncher:
    """子代理启动器：经组装根工厂装配真实 SubAgentRunner，模型、记忆与运行记录换成替身。

    记下每次收到的 scoped，并把子代理的事件发到 bus，用于核对子代理路径的来源。
    """

    def __init__(self, llm: _StreamLLM, bus: _RecordingBus) -> None:
        self._llm = llm
        self._bus = bus
        self.grants: list[ScopedGrant | None] = []

    async def run(self, session_id: int, prompt: str, spec: Any, **kwargs: Any) -> str:
        self.grants.append(kwargs.get("scoped"))
        kwargs.pop("bus", None)
        runner = make_subagent_runner(_Db(), bus=self._bus, llm=self._llm, memory=_NoMemory())
        runner.team_service.run_repo = _RunRepo()  # type: ignore[assignment]
        return await runner.run(session_id, prompt, spec, **kwargs)


def _lead_with_launcher(
    world: _World, lead_llm: _StreamLLM, launcher: _RunnerLauncher, source: FakeScopedSource
):
    """主运行时：Agent 工具经 launcher 启动子代理（_produce 按它重建回合配置）；其余同内存现场。"""
    runtime = _main_runtime(world, lead_llm, source)
    runtime.subagents = launcher  # type: ignore[assignment]
    return runtime


@pytest.mark.parametrize(
    ("agent_type", "expected"),
    [("general-purpose", [_GH_ISSUE, _GH_PR, _JIRA, _SLACK]), ("Explore", [])],
)
async def test_lead_agent_tool_hands_scoped_to_subagent_by_spec(
    monkeypatch: pytest.MonkeyPatch, agent_type: str, expected: list[str]
) -> None:
    world = _World()
    _install(monkeypatch, world, {})
    tools = _tools()
    lead_llm = _scripted(_call("Agent", arguments={"agent_type": agent_type, "prompt": "去查"}))
    sub_llm = _StreamLLM(ModelTurnResult(text="子报告", stop_reason="end_turn"))
    launcher = _RunnerLauncher(sub_llm, _RecordingBus())
    turn = await _start_turn(world, "派活")

    runtime = _lead_with_launcher(world, lead_llm, launcher, FakeScopedSource(*tools.values()))
    await runtime._produce(_SESSION_ID, turn.id, "派活")

    (grant,) = launcher.grants
    assert grant is not None and grant.tools == tuple(tools.values())
    assert sorted(grant.visible) == sorted(expected)
    # 子代理发给模型的工具：平台工具按规格裁剪在前，继承的 MCP 工具在后。
    assert _scoped_names(sub_llm.requests[0]) == expected
    assert world.store.turns[turn.id].status == "completed"


@pytest.mark.parametrize("plan_mode", [False, True], ids=["standard", "plan_mode"])
async def test_lead_delegate_gets_binding_intersect_parent_round(
    monkeypatch: pytest.MonkeyPatch, plan_mode: bool
) -> None:
    world = _World()
    _install(monkeypatch, world, {})
    delegate = _config(
        7,
        AgentMcpBinding(source="mcp:github", tool_names=frozenset({_GH_PR})),
        AgentMcpBinding(source="mcp:slack", tool_names=None),
    )
    _install_agents(monkeypatch, turn_config_module, delegate=delegate)
    # Agent 工具在函数体内从 agent.service 局部导入，替身同样挂到那里。
    monkeypatch.setattr(
        "agent.service.AgentDefinitionService", turn_config_module.AgentDefinitionService
    )
    tools = _tools()
    lead_llm = _scripted(_call("Agent", arguments={"agent_id": 7, "prompt": "审"}))
    sub_llm = _StreamLLM(ModelTurnResult(text="委派报告", stop_reason="end_turn"))
    launcher = _RunnerLauncher(sub_llm, _RecordingBus())
    turn = await _start_turn(world, "委派")

    runtime = _lead_with_launcher(world, lead_llm, launcher, FakeScopedSource(*tools.values()))
    if plan_mode:
        active = SimpleNamespace(content="只读方案")
        runtime.plan_service = SimpleNamespace(  # type: ignore[assignment]
            get_active=_returning(active)
        )
    await runtime._produce(_SESSION_ID, turn.id, "委派")

    if plan_mode:
        # Plan 模式的审核规则只放行 Explore / Plan 子代理，委派在派发前就被拒，不会把 MCP 工具交出去；
        # 交集逻辑本身对 Plan 模式的本轮名单同样成立，见 test_agent_mcp_bindings.py。
        assert launcher.grants == []
        result = world.store.session_messages(_SESSION_ID)[2][1]
        assert (result["output"], result["is_error"]) == (
            "Plan Mode只能派发Explore或Plan只读子代理。",
            True,
        )
        return
    (grant,) = launcher.grants
    assert grant is not None and grant.tools == tuple(tools.values())
    # 被委派 Agent 自己的绑定（github 白名单 + slack 全部）∩ 父回合本轮可见（全部）。
    assert grant.visible == frozenset({_GH_PR, _SLACK})
    assert _scoped_names(sub_llm.requests[0]) == [_GH_PR, _SLACK]


def _returning(value: Any):
    async def _call_it(*args: Any, **kwargs: Any) -> Any:
        return value

    return _call_it


# ---------- 子代理路径：事件与 tool_calls 带来源 ----------


def _subagent(monkeypatch: pytest.MonkeyPatch, llm: _StreamLLM) -> tuple[SubAgentRunner, _World]:
    """经组装根装配真实子代理运行器：真实审核链、网关与 ToolService，模型与记录换成替身。"""
    world = _World()
    _install(monkeypatch, world, {})
    runner = make_subagent_runner(_Db(), bus=_RecordingBus(), llm=llm, memory=_NoMemory())
    runner.team_service.run_repo = _RunRepo()  # type: ignore[assignment]
    return runner, world


async def test_subagent_events_and_tool_calls_carry_source(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tools = _tools()
    readable = {"file_path": __file__}
    llm = _scripted(_call(_JIRA, "call-s"), _call("Read", "call-b", readable))
    runner, world = _subagent(monkeypatch, llm)
    bus: _RecordingBus = runner.bus  # type: ignore[assignment]
    grant = ScopedGrant(tools=tuple(tools.values()), visible=frozenset({_JIRA}))

    await runner.run(
        _SESSION_ID,
        "去查",
        get_subagent_spec(AgentType.GENERAL_PURPOSE),
        parent_allowed_tool_names=frozenset({"Read", _JIRA}),
        scoped=grant,
    )

    events = [event.to_dict() for event in bus.events]
    tool_uses = [e["tool"] for e in events if e["type"] == "tool_use"]
    tool_results = [e["tool"] for e in events if e["type"] == "tool_result"]
    # 作用域工具带 source，内置工具不带（None 不序列化，事件形状与原来一致）。
    assert [(t["name"], t.get("source")) for t in tool_uses] == [
        (_JIRA, "mcp:jira"),
        ("Read", None),
    ]
    assert [(t["name"], t.get("source")) for t in tool_results] == [
        (_JIRA, "mcp:jira"),
        ("Read", None),
    ]
    # tool_calls.source 由 ToolService.start_call 按策略全集写入。
    records = {record.tool_name: record for record in world.tool_calls.values()}
    assert getattr(records[_JIRA], "source", None) == "mcp:jira"
    assert getattr(records["Read"], "source", None) is None


@pytest.mark.parametrize(
    ("builtin_name", "requires_approval"),
    [("Weather", False), ("Write", True)],
    ids=["no_approval", "requires_approval"],
)
async def test_explore_hidden_mcp_call_is_reviewed_like_hidden_builtin(
    monkeypatch: pytest.MonkeyPatch, builtin_name: str, requires_approval: bool
) -> None:
    """Explore 收到的作用域工具全不可见；调用其中一个时，审核结论与同等策略、同样不可见的内置工具一致。"""
    hidden = FakeScopedTool(_JIRA, source="mcp:jira", requires_approval=requires_approval)
    builtin_call = ToolCall(id="call-b", tool_name=builtin_name, arguments={"query": "北京"})
    scoped_call = ToolCall(id="call-s", tool_name=hidden.name, arguments={"query": "北京"})
    llm = _scripted(builtin_call, scoped_call)
    runner, _ = _subagent(monkeypatch, llm)
    bus: _RecordingBus = runner.bus  # type: ignore[assignment]
    executed: list[str] = []

    async def _gateway_run(self: Any, session_id: int, tool_name: str, *_: Any, **__: Any):
        executed.append(tool_name)
        return ToolOutput.ok("网关结果")

    # 网关的 run 换成记录替身：只比较审核结论与是否交给网关，不真正执行内置工具。
    monkeypatch.setattr("tools.gateway.ToolGateway.run", _gateway_run)
    explore = get_subagent_spec(AgentType.EXPLORE)
    parent = ScopedGrant(tools=(hidden,), visible=frozenset({hidden.name}))
    grant = explore.inherit_scoped(parent, None)

    await runner.run(_SESSION_ID, "去找", explore, scoped=grant)

    assert _scoped_names(llm.requests[0]) == []
    builtin_result, scoped_result = _results(bus)
    assert scoped_result.is_error == builtin_result.is_error
    assert scoped_result.output == builtin_result.output.replace(builtin_name, hidden.name)
    assert scoped_result.source == "mcp:jira" and builtin_result.source is None
    if requires_approval:
        # 需审批：子代理没有审批通道，两者都被合并为拒绝，都不执行。
        assert scoped_result.is_error is True and executed == []
    else:
        # 不需审批：能力检查按策略全集放行，两者都交给网关，与内置工具的现有行为一致。
        assert scoped_result.is_error is False and executed == [builtin_name, hidden.name]


# ---------- 队友：按成员规格继承 ----------


async def test_woken_teammates_inherit_scoped_by_member_spec(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    world = _World()
    _install(monkeypatch, world, {})
    members = [
        SimpleNamespace(id=1, name="写手", status="working", agent_type="general-purpose"),
        SimpleNamespace(id=2, name="侦察", status="working", agent_type="Explore"),
        SimpleNamespace(id=3, name="闲人", status="idle", agent_type=None),
    ]
    monkeypatch.setattr(
        agent_module, "TeamService", lambda db: SimpleNamespace(list_members=_returning(members))
    )
    woken: dict[str, ScopedGrant | None] = {}

    class _Teammates:
        def __init__(self, db: object, *, bus: object | None) -> None:
            pass

        async def run_teammate(self, session_id: int, name: str, spec: Any, **kwargs: Any) -> str:
            woken[name] = kwargs["scoped"]
            return "推进完成"

    tools = _tools()
    llm = _StreamLLM(ModelTurnResult(text="好的。", stop_reason="end_turn"))
    runtime = _main_runtime(world, llm, FakeScopedSource(*tools.values()))
    # 队友运行器工厂与独立会话由组装根注入，这里换成记录替身。
    runtime._teammates = _Teammates  # type: ignore[assignment]
    runtime._session_factory = _NullScope  # type: ignore[assignment]
    turn = await _start_turn(world, "推进")

    await runtime._produce(_SESSION_ID, turn.id, "推进")

    every = tuple(tools.values())
    assert woken == {
        "写手": ScopedGrant(tools=every, visible=frozenset(tools)),
        "侦察": ScopedGrant(tools=every, visible=frozenset()),
    }


class _NullScope:
    async def __aenter__(self) -> object:
        return object()

    async def __aexit__(self, *exc_info: object) -> bool:
        return False


# ---------- Workflow：执行体开头解析一次，成员各按自己的绑定拿到工具 ----------


def _install_workflow(monkeypatch: pytest.MonkeyPatch, *agent_ids: int) -> None:
    """顺序编排的 Workflow 替身：成员即 agent_ids，按给定顺序执行。"""

    class _Workflows:
        def __init__(self, db: object) -> None:
            pass

        async def require_runnable(self, workflow_id: int, workspace_id: int) -> Any:
            return SimpleNamespace(id=workflow_id, mode="sequential")

        async def get_run_members(self, workflow_id: int, workspace_id: int) -> tuple[Any, ...]:
            return tuple(
                WorkflowRunMember(agent_id=agent_id, sort_order=index)
                for index, agent_id in enumerate(agent_ids)
            )

    monkeypatch.setattr(workflow_module, "WorkflowService", _Workflows)


async def test_workflow_members_each_get_their_own_bound_scoped_tools(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    world = _World()
    _install(monkeypatch, world, {})
    first = _config(21, AgentMcpBinding(source="mcp:github", tool_names=frozenset({_GH_ISSUE})))
    second = _config(22, AgentMcpBinding(source="mcp:jira", tool_names=None))
    third = _config(23)
    _install_agents(monkeypatch, workflow_module, first=first, second=second, third=third)
    _install_workflow(monkeypatch, 21, 22, 23)
    tools = _tools()
    source = FakeScopedSource(*tools.values())
    member_llm = _StreamLLM(
        *(ModelTurnResult(text=f"结论{index}", stop_reason="end_turn") for index in range(3))
    )
    grants: list[ScopedGrant | None] = []
    harness = replace(default_harness(), scoped_tools=(source,))

    def _runner(db: Any, *, bus: Any) -> SubAgentRunner:
        runner = harness.subagent_runner(db, bus=bus, llm=member_llm, memory=_NoMemory())
        runner.team_service.run_repo = _RunRepo()  # type: ignore[assignment]
        original = runner.run

        async def _run(*args: Any, **kwargs: Any) -> str:
            grants.append(kwargs.get("scoped"))
            return await original(*args, **kwargs)

        runner.run = _run  # type: ignore[method-assign]
        return runner

    runtime = make_workflow_runtime(
        _Db(),
        user_id=_USER_ID,
        workspace_id=_WORKSPACE_ID,
        harness=harness,
        llm=member_llm,
        sessions=world.store,
    )
    # 成员运行器工厂由组装根注入；这里包一层，记下每个成员收到的 scoped，并注入模型与记录替身。
    runtime._runners = _runner  # type: ignore[assignment]
    runtime.bus = _RecordingBus()  # type: ignore[assignment]
    turn = await _start_turn(world, "跑流程")

    await runtime._produce(_SESSION_ID, turn.id, "跑流程", 5)

    # 执行体开头解析一次，全部成员共用；每个成员只看到自己绑定的 server。
    assert source.scopes == [_SCOPE]
    every = tuple(tools.values())
    assert grants == [
        ScopedGrant(tools=every, visible=frozenset({_GH_ISSUE})),
        ScopedGrant(tools=every, visible=frozenset({_JIRA})),
        ScopedGrant(tools=every, visible=frozenset()),
    ]
    assert [_scoped_names(context) for context in member_llm.requests] == [[_GH_ISSUE], [_JIRA], []]
    assert world.store.turns[turn.id].status == "completed"


async def test_workflow_runtime_gets_resolver_from_harness() -> None:
    source = FakeScopedSource()
    harness = replace(default_harness(), scoped_tools=(source,))

    runtime = make_workflow_runtime(
        object(), user_id=_USER_ID, workspace_id=_WORKSPACE_ID, harness=harness
    )
    rebuilt = runtime._rebuild(object())

    assert isinstance(runtime.tool_resolver, TurnToolResolver)
    assert runtime.tool_resolver._sources == (source,)
    assert runtime.tool_resolver._platform is harness.tools
    assert rebuilt.tool_resolver._sources == (source,)


async def test_orchestrator_without_resolved_gives_members_no_scoped() -> None:
    """BaseOrchestrator 的 resolved 带默认值 None：直接构造（黄金测试的用法）时成员不收作用域工具。"""
    orchestrator = SequentialOrchestrator(
        db=object(),
        bus=_RecordingBus(),
        user_id=_USER_ID,
        workspace_id=_WORKSPACE_ID,
        session_id=_SESSION_ID,
        turn_id=uuid4(),
        workflow_id=5,
        members=(),
        runners=make_subagent_runner,
        session_factory=_NullScope,
    )
    platform_only = ResolvedScope.platform_only(default_harness().tools)

    assert orchestrator.resolved is None
    assert orchestrator._member_scoped(_config(21)) is None
    orchestrator.resolved = platform_only
    assert orchestrator._member_scoped(_config(21)) is None


async def test_workflow_emits_tool_sources_after_turn_start_only_on_fault(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    world = _World()
    _install(monkeypatch, world, {})
    _install_agents(monkeypatch, workflow_module, only=_config(21))
    _install_workflow(monkeypatch, 21)
    failing = FakeScopedSource(error=RuntimeError("连不上"))
    member_llm = _StreamLLM(ModelTurnResult(text="结论", stop_reason="end_turn"))
    harness = replace(default_harness(), scoped_tools=(failing,))

    def _runner(db: Any, *, bus: Any) -> SubAgentRunner:
        runner = harness.subagent_runner(db, bus=bus, llm=member_llm, memory=_NoMemory())
        runner.team_service.run_repo = _RunRepo()  # type: ignore[assignment]
        return runner

    runtime = make_workflow_runtime(
        _Db(),
        user_id=_USER_ID,
        workspace_id=_WORKSPACE_ID,
        harness=harness,
        llm=member_llm,
        sessions=world.store,
    )
    runtime._runners = _runner  # type: ignore[assignment]
    runtime.bus = bus = _RecordingBus()  # type: ignore[assignment]
    turn = await _start_turn(world, "跑")

    await runtime._produce(_SESSION_ID, turn.id, "跑", 5)

    types = [event.to_dict()["type"] for event in bus.events]
    assert types[:3] == ["session_ready", "turn_start", "tool_sources"]
    (fault,) = bus.events[2].data["faults"]
    assert (fault["source"], fault["kind"]) == ("mcp", "unavailable")
    assert world.store.turns[turn.id].status == "completed"
