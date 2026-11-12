"""MCP 运行时接线（M1c）：故障事件、模型请求的工具顺序、审批挂起与恢复核对、422 / 403、
私有 server 可见性、落库与 SSE 的来源、手动刷新后立即生效，以及一条端到端链路。

大部分用例用 tests/test_scoped_tools.py 的内存现场（会话存储、调用记录、交互暂停点），
作用域来源换成按回合逐次给出工具的假来源；端到端用例走真实的 McpToolProvider、
tests/fixtures/mcp_fake.py 的假 server（ASGITransport，不联网、不监听端口）和内存版 McpServerSource。
"""

from dataclasses import replace
from types import SimpleNamespace
from typing import Any

import mcp_types as t
import pytest

from core.errors import AgentException
from core.events import BlockType, ToolInfo
from harness.adapters.mcp.provider import McpProviderLimits, McpToolProvider
from harness.adapters.mcp.tool import McpTool
from harness.app.ports import (
    ModelEventKind,
    ModelStreamEvent,
    ModelTurnResult,
    ScopedToolProvider,
    ScopedTools,
    ToolSourceFault,
)
from harness.bootstrap import default_harness
from harness.domain.mcp import (
    ApprovalMode,
    McpServerEntry,
    McpToolRef,
    mcp_tool_policy,
    schema_digest,
)
from harness.domain.messages import ToolCall, ToolResult
from harness.domain.tool import ToolContext, ToolOutput, ToolScope
from hooks import HookContext, HookEvent, HookRegistry
from tests.fixtures.harness_factory import make_agent_runtime
from tests.fixtures.mcp_fake import FakeMcpServer, StaticServers, make_config, mcp_harness
from tests.test_scoped_tools import (
    _SESSION_ID,
    _USER_ID,
    _WORKSPACE_ID,
    _Db,
    _install,
    _NoMemory,
    _RecordingBus,
    _start_turn,
    _tool_messages,
    _tool_names,
    _World,
)
from tools import service as tool_service_module
from workspace.service import WorkspaceService

pytestmark = pytest.mark.anyio

_SCOPE = ToolScope(workspace_id=_WORKSPACE_ID, user_id=_USER_ID)
_PLATFORM = default_harness().tools
_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {"query": {"type": "string"}},
    "required": ["query"],
}


def _mcp_tool(
    remote: str,
    *,
    key: str = "github",
    server_id: int = 11,
    config_rev: int = 1,
    schema: dict[str, Any] | None = None,
    requires_approval: bool = True,
    output: ToolOutput | None = None,
    calls: list[tuple[str, dict[str, Any]]] | None = None,
) -> McpTool:
    """真实的 McpTool；调用回调换成内存实现，记下 (模型名, 入参) 并返回给定结果。"""
    parameters = schema or _SCHEMA
    ref = McpToolRef(
        server_id=server_id,
        server_key=key,
        config_rev=config_rev,
        remote_name=remote,
        model_name=f"mcp__{key}__{remote}",
        schema_sha256=schema_digest(parameters),
    )
    record = calls if calls is not None else []
    result = output or ToolOutput.ok(f"{remote} 的结果")

    async def call(target: McpToolRef, arguments: dict[str, Any], ctx: ToolContext, timeout: float):
        record.append((target.model_name, arguments))
        text = t.TextContent(text=result.text)
        return t.CallToolResult(content=[text], is_error=result.is_error)

    return McpTool(
        ref=ref,
        description=f"{remote} 工具",
        parameters=parameters,
        policy=mcp_tool_policy(ref.model_name, requires_approval=requires_approval),
        call=call,
        call_timeout=5.0,
        max_output_chars=20000,
    )


class _TurnSource(ScopedToolProvider):
    """按回合逐次给出作用域工具的假来源：第 n 次解析取 rounds[n]（用完沿用最后一组），记下身份。"""

    def __init__(self, *rounds: ScopedTools) -> None:
        self._rounds = list(rounds)
        self.scopes: list[ToolScope] = []

    @property
    def name(self) -> str:
        return "mcp"

    async def list_tools(self, scope: ToolScope) -> ScopedTools:
        self.scopes.append(scope)
        index = min(len(self.scopes), len(self._rounds)) - 1
        return self._rounds[index]


class _ScriptedLLM:
    """模型替身：按脚本给出结果；工具调用同时以 TOOL_USE 流事件发出，记下每次请求的 Context。"""

    provider = "anthropic"
    model = "claude-mcp"

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


def _scripted(*calls: ToolCall) -> _ScriptedLLM:
    return _ScriptedLLM(
        ModelTurnResult(tool_calls=list(calls), stop_reason="tool_use"),
        ModelTurnResult(text="完成。", stop_reason="end_turn"),
    )


def _call(tool: Any, call_id: str = "call-1", query: str = "北京") -> ToolCall:
    return ToolCall(id=call_id, tool_name=tool.name, arguments={"query": query})


def _runtime(
    world: _World,
    llm: _ScriptedLLM,
    *sources: ScopedToolProvider,
    bus: _RecordingBus | None = None,
    user_id: int = _USER_ID,
):
    """经组装根装配主运行时：作用域来源、会话存储、模型与记忆换成替身，其余走真实实现。"""
    harness = replace(default_harness(), scoped_tools=sources)
    runtime = make_agent_runtime(
        _Db(),
        user_id=user_id,
        workspace_id=_WORKSPACE_ID,
        harness=harness,
        llm=llm,
        memory=_NoMemory(),
        sessions=world.store,
    )
    runtime.interaction_service = world.interactions  # type: ignore[assignment]

    async def _no_plan(*args: Any) -> None:
        return None

    runtime.plan_service = SimpleNamespace(get_active=_no_plan)  # type: ignore[assignment]
    runtime.bus = bus or _RecordingBus()  # type: ignore[assignment]
    return runtime


def _frames(bus: _RecordingBus) -> list[dict[str, Any]]:
    return [event.to_dict() for event in bus.events]


async def _suspend(world: _World, llm: _ScriptedLLM, source: _TurnSource, *calls: ToolCall) -> Any:
    """跑一个回合直到审批挂起，返回待处理的交互请求。"""
    turn = await _start_turn(world, "处理一下")
    await _runtime(world, llm, source)._produce(_SESSION_ID, turn.id, "处理一下")
    assert world.interactions.request.kind == "tool_approval"
    return world.interactions.request


async def _approve(
    world: _World, llm: _ScriptedLLM, source: _TurnSource, response: dict[str, Any]
) -> _RecordingBus:
    """请求路径保存审批响应，再在后台恢复执行；返回恢复执行的事件总线。"""
    request = world.interactions.request
    approver = _runtime(world, llm, source)
    await approver.prepare_interaction_response(_SESSION_ID, request.id, "tool_approval", response)
    bus = _RecordingBus()
    await _runtime(world, llm, source, bus=bus)._produce_resume(_SESSION_ID, request.id)
    return bus


# ---------- 模型请求、故障事件与 SSE ----------


async def test_model_request_lists_builtin_then_mcp_tools_sorted(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    world = _World()
    _install(monkeypatch, world, {})
    jira = _mcp_tool("search", key="jira", server_id=12, requires_approval=False)
    issue = _mcp_tool("create_issue", requires_approval=False)
    prs = _mcp_tool("list_prs", requires_approval=False)
    llm = _ScriptedLLM(ModelTurnResult(text="好。", stop_reason="end_turn"))
    turn = await _start_turn(world, "看看")

    await _runtime(world, llm, _TurnSource(ScopedTools(tools=(jira, prs, issue))))._produce(
        _SESSION_ID, turn.id, "看看"
    )

    builtin = [tool.name for tool in _PLATFORM.without("ExitPlanMode", "WritePlan").tools]
    # 内置工具按 BuiltinToolProvider 顺序在前，MCP 工具按模型名字典序在后。
    assert _tool_names(llm.requests[0]) == [
        *builtin,
        "mcp__github__create_issue",
        "mcp__github__list_prs",
        "mcp__jira__search",
    ]


async def test_faults_emit_tool_sources_after_turn_start(monkeypatch: pytest.MonkeyPatch) -> None:
    world = _World()
    _install(monkeypatch, world, {})
    faults = (
        ToolSourceFault(source="mcp:jira", server_id=12, kind="timeout", message="获取超时"),
        ToolSourceFault(source="mcp:wiki", server_id=13, kind="unavailable", message="连接失败"),
    )
    bus = _RecordingBus()
    llm = _ScriptedLLM(ModelTurnResult(text="好。", stop_reason="end_turn"))
    turn = await _start_turn(world, "看看")

    source = _TurnSource(ScopedTools(tools=(), faults=faults))
    await _runtime(world, llm, source, bus=bus)._produce(_SESSION_ID, turn.id, "看看")

    types = [frame["type"] for frame in _frames(bus)]
    start = types.index("turn_start")
    assert types[start + 1] == "tool_sources"
    assert _frames(bus)[start + 1] == {
        "type": "tool_sources",
        "data": {
            "session_id": _SESSION_ID,
            "faults": [
                {"source": "mcp:jira", "server_id": 12, "kind": "timeout", "message": "获取超时"},
                {
                    "source": "mcp:wiki",
                    "server_id": 13,
                    "kind": "unavailable",
                    "message": "连接失败",
                },
            ],
        },
    }


async def test_no_fault_no_mcp_keeps_event_stream_unchanged(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """没有故障时不发 tool_sources；内置工具的 tool_use / tool_result 不带 source。"""
    world = _World()
    _install(monkeypatch, world, {})
    bus = _RecordingBus()
    read = ToolCall(id="call-r", tool_name="Read", arguments={"file_path": "/nope/a.txt"})
    turn = await _start_turn(world, "读")

    await _runtime(world, _scripted(read), _TurnSource(ScopedTools(tools=())), bus=bus)._produce(
        _SESSION_ID, turn.id, "读"
    )

    frames = _frames(bus)
    assert "tool_sources" not in [frame["type"] for frame in frames]
    tool_frames = [frame["tool"] for frame in frames if "tool" in frame]
    assert tool_frames and all("source" not in tool for tool in tool_frames)


async def test_sse_and_tool_calls_carry_source_and_error(monkeypatch: pytest.MonkeyPatch) -> None:
    world = _World()
    _install(monkeypatch, world, {})
    observed: list[HookContext] = []

    async def _observe(ctx: HookContext) -> None:
        observed.append(ctx)

    hooks = HookRegistry()
    hooks.register(HookEvent.POST_TOOL_USE, _observe)
    monkeypatch.setattr(tool_service_module, "get_hook_registry", lambda: hooks)
    failing = _mcp_tool(
        "fail", requires_approval=False, output=ToolOutput.error("远程报错：参数不合法")
    )
    bus = _RecordingBus()
    turn = await _start_turn(world, "试试")

    source = _TurnSource(ScopedTools(tools=(failing,)))
    await _runtime(world, _scripted(_call(failing)), source, bus=bus)._produce(
        _SESSION_ID, turn.id, "试试"
    )

    tool_frames = [frame for frame in _frames(bus) if frame["type"] in {"tool_use", "tool_result"}]
    assert [(frame["type"], frame["tool"]["source"]) for frame in tool_frames] == [
        ("tool_use", "mcp:github"),
        ("tool_result", "mcp:github"),
    ]
    assert tool_frames[1]["tool"]["is_error"] is True
    record = world.tool_calls[1]
    # isError 记为失败调用，来源建出记录后写入；PostToolUse 载荷带 is_error。
    assert (record.status, record.error_message) == ("failed", "远程报错：参数不合法")
    assert record.source == "mcp:github"
    assert [(ctx.tool_name, ctx.is_error) for ctx in observed] == [(failing.name, True)]


# ---------- 审批挂起与恢复核对 ----------


async def test_mcp_approval_payload_and_continuation_refs(monkeypatch: pytest.MonkeyPatch) -> None:
    world = _World()
    _install(monkeypatch, world, {})
    guarded = _mcp_tool("delete_repo")
    read = ToolCall(id="call-r", tool_name="Read", arguments={"file_path": "/nope/a.txt"})
    llm = _scripted(_call(guarded), read)

    request = await _suspend(world, llm, _TurnSource(ScopedTools(tools=(guarded,))))

    assert request.request_payload == {
        "tool": {
            "id": "call-1",
            "name": guarded.name,
            "input": {"query": "北京"},
            "source": "mcp:github",
        },
        "reason": "危险工具默认需审批",
        "allowed_decisions": ["allow_once", "always_allow", "deny"],
        "can_update_input": False,
    }
    continuation = world.interactions.suspension.continuation_payload
    # 只冻结 MCP 调用的路由键；内置调用不进 mcp_refs。
    assert continuation["mcp_refs"] == {"call-1": guarded.ref.to_dict()}
    assert set(continuation) == {
        "assistant_message_id",
        "pending",
        "done_tool_use_ids",
        "additional_context",
        "tool_budget",
        "mcp_refs",
    }
    assert world.tool_calls[1].source == "mcp:github"


async def test_builtin_approval_keeps_payload_and_continuation_shape(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    world = _World()
    _install(monkeypatch, world, {})
    write = ToolCall(id="call-w", tool_name="Write", arguments={"file_path": "/x", "content": "y"})
    other = _mcp_tool("search", requires_approval=False)

    request = await _suspend(world, _scripted(write), _TurnSource(ScopedTools(tools=(other,))))

    assert "source" not in request.request_payload["tool"]
    assert request.request_payload["can_update_input"] is True
    assert "mcp_refs" not in world.interactions.suspension.continuation_payload
    assert getattr(world.tool_calls[1], "source", None) is None


async def test_resume_executes_when_route_unchanged(monkeypatch: pytest.MonkeyPatch) -> None:
    world = _World()
    _install(monkeypatch, world, {})
    calls: list[tuple[str, dict[str, Any]]] = []
    guarded = _mcp_tool("delete_repo", calls=calls)
    source = _TurnSource(ScopedTools(tools=(guarded,)))
    llm = _scripted(_call(guarded))
    await _suspend(world, llm, source)

    bus = await _approve(world, llm, source, {"decision": "allow_once"})

    assert calls == [(guarded.name, {"query": "北京"})]
    assert world.tool_calls[1].status == "succeeded"
    results = [frame["tool"] for frame in _frames(bus) if frame["type"] == "tool_result"]
    assert results[0]["source"] == "mcp:github"


_CHANGED = "MCP 工具已变更，本次审批的调用未执行"


def _variants(calls: list[tuple[str, dict[str, Any]]]) -> dict[str, tuple[Any, ...]]:
    """恢复时重新解析出的三种变化：server 已删除、config_rev 变化、schema 变化。"""
    wider = {**_SCHEMA, "properties": {**_SCHEMA["properties"], "force": {"type": "boolean"}}}
    return {
        "server_deleted": (),
        "config_rev_changed": (_mcp_tool("delete_repo", config_rev=2, calls=calls),),
        "schema_changed": (_mcp_tool("delete_repo", schema=wider, calls=calls),),
    }


@pytest.mark.parametrize("change", ["server_deleted", "config_rev_changed", "schema_changed"])
async def test_resume_rejects_changed_route_and_continues_others(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Any, change: str
) -> None:
    """批准后工具已变：被批准的调用与同批里同样已变的调用都记为拒绝，其余调用照常执行。"""
    world = _World()
    _install(monkeypatch, world, {})
    calls: list[tuple[str, dict[str, Any]]] = []
    first = _mcp_tool("delete_repo", calls=calls)
    second_call = ToolCall(id="call-2", tool_name=first.name, arguments={"query": "上海"})
    note = tmp_path / "a.txt"
    note.write_text("内容", encoding="utf-8")
    read = ToolCall(id="call-r", tool_name="Read", arguments={"file_path": str(note)})
    changed = _variants(calls)[change]
    source = _TurnSource(ScopedTools(tools=(first,)), ScopedTools(tools=changed))
    llm = _scripted(_call(first), second_call, read)
    await _suspend(world, llm, source)
    assert set(world.interactions.suspension.continuation_payload["mcp_refs"]) == {
        "call-1",
        "call-2",
    }

    bus = await _approve(world, llm, source, {"decision": "allow_once"})

    # 不会在用户没批准过的地址或 schema 上执行。
    assert calls == []
    results = {
        frame["tool"]["id"]: frame["tool"]
        for frame in _frames(bus)
        if frame["type"] == "tool_result"
    }
    for call_id in ("call-1", "call-2"):
        assert (results[call_id]["output"], results[call_id]["is_error"]) == (_CHANGED, True)
    assert results["call-r"]["is_error"] is False
    assert world.tool_calls[1].status == "rejected"
    assert world.interactions.suspension.status == "resolved"
    assert world.store.turns[next(iter(world.store.turns))].status == "completed"
    # 下一轮发给模型的工具结果里，被拒的调用带着同一段文本。
    sent = [m for m in llm.requests[-1].messages if isinstance(m, ToolResult)]
    assert [(m.tool_call_id, m.content) for m in sent[:2]] == [
        ("call-1", _CHANGED),
        ("call-2", _CHANGED),
    ]


# ---------- 审批响应：改参数 422、全局「始终允许」的管理员限制 ----------


async def test_mcp_approval_with_updated_input_is_422(monkeypatch: pytest.MonkeyPatch) -> None:
    world = _World()
    _install(monkeypatch, world, {})
    guarded = _mcp_tool("delete_repo")
    source = _TurnSource(ScopedTools(tools=(guarded,)))
    request = await _suspend(world, _scripted(_call(guarded)), source)

    with pytest.raises(AgentException) as exc_info:
        await _runtime(world, _ScriptedLLM(), source).prepare_interaction_response(
            _SESSION_ID,
            request.id,
            "tool_approval",
            {"decision": "allow_once", "updated_input": {"query": "上海"}},
        )

    assert exc_info.value.status_code == 422
    assert exc_info.value.message == "该工具不支持在审批时修改参数"
    # 响应没有落库，暂停点仍可用正确的响应恢复。
    assert world.interactions.request.response_payload is None


class _Roles:
    """WorkspaceService.require_workspace_role 的替身：按用户给出角色，记下每次校验。"""

    def __init__(self, roles: dict[int, str]) -> None:
        self.roles = roles
        self.checked: list[tuple[int, int, set[str]]] = []

    def install(self, monkeypatch: pytest.MonkeyPatch) -> None:
        roles = self

        async def _require(self: Any, workspace_id: int, user_id: int, allowed: set[str]) -> Any:
            roles.checked.append((workspace_id, user_id, set(allowed)))
            if roles.roles.get(user_id) not in allowed:
                raise AgentException.message("无权执行该工作区操作", status_code=403)
            return SimpleNamespace(role=roles.roles[user_id])

        monkeypatch.setattr(WorkspaceService, "require_workspace_role", _require)


_GLOBAL_ALLOW = {"decision": "always_allow", "always_scope": "global"}


@pytest.mark.parametrize(("role", "allowed"), [("member", False), ("admin", True), ("owner", True)])
async def test_mcp_global_always_allow_requires_admin(
    monkeypatch: pytest.MonkeyPatch, role: str, allowed: bool
) -> None:
    world = _World()
    _install(monkeypatch, world, {})
    roles = _Roles({_USER_ID: role})
    roles.install(monkeypatch)
    guarded = _mcp_tool("delete_repo")
    source = _TurnSource(ScopedTools(tools=(guarded,)))
    request = await _suspend(world, _scripted(_call(guarded)), source)
    runtime = _runtime(world, _ScriptedLLM(), source)

    if allowed:
        await runtime.prepare_interaction_response(
            _SESSION_ID, request.id, "tool_approval", _GLOBAL_ALLOW
        )
        assert world.interactions.request.response_payload == _GLOBAL_ALLOW
    else:
        with pytest.raises(AgentException) as exc_info:
            await runtime.prepare_interaction_response(
                _SESSION_ID, request.id, "tool_approval", _GLOBAL_ALLOW
            )
        assert exc_info.value.status_code == 403
        assert world.interactions.request.response_payload is None
    assert roles.checked == [(_WORKSPACE_ID, _USER_ID, {"owner", "admin"})]


@pytest.mark.parametrize(
    "response",
    [{"decision": "always_allow"}, {"decision": "allow_once"}],
    ids=["session_scope", "allow_once"],
)
async def test_mcp_non_global_decisions_skip_role_check(
    monkeypatch: pytest.MonkeyPatch, response: dict[str, Any]
) -> None:
    world = _World()
    _install(monkeypatch, world, {})
    roles = _Roles({_USER_ID: "member"})
    roles.install(monkeypatch)
    guarded = _mcp_tool("delete_repo")
    source = _TurnSource(ScopedTools(tools=(guarded,)))
    request = await _suspend(world, _scripted(_call(guarded)), source)

    await _runtime(world, _ScriptedLLM(), source).prepare_interaction_response(
        _SESSION_ID, request.id, "tool_approval", response
    )

    assert roles.checked == []


async def test_builtin_global_always_allow_keeps_current_behavior(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """内置工具的 always_scope=global 与改参数照旧：不查角色，updated_input 按 schema 规范化。"""
    world = _World()
    _install(monkeypatch, world, {})
    roles = _Roles({_USER_ID: "member"})
    roles.install(monkeypatch)
    write = ToolCall(id="call-w", tool_name="Write", arguments={"file_path": "/x", "content": "y"})
    source = _TurnSource(ScopedTools(tools=()))
    request = await _suspend(world, _scripted(write), source)
    response = {**_GLOBAL_ALLOW, "updated_input": {"file_path": "/z", "content": "w"}}

    await _runtime(world, _ScriptedLLM(), source).prepare_interaction_response(
        _SESSION_ID, request.id, "tool_approval", response
    )

    assert roles.checked == []
    assert world.interactions.request.response_payload["updated_input"]["file_path"] == "/z"


# ---------- 真实 McpToolProvider：私有 server、端到端、手动刷新 ----------

_OTHER_USER = 4


def _limits() -> McpProviderLimits:
    return McpProviderLimits(
        list_timeout=3.0, call_timeout=3.0, max_output_chars=20000, max_schema_bytes=16384
    )


def _echo_server() -> FakeMcpServer:
    fake = FakeMcpServer(ttl_ms=60_000)
    fake.add(
        "lookup",
        lambda args: t.CallToolResult(content=[t.TextContent(text=f"查到 {args['query']}")]),
        schema=_SCHEMA,
        annotations=t.ToolAnnotations(read_only_hint=True),
    )
    return fake


async def test_private_server_only_in_owner_turns(monkeypatch: pytest.MonkeyPatch) -> None:
    world = _World()
    _install(monkeypatch, world, {})
    shared = McpServerEntry(make_config(server_id=1, server_key="team", workspace_id=_WORKSPACE_ID))
    private = McpServerEntry(
        make_config(
            server_id=2, server_key="mine", workspace_id=_WORKSPACE_ID, owner_user_id=_USER_ID
        )
    )
    async with mcp_harness(_echo_server()) as h:
        servers = StaticServers(shared, private)
        provider = McpToolProvider(runtime=h.runtime, servers=servers, limits=_limits())
        owner_llm = _ScriptedLLM(ModelTurnResult(text="好。", stop_reason="end_turn"))
        other_llm = _ScriptedLLM(ModelTurnResult(text="好。", stop_reason="end_turn"))
        turn = await _start_turn(world, "看看")
        await _runtime(world, owner_llm, provider)._produce(_SESSION_ID, turn.id, "看看")
        other = _runtime(world, other_llm, provider, user_id=_OTHER_USER)
        resolved = await other._resolve_scope()

    owner_names = [name for name in _tool_names(owner_llm.requests[0]) if name.startswith("mcp__")]
    assert owner_names == ["mcp__mine__lookup", "mcp__team__lookup"]
    # 同一工作区的其他成员解析不到别人的私有 server。
    assert [tool.name for tool in resolved.scoped] == ["mcp__team__lookup"]
    assert servers.scopes == [_SCOPE, ToolScope(workspace_id=_WORKSPACE_ID, user_id=_OTHER_USER)]


async def test_end_to_end_resolve_call_and_result_back_to_model(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """解析 → 发给模型 → 审核 → 经 Streamable HTTP 调用假 server → 结果回给模型，全程不联网。"""
    world = _World()
    _install(monkeypatch, world, {})
    fake = _echo_server()
    entry = McpServerEntry(
        make_config(
            server_id=1,
            server_key="docs",
            workspace_id=_WORKSPACE_ID,
            approval_mode=ApprovalMode.TRUST_ANNOTATIONS,
        )
    )
    bus = _RecordingBus()
    call = ToolCall(id="call-1", tool_name="mcp__docs__lookup", arguments={"query": "北京"})
    llm = _scripted(call)
    turn = await _start_turn(world, "查文档")
    async with mcp_harness(fake) as h:
        provider = McpToolProvider(
            runtime=h.runtime, servers=StaticServers(entry), limits=_limits()
        )
        await _runtime(world, llm, provider, bus=bus)._produce(_SESSION_ID, turn.id, "查文档")

    assert _tool_names(llm.requests[0])[-1] == "mcp__docs__lookup"
    # 只读注解在 trust_annotations 下免审，直接执行；server 收到原名与原样入参。
    assert fake.calls == [("lookup", {"query": "北京"})]
    sent = llm.requests[1].messages[-1]
    assert isinstance(sent, ToolResult)
    assert (sent.tool_call_id, sent.content, sent.is_error) == ("call-1", "查到 北京", False)
    assert _tool_messages(world)[-1]["output"] == "查到 北京"
    assert world.tool_calls[1].source == "mcp:docs"
    assert world.tool_calls[1].status == "succeeded"
    assert world.store.turns[turn.id].status == "completed"


async def test_manual_refresh_makes_next_turn_see_new_catalog() -> None:
    """管理员手动刷新探测成功后清掉本进程的清单缓存：下一回合不用等 TTL 就拿到新清单。"""
    from harness.adapters.mcp.connector import McpConnector
    from harness.app.mcp_admin import McpAdminService
    from tests.test_mcp_admin import InMemoryRegistry, _draft

    fake = _echo_server()
    async with mcp_harness(fake) as h:
        registry = InMemoryRegistry()
        connector = McpConnector(runtime=h.runtime, probe_timeout=5, max_schema_bytes=16384)
        admin = McpAdminService(
            registry=registry,
            connector=connector,
            workspace_id=_WORKSPACE_ID,
            user_id=_USER_ID,
        )
        view = await admin.create_server(_draft(server_key="docs", headers={}, auth_type="none"))
        entry = McpServerEntry(replace(view.config, url="https://mcp.example.com/mcp"))
        provider = McpToolProvider(
            runtime=h.runtime, servers=StaticServers(entry), limits=_limits()
        )
        first = await provider.list_tools(_SCOPE)
        fake.add("archive", schema=_SCHEMA)
        cached = await provider.list_tools(_SCOPE)
        listed_before_refresh = fake.list_calls

        result = await admin.refresh_tools(view.server_id)
        refreshed = await provider.list_tools(_SCOPE)

    assert [tool.name for tool in first.tools] == ["mcp__docs__lookup"]
    # 刷新之前命中缓存，看不到新工具。
    assert [tool.name for tool in cached.tools] == ["mcp__docs__lookup"]
    assert result.error is None and result.diff.added == ("archive",)
    assert [tool.name for tool in refreshed.tools] == ["mcp__docs__archive", "mcp__docs__lookup"]
    # 探测一次、下一回合重新拉取一次。
    assert fake.list_calls == listed_before_refresh + 2


async def test_drop_cached_tools_clears_every_partition_and_last_good() -> None:
    """按 server 清缓存：全部授权分区的 SDK 缓存条目与最近成功清单都清掉，别的 server 不受影响。"""
    fake = _echo_server()
    shared = McpServerEntry(make_config(server_id=1, server_key="team", workspace_id=_WORKSPACE_ID))
    private = McpServerEntry(
        make_config(
            server_id=2, server_key="mine", workspace_id=_WORKSPACE_ID, owner_user_id=_USER_ID
        )
    )
    async with mcp_harness(fake) as h:
        provider = McpToolProvider(
            runtime=h.runtime, servers=StaticServers(shared, private), limits=_limits()
        )
        await provider.list_tools(_SCOPE)
        await provider.list_tools(ToolScope(workspace_id=_WORKSPACE_ID, user_id=_OTHER_USER))
        assert fake.list_calls == 2

        removed = await h.runtime.drop_cached_tools(1)
        await provider.list_tools(_SCOPE)

        remaining = [key.server_id for key in h.runtime._last_good]

    assert removed == 1
    # 只有 server 1 重新拉取；server 2（私有）仍命中缓存。
    assert fake.list_calls == 3
    assert sorted(remaining) == [1, 2]
