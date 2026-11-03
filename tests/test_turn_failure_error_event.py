"""主循环通用失败分支:run_end(failed) 之前补发 error 事件,且不向客户端泄漏内部异常细节。

驱动真实的 _produce、_mark_abnormal_end 与 RunStreamBus(本地事件流后端),只替换数据库、
会话服务与主循环;收尾按 execution._finish 的顺序关闭总线并写入 run_end,
从事件流读出的帧序列即 SSE 订阅方看到的顺序。
"""

import asyncio
import json
import logging
from types import SimpleNamespace
from typing import Any
from uuid import UUID, uuid4

import pytest

from core.errors import AgentException, LeaseLostError
from core.event_bus import RunStreamBus
from core.events import ORCHESTRATOR_ACTOR, RunEvent
from core.run_events import LocalRunEventStore
from core.run_state import TurnState, fold_all, restore
from llm.errors import LLMCallError, LLMConnectionError, LLMStatusError
from runtime import agent as agent_module
from runtime.agent import AgentRuntime, TurnLoopResult
from runtime.execution import run_end_status

pytestmark = pytest.mark.anyio

_SID = 7
_OWNER = "owner-a"
_SECRET = "postgresql://agentos:p4ss@10.0.0.8/agentos"
_GENERIC_MESSAGE = "本轮处理失败，请稍后重试"
_LLM_UNAVAILABLE_MESSAGE = "模型服务暂时不可用，请稍后重试"


class _Counter:
    """替身 session_turns.last_event_offset:执行内 emit 与收尾的 run_end 共用,只增不减。"""

    def __init__(self) -> None:
        self.last = 0

    def reserve(self, count: int) -> int:
        first = self.last + 1
        self.last += count
        return first


class _Offsets:
    """替身 TurnOffsets:按块预留,run_end 的 offset 一定排在已预留块之后。"""

    def __init__(self, counter: _Counter) -> None:
        self._counter = counter
        self._next = 0
        self._remaining = 0

    async def take(self) -> int:
        if self._remaining == 0:
            self._next, self._remaining = self._counter.reserve(4), 4
        offset = self._next
        self._next += 1
        self._remaining -= 1
        return offset


class _Db:
    def __init__(self) -> None:
        self.commits = 0
        self.rollbacks = 0

    async def commit(self) -> None:
        self.commits += 1

    async def rollback(self) -> None:
        self.rollbacks += 1


class _Sessions:
    """替身 SessionService:覆盖 _produce 与 _mark_abnormal_end 用到的方法,状态改在内存对象上。"""

    def __init__(
        self,
        *,
        lock_error: Exception | None = None,
        persist_error: Exception | None = None,
    ) -> None:
        self.session = SimpleNamespace(id=_SID, title="标题", status="running")
        self.turn = SimpleNamespace(id=uuid4(), session_id=_SID, status="running")
        self.lock_error = lock_error
        self.persist_error = persist_error
        self.repo = SimpleNamespace(update_status=self._update_session_status)

    async def get_required(self, session_id: int) -> SimpleNamespace:
        return self.session

    async def get_turn_required(self, turn_id: UUID) -> SimpleNamespace:
        return self.turn

    async def lock_owned_turn(
        self,
        session_id: int,
        turn_id: UUID,
        owner: str,
    ) -> tuple[SimpleNamespace, SimpleNamespace]:
        if self.lock_error is not None:
            raise self.lock_error
        return self.session, self.turn

    async def mark_turn_status(self, turn: SimpleNamespace, status: str, **kwargs: Any) -> None:
        if self.persist_error is not None:
            raise self.persist_error
        turn.status = status

    async def _update_session_status(self, session: SimpleNamespace, status: str) -> None:
        session.status = status


async def _noop(*args: Any, **kwargs: Any) -> None:
    return None


def _raise(exc: BaseException):
    async def _fail(*args: Any, **kwargs: Any) -> Any:
        raise exc

    return _fail


def _runtime(
    monkeypatch: pytest.MonkeyPatch,
    sessions: _Sessions,
    loop: Any,
) -> AgentRuntime:
    """绕过 __init__ 的重依赖;_produce 与 _mark_abnormal_end 保持真实实现。"""
    runtime = AgentRuntime.__new__(AgentRuntime)
    runtime.db = _Db()
    runtime.session_service = sessions
    runtime.mode_slug = "standard"
    monkeypatch.setattr(runtime, "_configure_agent", _noop)
    monkeypatch.setattr(runtime, "_apply_user_prompt_hooks", _noop)
    monkeypatch.setattr(runtime, "_run_llm_loop", loop)
    monkeypatch.setattr(
        agent_module,
        "TaskService",
        lambda db, bus: SimpleNamespace(emit_snapshot=_noop),
    )
    return runtime


async def _drive(
    runtime: AgentRuntime,
    sessions: _Sessions,
    *,
    raises: type[BaseException] | None = None,
) -> tuple[list[dict[str, Any]], dict[str, Any] | None]:
    """按 execution._run/_finish 的顺序执行:run_started → body → close → run_end。

    返回事件流中的全部事件与最后一次 projection。
    """
    turn_key = str(sessions.turn.id)
    store = LocalRunEventStore(maxlen=5000)
    counter = _Counter()
    bus = RunStreamBus(store, turn_key, _Offsets(counter), TurnState())
    runtime.bind_execution(bus, _OWNER)
    await bus.emit(RunEvent.run_started(turn_key, _SID))
    if raises is None:
        await runtime._produce(_SID, sessions.turn.id, "问题")
    else:
        with pytest.raises(raises):
            await runtime._produce(_SID, sessions.turn.id, "问题")
    await bus.close()
    # 与 finish_turn_execution 相同:仍是 running 按中断收尾,run_end 在租约内另取 offset。
    status = run_end_status(sessions.turn.status)
    await bus.write_terminal(RunEvent.run_end(turn_key, status), counter.reserve(1))
    capture = await store.capture(turn_key)
    return [event for _, event in capture.entries], capture.projection


def _types(events: list[dict[str, Any]]) -> list[str]:
    return [event["type"] for event in events]


def _errors(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [event for event in events if event["type"] == "error"]


async def test_business_error_emits_error_before_failed_run_end(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """业务异常:run_end(failed) 之前有一条 error,消息即 AgentException 的中文文案。"""
    sessions = _Sessions()
    exc = AgentException.message(
        "Memory Catalog 不能超过 25600 字节",
        {"internal": _SECRET},
    )
    runtime = _runtime(monkeypatch, sessions, _raise(exc))

    events, _ = await _drive(runtime, sessions)

    assert _types(events) == ["run_started", "session_ready", "error", "run_end"]
    error, run_end = events[-2], events[-1]
    assert error["actor"] == ORCHESTRATOR_ACTOR.to_dict_compact()
    assert error["session_id"] == _SID
    assert error["error"] == {
        "code": "TURN_FAILED",
        "message": "Memory Catalog 不能超过 25600 字节",
        "retriable": False,
        "fatal": True,
    }
    assert run_end["status"] == "failed"
    assert error["sequence"] < run_end["sequence"]
    assert (sessions.turn.status, sessions.session.status) == ("failed", "failed")
    # details 只用于服务端排障,不进入事件流。
    assert _SECRET not in json.dumps(events, ensure_ascii=False)


@pytest.mark.parametrize(
    "exc",
    [
        RuntimeError(f"包含 secret 的内部细节: {_SECRET}"),
        ValueError(f"上游原始响应 {_SECRET}"),
        LLMCallError(f"响应校验失败 {_SECRET}", sdk_error_type="APIResponseValidationError"),
        LLMStatusError(
            f"invalid request {_SECRET}",
            sdk_error_type="BadRequestError",
            status_code=400,
        ),
    ],
    ids=["runtime_error", "value_error", "llm_call_error", "llm_status_400"],
)
async def test_internal_error_uses_generic_message(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    exc: Exception,
) -> None:
    """非业务异常:客户端只拿到固定文案,原始异常文本只进服务端日志。"""
    sessions = _Sessions()
    runtime = _runtime(monkeypatch, sessions, _raise(exc))

    with caplog.at_level(logging.ERROR, logger="runtime.agent"):
        events, _ = await _drive(runtime, sessions)

    assert [error["error"] for error in _errors(events)] == [
        {"code": "TURN_FAILED", "message": _GENERIC_MESSAGE, "retriable": False, "fatal": True},
    ]
    assert _types(events)[-2:] == ["error", "run_end"]
    assert events[-1]["status"] == "failed"
    dumped = json.dumps(events, ensure_ascii=False)
    assert str(exc) not in dumped
    assert _SECRET not in dumped
    logged = [record for record in caplog.records if record.getMessage() == "处理会话消息失败"]
    assert len(logged) == 1
    assert logged[0].exc_info is not None and logged[0].exc_info[1] is exc


@pytest.mark.parametrize(
    "exc",
    [
        LLMConnectionError(f"timeout {_SECRET}", sdk_error_type="APITimeoutError"),
        LLMStatusError(
            f"overloaded {_SECRET}",
            sdk_error_type="InternalServerError",
            status_code=503,
        ),
        LLMStatusError(f"rate limited {_SECRET}", sdk_error_type="RateLimitError", status_code=429),
    ],
    ids=["connection", "status_503", "status_429"],
)
async def test_transient_llm_error_is_retriable(
    monkeypatch: pytest.MonkeyPatch,
    exc: Exception,
) -> None:
    """模型服务暂时不可用:retriable=True,文案仍是通用文案,不带上游原文。"""
    sessions = _Sessions()
    runtime = _runtime(monkeypatch, sessions, _raise(exc))

    events, _ = await _drive(runtime, sessions)

    assert [error["error"] for error in _errors(events)] == [
        {
            "code": "TURN_FAILED",
            "message": _LLM_UNAVAILABLE_MESSAGE,
            "retriable": True,
            "fatal": True,
        },
    ]
    assert events[-1]["status"] == "failed"
    assert _SECRET not in json.dumps(events, ensure_ascii=False)


async def test_snapshot_keeps_turn_error_for_replay(monkeypatch: pytest.MonkeyPatch) -> None:
    """从头 fold 与「projection + 其后事件」两种回放都保留这条错误,刷新续播也能看到。"""
    sessions = _Sessions()
    runtime = _runtime(monkeypatch, sessions, _raise(RuntimeError(_SECRET)))

    events, projection = await _drive(runtime, sessions)

    replayed = fold_all(TurnState(), events)
    refreshed = restore(projection, events)
    for state in (replayed, refreshed):
        assert state.status == "failed"
        assert [item["error"]["code"] for item in state.errors] == ["TURN_FAILED"]
        assert state.errors[0]["error"]["message"] == _GENERIC_MESSAGE
        assert state.errors[0]["sequence"] < state.run_end["sequence"]


async def test_no_error_when_turn_already_completed(monkeypatch: pytest.MonkeyPatch) -> None:
    """完成状态已提交后唤醒队友失败:终态是 completed,不能再补发 fatal error。"""
    sessions = _Sessions()

    async def _loop(*args: Any, **kwargs: Any) -> TurnLoopResult:
        return TurnLoopResult(suspended=False, completed_message_id=1)

    async def _complete(session: Any, turn: Any, message_id: int | None) -> None:
        turn.status = "completed"

    runtime = _runtime(monkeypatch, sessions, _loop)
    monkeypatch.setattr(runtime, "_complete_turn", _complete)
    monkeypatch.setattr(runtime, "_run_inline_memory_job", _noop)
    monkeypatch.setattr(runtime, "_wake_team_members", _raise(RuntimeError("队友列表读取失败")))

    events, _ = await _drive(runtime, sessions)

    assert _errors(events) == []
    assert events[-1]["status"] == "completed"


async def test_no_error_when_lease_lost_while_marking_failed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """标 failed 时发现租约已被清除:终态与 run_end 归清除方,本次执行不发 error。"""
    sessions = _Sessions(lock_error=LeaseLostError("执行租约已失效"))
    runtime = _runtime(monkeypatch, sessions, _raise(RuntimeError("模型调用失败")))

    events, _ = await _drive(runtime, sessions)

    assert _errors(events) == []
    assert sessions.turn.status == "running"


async def test_error_still_emitted_when_failed_state_not_persisted(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """写 failed 的事务本身失败时轮次仍是失败的,用户必须看到 error;run_end 按中断收尾。"""
    sessions = _Sessions(persist_error=RuntimeError(_SECRET))
    runtime = _runtime(monkeypatch, sessions, _raise(RuntimeError("模型调用失败")))

    events, _ = await _drive(runtime, sessions)

    assert [error["error"]["message"] for error in _errors(events)] == [_GENERIC_MESSAGE]
    assert _types(events)[-2:] == ["error", "run_end"]
    assert events[-1]["status"] == "interrupted"
    assert _SECRET not in json.dumps(events, ensure_ascii=False)


async def test_lease_lost_branch_emits_no_error(monkeypatch: pytest.MonkeyPatch) -> None:
    """LeaseLostError 分支:只回滚,不写终态也不发 error。"""
    sessions = _Sessions()
    runtime = _runtime(monkeypatch, sessions, _raise(LeaseLostError("执行租约已失效")))

    events, _ = await _drive(runtime, sessions)

    assert _errors(events) == []
    assert runtime.db.rollbacks == 1
    assert sessions.turn.status == "running"


async def test_cancelled_branch_emits_no_error(monkeypatch: pytest.MonkeyPatch) -> None:
    """CancelledError 分支:绑定执行时只回滚并上抛,中断终态由执行包装层收尾。"""
    sessions = _Sessions()
    runtime = _runtime(monkeypatch, sessions, _raise(asyncio.CancelledError()))

    events, _ = await _drive(runtime, sessions, raises=asyncio.CancelledError)

    assert _errors(events) == []
    assert runtime.db.rollbacks == 1
    assert events[-1]["status"] == "interrupted"


async def test_suspension_committed_failure_emits_no_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """挂起已提交后发布交互事件失败:轮次已在等待交互,只回滚,不按失败处理。"""
    sessions = _Sessions()

    async def _loop(session_id: int, session: Any, turn: Any, *args: Any) -> TurnLoopResult:
        turn.status = "awaiting_interaction"
        return TurnLoopResult(suspended=True, interaction=SimpleNamespace())

    runtime = _runtime(monkeypatch, sessions, _loop)
    monkeypatch.setattr(runtime, "_emit_interaction", _raise(RuntimeError("交互事件发布失败")))

    events, _ = await _drive(runtime, sessions)

    assert _errors(events) == []
    assert (runtime.db.commits, runtime.db.rollbacks) == (1, 1)
    assert events[-1]["status"] == "awaiting_interaction"
