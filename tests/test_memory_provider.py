"""记忆端口 MemoryProvider 的替换性与内置实现 BuiltinMemoryProvider 的单元测试。

替换性：假的 Provider 注入 AgentRuntime 与 SubAgentRunner 后，召回、回合写入、提交后动作与子代理继承
都经它完成，后台执行重建运行时也沿用它。内置实现：recall / inherited / on_turn_completed / after_commit
分别只调用 memory 域 Service 的公开方法，事务动作与失败隔离按端口约定。
"""

import logging
from dataclasses import replace
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock
from uuid import UUID

import pytest

from core.config import settings as app_settings
from core.errors import AgentException
from harness.adapters.memory import builtin_provider as builtin_module
from harness.adapters.memory.builtin_provider import BuiltinMemoryProvider
from harness.app.memory_port import MemoryProvider
from harness.app.ports import ModelEventKind, ModelStreamEvent, ModelTurnResult
from harness.domain.memory import MemoryRecall, MemoryWriteTicket, TurnRef
from memory.jobs.service import MemoryJobService
from memory.recall.service import MemoryRecallService
from runtime import agent as agent_module
from runtime import lead_loop as lead_loop_module
from runtime.agent import AgentRuntime
from runtime.subagent import SubAgentRunner

pytestmark = pytest.mark.anyio

_TURN_ID = UUID("00000000-0000-4000-8000-00000000f001")
_JOB_ID = UUID("00000000-0000-4000-8000-00000000f002")
_TURN = TurnRef(workspace_id=2, user_id=3, session_id=1, turn_id=_TURN_ID)


class _FakeMemory(MemoryProvider):
    """记录每次调用的假 Provider，召回与继承返回预设结果，回合写入返回固定回执。"""

    def __init__(self, recall: MemoryRecall | None = None) -> None:
        self.result = recall
        self.calls: list[tuple[str, Any]] = []

    async def recall(self, turn: TurnRef) -> MemoryRecall | None:
        self.calls.append(("recall", turn))
        return self.result

    async def inherited(self, turn: TurnRef) -> MemoryRecall | None:
        self.calls.append(("inherited", turn))
        return self.result

    async def on_turn_completed(self, turn: TurnRef) -> MemoryWriteTicket | None:
        self.calls.append(("on_turn_completed", turn))
        return MemoryWriteTicket(turn=turn, ref="ticket-1")

    async def after_commit(self, ticket: MemoryWriteTicket) -> None:
        self.calls.append(("after_commit", ticket))


class _RecordingLLM:
    """模型替身：记下每次调用的系统指令，直接给出终局文本。"""

    provider = "anthropic"
    model = "claude-test"

    def __init__(self) -> None:
        self.instructions: list[Any] = []

    async def stream(self, context: Any, settings: Any) -> Any:
        self.instructions.append(context.instruction)
        yield ModelStreamEvent(
            ModelEventKind.TURN_RESULT,
            result=ModelTurnResult(text="好", stop_reason="end_turn"),
        )


class _Db:
    """数据库会话替身：按序记下提交、回滚与 SAVEPOINT 动作。"""

    def __init__(self) -> None:
        self.ops: list[str] = []

    async def commit(self) -> None:
        self.ops.append("commit")

    async def rollback(self) -> None:
        self.ops.append("rollback")

    async def begin_nested(self) -> Any:
        self.ops.append("begin_nested")
        ops = self.ops

        class _Nested:
            async def commit(self) -> None:
                ops.append("savepoint.commit")

            async def rollback(self) -> None:
                ops.append("savepoint.rollback")

        return _Nested()


# ---------- 替换性：假的 Provider 注入运行时 ----------


def test_agent_runtime_defaults_to_builtin_provider() -> None:
    runtime = AgentRuntime(object(), user_id=3, workspace_id=2, llm=object())  # type: ignore[arg-type]

    assert isinstance(runtime.memory, BuiltinMemoryProvider)
    assert runtime._injected_memory() is None


async def test_injected_provider_drives_recall_and_turn_write(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """召回结果分别进系统提示末段与本轮用户消息前缀，回合完成后先提交再交给 after_commit。"""
    fake = _FakeMemory(MemoryRecall(instruction="- [k](memory:1@1) - 目录", message_prefix="正文"))
    db = _Db()
    llm = _RecordingLLM()
    runtime = AgentRuntime(db, user_id=3, workspace_id=2, llm=llm, memory=fake)  # type: ignore[arg-type]
    prefixes: list[Any] = []

    async def _load(turn: Any, additional_context: Any, rendered_memories: Any = None) -> Any:
        prefixes.append(rendered_memories)
        return [], [{"role": "user", "content": "你好"}], None

    runtime.sessions = SimpleNamespace(
        load_context_for_turn=_load,
        add_message=AsyncMock(return_value=SimpleNamespace(id=9)),
        mark_turn_status=AsyncMock(),
        set_status=AsyncMock(),
    )
    runtime.compact_service = SimpleNamespace(maybe_compact=AsyncMock(return_value=[]))
    runtime.plan_service = SimpleNamespace(get_active=AsyncMock(return_value=None))
    runtime._apply_stop_hooks = AsyncMock(return_value=None)
    runtime.bus = SimpleNamespace(emit=AsyncMock())
    monkeypatch.setattr(lead_loop_module.SkillService, "get_catalog", AsyncMock(return_value=""))
    turn = SimpleNamespace(id=_TURN_ID, session_id=1, user_id=3, workspace_id=2)

    session = SimpleNamespace(system_prompt=None, model_name=None)
    await runtime._run_llm_loop(1, session, turn, None)
    ticket = await runtime._complete_turn(SimpleNamespace(id=1), turn, 9)
    await runtime.memory.after_commit(ticket)

    assert [name for name, _ in fake.calls] == ["recall", "on_turn_completed", "after_commit"]
    assert fake.calls[0][1] == _TURN
    assert prefixes == ["正文"]
    assert llm.instructions[0][-1].text == "## Memory Catalog\n- [k](memory:1@1) - 目录"
    # 召回命中后先提交一次，供子代理继承；回合终态再提交一次。
    assert db.ops == ["commit", "commit"]
    assert ticket == MemoryWriteTicket(turn=_TURN, ref="ticket-1")


async def test_turbo_mode_skips_injected_provider() -> None:
    fake = _FakeMemory()
    db = _Db()
    runtime = AgentRuntime(db, user_id=3, workspace_id=2, llm=object(), memory=fake)  # type: ignore[arg-type]
    runtime.configuration = replace(runtime.configuration, mode_slug="turbo")
    runtime.sessions = SimpleNamespace(
        mark_turn_status=AsyncMock(), set_status=AsyncMock()
    )
    turn = SimpleNamespace(id=_TURN_ID, session_id=1, user_id=3, workspace_id=2)

    assert await runtime._complete_turn(SimpleNamespace(id=1), turn, 9) is None
    assert fake.calls == []
    assert db.ops == ["commit"]


async def test_injected_provider_survives_background_rebuild(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """后台执行用独立数据库会话重建运行时，注入的 Provider 与注入的客户端一样随之传递。"""
    fake = _FakeMemory()
    llm = object()
    runtime = AgentRuntime(_Db(), user_id=3, workspace_id=2, llm=llm, memory=fake)  # type: ignore[arg-type]
    runtime.sessions = SimpleNamespace(
        prepare_for_message=AsyncMock(return_value=SimpleNamespace(id=1)),
        start_turn=AsyncMock(return_value=(SimpleNamespace(id=_TURN_ID), None)),
        claim_turn_lease=AsyncMock(return_value=0),
    )
    rebuilt: list[AgentRuntime] = []

    class _Scope:
        async def __aenter__(self) -> object:
            return _Db()

        async def __aexit__(self, *exc_info: object) -> bool:
            return False

    async def _produce(self: AgentRuntime, *args: Any) -> None:
        rebuilt.append(self)

    async def _launch(*, body: Any, **kwargs: Any) -> None:
        await body(SimpleNamespace())

    monkeypatch.setattr(agent_module, "AsyncSessionLocal", _Scope)
    monkeypatch.setattr(agent_module, "launch_turn_execution", _launch)
    monkeypatch.setattr(AgentRuntime, "_produce", _produce)

    await runtime.start(None, "你好")

    assert rebuilt[0].memory is fake
    assert rebuilt[0].llm is llm


async def test_subagent_inherits_through_injected_provider() -> None:
    fake = _FakeMemory(MemoryRecall(instruction="目录", message_prefix="父回合正文"))
    runner = SubAgentRunner(_Db(), llm=object(), memory=fake)  # type: ignore[arg-type]

    inherited = await runner._load_frozen_memories(1, _TURN_ID, 3, 2)
    skipped = await runner._load_frozen_memories(1, None, 3, 2)

    assert inherited == "父回合正文"
    assert skipped is None
    assert fake.calls == [("inherited", _TURN)]


def test_subagent_runner_defaults_to_builtin_provider() -> None:
    runner = SubAgentRunner(object(), llm=object())  # type: ignore[arg-type]

    assert isinstance(runner.memory, BuiltinMemoryProvider)


# ---------- 内置实现：只调用 memory 域 Service 的公开方法 ----------


def _context(catalog: str = "目录", memories: str = "正文") -> SimpleNamespace:
    return SimpleNamespace(rendered_catalog=catalog, rendered_memories=memories)


async def test_builtin_recall_maps_frozen_context(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[tuple[Any, ...]] = []

    async def _get_or_create(self: Any, workspace_id: int, user_id: int, turn_id: UUID, **kw: Any):
        calls.append((workspace_id, user_id, turn_id, kw))
        return _context("- [k](memory:1@1) - 目录", "<relevant_memories/>")

    monkeypatch.setattr(MemoryRecallService, "get_or_create_context", _get_or_create)

    recall = await BuiltinMemoryProvider(_Db()).recall(_TURN)  # type: ignore[arg-type]

    assert recall == MemoryRecall(
        instruction="- [k](memory:1@1) - 目录", message_prefix="<relevant_memories/>"
    )
    assert calls == [(2, 3, _TURN_ID, {"session_id": 1})]


async def test_builtin_recall_returns_none_when_not_frozen(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        MemoryRecallService, "get_or_create_context", AsyncMock(return_value=None)
    )

    assert await BuiltinMemoryProvider(_Db()).recall(_TURN) is None  # type: ignore[arg-type]


async def test_builtin_inherited_reads_frozen_context_only(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """继承只读冻结结果，不经 get_or_create_context，不会触发新的召回。"""
    frozen: list[tuple[Any, ...]] = []

    async def _get_frozen(self: Any, workspace_id: int, user_id: int, turn_id: UUID, **kw: Any):
        frozen.append((workspace_id, user_id, turn_id, kw))
        return _context(memories="")

    create = AsyncMock()
    monkeypatch.setattr(MemoryRecallService, "get_frozen_context", _get_frozen)
    monkeypatch.setattr(MemoryRecallService, "get_or_create_context", create)
    provider = BuiltinMemoryProvider(_Db())  # type: ignore[arg-type]

    recall = await provider.inherited(_TURN)
    monkeypatch.setattr(MemoryRecallService, "get_frozen_context", AsyncMock(return_value=None))
    missing = await provider.inherited(_TURN)

    assert recall == MemoryRecall(instruction="目录", message_prefix="")
    assert missing is None
    assert frozen == [(2, 3, _TURN_ID, {"session_id": 1})]
    create.assert_not_awaited()


async def test_builtin_turn_write_disabled_does_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(app_settings, "memory_extraction_enabled", False)
    enqueue = AsyncMock()
    monkeypatch.setattr(MemoryJobService, "enqueue_extract", enqueue)
    db = _Db()

    assert await BuiltinMemoryProvider(db).on_turn_completed(_TURN) is None  # type: ignore[arg-type]
    assert db.ops == []
    enqueue.assert_not_awaited()


async def test_builtin_turn_write_enqueues_inside_savepoint(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.setattr(app_settings, "memory_extraction_enabled", True)
    enqueued: list[tuple[int, UUID]] = []

    async def _enqueue(self: Any, session_id: int, turn_id: UUID) -> Any:
        enqueued.append((session_id, turn_id))
        return SimpleNamespace(id=_JOB_ID, payload={"mode": "shadow"})

    monkeypatch.setattr(MemoryJobService, "enqueue_extract", _enqueue)
    db = _Db()

    with caplog.at_level(logging.INFO, logger=builtin_module.__name__):
        ticket = await BuiltinMemoryProvider(db).on_turn_completed(_TURN)  # type: ignore[arg-type]

    assert ticket == MemoryWriteTicket(turn=_TURN, ref=str(_JOB_ID))
    assert enqueued == [(1, _TURN_ID)]
    # 只开关 SAVEPOINT，不提交调用方的事务。
    assert db.ops == ["begin_nested", "savepoint.commit"]
    (record,) = [r for r in caplog.records if r.getMessage() == "Memory提取任务已入队"]
    assert (record.job_id, record.mode) == (str(_JOB_ID), "shadow")


async def test_builtin_turn_write_failure_rolls_back_savepoint_only(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.setattr(app_settings, "memory_extraction_enabled", True)
    monkeypatch.setattr(
        MemoryJobService,
        "enqueue_extract",
        AsyncMock(side_effect=AgentException.message("只有归属一致且已完成的交互轮次才能创建提取Job")),
    )
    db = _Db()

    with caplog.at_level(logging.ERROR, logger=builtin_module.__name__):
        ticket = await BuiltinMemoryProvider(db).on_turn_completed(_TURN)  # type: ignore[arg-type]

    assert ticket is None
    assert db.ops == ["begin_nested", "savepoint.rollback"]
    (record,) = caplog.records
    assert record.getMessage() == "创建Memory提取任务失败，本轮不提取记忆"
    assert (record.session_id, record.turn_id) == ("1", str(_TURN_ID))


@pytest.mark.parametrize("inline", [True, False])
async def test_builtin_after_commit_runs_job_in_turn_scope(
    monkeypatch: pytest.MonkeyPatch, inline: bool
) -> None:
    monkeypatch.setattr(app_settings, "memory_extraction_inline", inline)
    runs: list[tuple[Any, UUID | None]] = []

    async def _run_once(self: Any, *, job_id: UUID | None = None) -> int:
        runs.append((self.claim_scope, job_id))
        return 1

    monkeypatch.setattr(builtin_module.MemoryJobRunner, "run_once", _run_once)

    await BuiltinMemoryProvider(_Db()).after_commit(  # type: ignore[arg-type]
        MemoryWriteTicket(turn=_TURN, ref=str(_JOB_ID))
    )

    assert runs == ([((2, 3), _JOB_ID)] if inline else [])


async def test_builtin_after_commit_failure_only_logs(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.setattr(app_settings, "memory_extraction_inline", True)
    monkeypatch.setattr(
        builtin_module.MemoryJobRunner, "run_once", AsyncMock(side_effect=RuntimeError("认领失败"))
    )

    with caplog.at_level(logging.ERROR, logger=builtin_module.__name__):
        await BuiltinMemoryProvider(_Db()).after_commit(  # type: ignore[arg-type]
            MemoryWriteTicket(turn=_TURN, ref=str(_JOB_ID))
        )

    (record,) = caplog.records
    assert record.getMessage() == "就地执行Memory提取任务失败，等待独立Worker接管"
    assert record.job_id == str(_JOB_ID)
