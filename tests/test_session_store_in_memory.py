"""会话存储端口的替换性：注入内存实现，不连数据库、不打桩 SessionService，跑通主 Agent 回合。

第一轮「工具调用 → 回答」：准入、回合循环、工具执行与回合完成都只经 SessionStore 读写会话，
断言内存里的消息序列与轮次、会话状态；第二轮的历史超过阈值，触发压缩并经同一端口写入快照。
另外核对端口与数据库实现 SessionService 的方法签名逐一对齐（SessionService 不写适配器直接充当实现）。
"""

import annotationlib
import inspect
import typing
from collections.abc import AsyncIterator
from dataclasses import replace
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import pytest

from harness.app.memory_port import MemoryProvider
from harness.app.ports import (
    ModelEventKind,
    ModelStreamEvent,
    ModelTurnResult,
    ResolvedModel,
    ToolCallReviewer,
)
from harness.app.session_port import SessionStore, TurnLeaseStore
from harness.bootstrap import Harness, default_harness
from harness.domain.memory import MemoryRecall, MemoryWriteTicket, TurnRef
from harness.domain.messages import ToolCall
from harness.domain.review import Decision, ReviewRequest
from harness.domain.tool import ToolOutput
from hooks import HookRegistry
from runtime import agent as agent_module
from runtime import lead_loop as lead_loop_module
from runtime.agent import AgentRuntime
from runtime.turn_config import TurnConfiguration
from session.service import SessionService
from tests.fixtures.harness_factory import (
    make_agent_runtime,
    make_subagent_runner,
    make_workflow_runtime,
)
from tests.fixtures.in_memory_session_store import InMemorySessionStore, MemorySnapshot

pytestmark = pytest.mark.anyio

_SESSION_ID = 7
_USER_ID = 3
_WORKSPACE_ID = 2
_ECHO_CALL = ToolCall(id="call-1", tool_name="echo", arguments={"text": "北京"})


class _ScriptedLLM:
    """模型替身：按脚本依次给出终局结果，记下每次请求的消息。"""

    provider = "anthropic"
    model = "claude-memory-store"

    def __init__(self, *results: ModelTurnResult) -> None:
        self._results = list(results)
        self.requests: list[Any] = []

    async def stream(self, context: Any, settings: Any) -> AsyncIterator[ModelStreamEvent]:
        self.requests.append(context)
        yield ModelStreamEvent(ModelEventKind.TURN_RESULT, result=self._results.pop(0))


class _NoMemory(MemoryProvider):
    """记忆替身：不召回、不写入，让回合只经过会话存储。"""

    async def recall(self, turn: TurnRef) -> MemoryRecall | None:
        return None

    async def inherited(self, turn: TurnRef) -> MemoryRecall | None:
        return None

    async def on_turn_completed(self, turn: TurnRef) -> MemoryWriteTicket | None:
        return None

    async def after_commit(self, ticket: MemoryWriteTicket) -> None:
        return None


class _Db:
    """数据库会话替身：运行时只在挂起点与回合终态提交，这里只计数，不连真实数据库。"""

    def __init__(self) -> None:
        self.ops: list[str] = []

    async def commit(self) -> None:
        self.ops.append("commit")

    async def flush(self) -> None:
        self.ops.append("flush")

    async def rollback(self) -> None:
        self.ops.append("rollback")


class _AllowAll(ToolCallReviewer):
    """审核员替身：一律放行，审核规则链由 tool_review 黄金测试覆盖。"""

    async def review(self, call: ToolCall, request: ReviewRequest) -> Decision:
        return Decision.allow(call.arguments, source="test")


class _EchoGateway:
    """工具网关替身：工具调用记录属 tools 域，这里只记下调用并给出固定输出。"""

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []

    async def run(
        self, session_id: int, tool_name: str, arguments: dict[str, Any], **_: Any
    ) -> ToolOutput:
        self.calls.append((tool_name, arguments))
        return ToolOutput.ok("晴")


def _install(monkeypatch: pytest.MonkeyPatch, gateway: _EchoGateway) -> None:
    """把会话以外的协作者换成替身：回合配置的查库、Hook、任务基线、Skill 目录与队友唤醒。"""

    async def _configure(
        db: Any, session: Any, *, injected: Any, tools: Any, subagents: Any, **_: Any
    ) -> TurnConfiguration:
        configuration = TurnConfiguration.for_lead(
            db, tools=tools, subagents=subagents, model=ResolvedModel.from_client(injected)
        )
        return replace(configuration, gateway=gateway)

    monkeypatch.setattr(agent_module, "configure_turn", _configure)
    monkeypatch.setattr(agent_module, "get_hook_registry", HookRegistry)
    monkeypatch.setattr(
        agent_module, "TaskService", lambda db, bus: SimpleNamespace(emit_snapshot=AsyncMock())
    )
    monkeypatch.setattr(
        agent_module,
        "TeamService",
        lambda db: SimpleNamespace(list_members=AsyncMock(return_value=[])),
    )
    monkeypatch.setattr(lead_loop_module.SkillService, "get_catalog", AsyncMock(return_value=""))


def _runtime(
    db: _Db,
    store: InMemorySessionStore,
    llm: _ScriptedLLM,
    harness: Harness | None = None,
) -> AgentRuntime:
    runtime = make_agent_runtime(
        db,
        user_id=_USER_ID,
        workspace_id=_WORKSPACE_ID,
        harness=harness,
        llm=llm,
        reviewer=_AllowAll(),
        memory=_NoMemory(),
        sessions=store,
    )
    runtime.plan_service = SimpleNamespace(get_active=AsyncMock(return_value=None))  # type: ignore[assignment]
    return runtime


async def _start_turn(store: InMemorySessionStore, content: str) -> Any:
    """请求阶段的准入：经同一个存储把会话标为运行中并建轮次。"""
    session = await store.prepare_for_message(_SESSION_ID, content, _USER_ID, _WORKSPACE_ID)
    turn, _ = await store.start_turn(session, _USER_ID, _WORKSPACE_ID, content)
    return turn


async def test_lead_turn_runs_against_in_memory_store(monkeypatch: pytest.MonkeyPatch) -> None:
    """工具调用 → 回答：消息、轮次与会话状态全部落进内存存储，次序与数据库实现一致。"""
    gateway = _EchoGateway()
    _install(monkeypatch, gateway)
    store = InMemorySessionStore()
    store.create_session(_SESSION_ID, workspace_id=_WORKSPACE_ID, user_id=_USER_ID)
    llm = _ScriptedLLM(
        ModelTurnResult(tool_calls=[_ECHO_CALL], stop_reason="tool_use"),
        ModelTurnResult(text="北京今天晴。", stop_reason="end_turn"),
    )
    db = _Db()
    turn = await _start_turn(store, "北京天气怎么样？")

    await _runtime(db, store, llm)._produce(_SESSION_ID, turn.id, "北京天气怎么样？")

    assert store.session_messages(_SESSION_ID) == [
        ("user", "北京天气怎么样？"),
        ("assistant", [{"kind": "tool_use", "id": "call-1", "name": "echo", "input": {"text": "北京"}}]),
        (
            "tool",
            {
                "tool_use_id": "call-1",
                "tool_name": "echo",
                "input_args": {"text": "北京"},
                "output": "晴",
                "is_error": False,
            },
        ),
        ("assistant", [{"kind": "text", "text": "北京今天晴。"}]),
    ]
    assert gateway.calls == [("echo", {"text": "北京"})]
    final = store.messages[-1]
    assert (final.provider, final.model) == ("anthropic", "claude-memory-store")
    assert store.turns[turn.id].status == "completed"
    assert store.turns[turn.id].completed_message_id == final.id
    assert store.sessions[_SESSION_ID].status == "idle"
    assert store.snapshots == []
    # 第二次模型调用看到的上下文就是存储里的完整本轮消息，说明加载也走了内存实现。
    assert len(llm.requests[1].messages) == 3
    # 提交时点不变：回合终态一次提交，工具阶段只 flush。
    assert db.ops == ["flush", "commit"]


async def test_second_turn_compacts_history_into_snapshot(monkeypatch: pytest.MonkeyPatch) -> None:
    """历史超过阈值时压缩，摘要经 save_compact_snapshot 写进内存存储，水位是上一轮最后一条消息。"""
    _install(monkeypatch, _EchoGateway())
    store = InMemorySessionStore()
    store.create_session(_SESSION_ID, workspace_id=_WORKSPACE_ID, user_id=_USER_ID)
    first = _ScriptedLLM(
        ModelTurnResult(tool_calls=[_ECHO_CALL], stop_reason="tool_use"),
        ModelTurnResult(text="北京今天晴。", stop_reason="end_turn"),
    )
    turn = await _start_turn(store, "北京天气怎么样？")
    await _runtime(_Db(), store, first)._produce(_SESSION_ID, turn.id, "北京天气怎么样？")
    watermark = store.messages[-1].id

    second = _ScriptedLLM(ModelTurnResult(text="上海多云。", stop_reason="end_turn"))
    runtime = _runtime(_Db(), store, second)
    compact_llm = _ScriptedLLM(ModelTurnResult(text="问过北京天气，答复晴。"))
    runtime.compact_service.model = ResolvedModel.from_client(compact_llm)  # type: ignore[arg-type]
    runtime.compact_service.threshold = 1
    turn = await _start_turn(store, "那上海呢？")
    await runtime._produce(_SESSION_ID, turn.id, "那上海呢？")

    summary = "[Conversation compressed.]\n\n问过北京天气，答复晴。"
    assert store.snapshots == [
        MemorySnapshot(
            session_id=_SESSION_ID,
            messages=[{"role": "user", "content": summary}],
            summary=summary,
            through_message_id=watermark,
        )
    ]
    # 本轮请求 = 快照摘要 + 本轮用户消息；落库的消息不受压缩影响。
    assert [m.to_content() for m in second.requests[0].messages] == [summary, "那上海呢？"]
    assert store.session_messages(_SESSION_ID)[-2:] == [
        ("user", "那上海呢？"),
        ("assistant", [{"kind": "text", "text": "上海多云。"}]),
    ]
    # 再加载一次：上下文以快照替换水位及之前的消息，不再重复带上第一轮。
    history, current, through = await store.load_context_for_turn(store.turns[turn.id])
    assert history == [{"role": "user", "content": summary}]
    assert through == watermark
    assert [row["role"] for row in current] == ["user", "assistant"]


def test_runtimes_fall_back_to_session_service_and_keep_injected_store() -> None:
    """未注入时由组装根装配数据库实现；注入的存储贯穿运行时的各个协作者。"""
    db = object()
    store = InMemorySessionStore()
    default = make_agent_runtime(db, user_id=1, workspace_id=1, llm=object())
    injected = make_agent_runtime(db, user_id=1, workspace_id=1, llm=object(), sessions=store)

    assert isinstance(default.sessions, SessionService)
    # 默认实现不随后台执行传递：后台重建时绑定新的数据库会话另建一个。
    rebuilt_default = default._rebuild(object())
    assert isinstance(rebuilt_default.sessions, SessionService)
    assert rebuilt_default.sessions is not default.sessions
    assert injected.sessions is store
    assert injected._rebuild(object()).sessions is store
    assert injected.compact_service.snapshots is store
    assert isinstance(make_workflow_runtime(db).sessions, SessionService)
    assert make_workflow_runtime(db, sessions=store).sessions is store
    assert isinstance(make_subagent_runner(db, llm=object()).sessions, SessionService)
    assert make_subagent_runner(db, llm=object(), sessions=store).sessions is store


async def test_injected_store_survives_background_rebuild(monkeypatch: pytest.MonkeyPatch) -> None:
    """后台执行在独立数据库会话里重建运行时，注入的存储与注入的模型、记忆一样随之传递。"""

    class _Scope:
        async def __aenter__(self) -> _Db:
            return _Db()

        async def __aexit__(self, *exc_info: object) -> bool:
            return False

    store = InMemorySessionStore()
    store.create_session(_SESSION_ID, workspace_id=_WORKSPACE_ID, user_id=_USER_ID)
    harness = replace(default_harness(), session_factory=_Scope)
    runtime = _runtime(_Db(), store, _ScriptedLLM(), harness)
    rebuilt: list[AgentRuntime] = []

    async def _produce(self: AgentRuntime, *args: Any) -> None:
        rebuilt.append(self)

    async def _launch(*, body: Any, **kwargs: Any) -> None:
        await body(SimpleNamespace())

    monkeypatch.setattr(agent_module, "launch_turn_execution", _launch)
    monkeypatch.setattr(AgentRuntime, "_produce", _produce)

    await runtime.start(_SESSION_ID, "你好")

    assert rebuilt[0] is not runtime
    assert rebuilt[0].sessions is store
    assert store.turns[next(iter(store.turns))].lease_owner is not None


@pytest.mark.parametrize("port", [SessionStore, TurnLeaseStore], ids=["store", "lease"])
def test_session_service_signatures_match_port(port: type) -> None:
    """SessionService 不经适配器直接充当数据库实现：端口每个方法都在，参数名、种类与默认值逐一一致。

    ORM 实体的 Mapped 字段静态检查器认不成只读属性，所以这里按签名运行期核对，补静态检查的缺口。
    """
    for name in sorted(typing.get_protocol_members(port)):
        assert _shape(getattr(SessionService, name)) == _shape(getattr(port, name)), name
        assert inspect.iscoroutinefunction(getattr(SessionService, name)), name


def test_in_memory_store_signatures_match_port() -> None:
    """内存实现覆盖 SessionStore 的全部方法，签名与端口一致。"""
    for name in sorted(typing.get_protocol_members(SessionStore)):
        assert _shape(getattr(InMemorySessionStore, name)) == _shape(getattr(SessionStore, name)), name


def _shape(method: Any) -> list[tuple[str, Any, Any]]:
    """方法的参数名、种类与默认值。

    注解按字符串读取、不求值：SessionService 自带名为 list 的方法，类体里的 list[...] 注解
    在 3.14 惰性求值时会解析成该方法而报错，签名比对只需要形状。
    """
    signature = inspect.signature(method, annotation_format=annotationlib.Format.STRING)
    return [(p.name, p.kind, p.default) for p in signature.parameters.values()]
