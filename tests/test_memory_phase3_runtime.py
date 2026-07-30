"""Memory Phase 3 Runtime 与 Job Runner 事务边界测试。"""

import asyncio
from types import SimpleNamespace
from uuid import uuid4

import pytest

import services.agent_runtime as agent_runtime_module
import services.memory_job_service as memory_job_module
from core.config import settings
from core.errors import AgentException
from services.agent_runtime import AgentRuntime, TurnLoopResult
from services.memory_extraction_service import MemoryApplyResult
from services.memory_job_service import MemoryJobRunner, MemoryJobService


class _RuntimeNested:
    """模拟 SAVEPOINT：入队失败时只回滚它，不影响外层 Turn 状态提交。"""

    def __init__(self, events: list[str]) -> None:
        self.events = events

    async def commit(self) -> None:
        self.events.append("nested:commit")

    async def rollback(self) -> None:
        self.events.append("nested:rollback")


class _RuntimeDb:
    def __init__(self, events: list[str]) -> None:
        self.events = events
        self.commit_count = 0

    async def commit(self) -> None:
        self.commit_count += 1
        self.events.append("commit")

    async def begin_nested(self) -> _RuntimeNested:
        return _RuntimeNested(self.events)


class _RuntimeSessionService:
    def __init__(self, events: list[str]) -> None:
        self.events = events
        self.repo = self

    async def mark_turn_status(self, turn, status, *, completed_message_id=None) -> None:
        self.events.append(f"turn:{status}")
        turn.status = status
        turn.completed_message_id = completed_message_id

    async def update_status(self, session, status) -> None:
        self.events.append(f"session:{status}")
        session.status = status


def _runtime_with_completion_fakes(events: list[str]) -> AgentRuntime:
    runtime = object.__new__(AgentRuntime)
    runtime.db = _RuntimeDb(events)
    runtime.session_service = _RuntimeSessionService(events)
    return runtime


def test_complete_turn_enqueues_after_terminal_state_and_commits_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[str] = []
    job_id = uuid4()

    class FakeMemoryJobService:
        def __init__(self, db) -> None:
            assert db is runtime.db

        async def enqueue_extract(self, session, turn):
            assert session.status == "idle"
            assert turn.status == "completed"
            assert turn.completed_message_id == 31
            events.append("enqueue")
            return SimpleNamespace(id=job_id)

    runtime = _runtime_with_completion_fakes(events)
    session = SimpleNamespace(id=7, status="running")
    turn = SimpleNamespace(id=uuid4(), status="running", completed_message_id=None)
    monkeypatch.setattr(settings, "memory_extraction_enabled", True)
    monkeypatch.setattr(agent_runtime_module, "MemoryJobService", FakeMemoryJobService)

    result = asyncio.run(runtime._complete_turn(session, turn, 31))

    assert result == job_id
    # 入队包在 SAVEPOINT 内，成功时先提交 SAVEPOINT 再提交外层事务
    assert events == [
        "turn:completed",
        "session:idle",
        "enqueue",
        "nested:commit",
        "commit",
    ]
    assert runtime.db.commit_count == 1


def test_complete_turn_survives_enqueue_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Memory 入队失败时只回滚 SAVEPOINT，Turn 完成与 Session 恢复照常提交。"""
    events: list[str] = []

    class BrokenMemoryJobService:
        def __init__(self, db) -> None:
            return None

        async def enqueue_extract(self, session, turn):
            events.append("enqueue:boom")
            raise RuntimeError("入队失败")

    runtime = _runtime_with_completion_fakes(events)
    session = SimpleNamespace(id=7, status="running")
    turn = SimpleNamespace(id=uuid4(), status="running", completed_message_id=None)
    monkeypatch.setattr(settings, "memory_extraction_enabled", True)
    monkeypatch.setattr(agent_runtime_module, "MemoryJobService", BrokenMemoryJobService)

    result = asyncio.run(runtime._complete_turn(session, turn, 31))

    # 本轮放弃记忆提取，但主链路状态必须已落库
    assert result is None
    assert events == [
        "turn:completed",
        "session:idle",
        "enqueue:boom",
        "nested:rollback",
        "commit",
    ]
    assert runtime.db.commit_count == 1
    assert turn.status == "completed"
    assert session.status == "idle"


def test_complete_turn_does_not_enqueue_when_extraction_disabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[str] = []

    class ForbiddenMemoryJobService:
        def __init__(self, db) -> None:
            raise AssertionError("关闭提取时不应创建MemoryJobService")

    runtime = _runtime_with_completion_fakes(events)
    session = SimpleNamespace(id=7, status="running")
    turn = SimpleNamespace(id=uuid4(), status="running", completed_message_id=None)
    monkeypatch.setattr(settings, "memory_extraction_enabled", False)
    # 显式关闭影子提取，验证的是提取完全关闭时不入队的行为
    monkeypatch.setattr(settings, "memory_extraction_shadow_enabled", False)
    monkeypatch.setattr(agent_runtime_module, "MemoryJobService", ForbiddenMemoryJobService)

    result = asyncio.run(runtime._complete_turn(session, turn, 31))

    assert result is None
    assert events == ["turn:completed", "session:idle", "commit"]
    assert runtime.db.commit_count == 1


def test_memory_job_service_uses_stable_idempotency_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    job_id = uuid4()
    turn_id = uuid4()
    keys: list[str] = []
    service = MemoryJobService(SimpleNamespace())

    class FakeMemoryRepo:
        async def get_or_create_space_for_update(self, workspace_id, user_id):
            assert (workspace_id, user_id) == (8, 9)
            return SimpleNamespace(id=17, catalog_version=4)

    class FakeJobRepo:
        async def enqueue_extract(self, **kwargs):
            keys.append(kwargs["idempotency_key"])
            return SimpleNamespace(id=job_id)

    service.memory_repo = FakeMemoryRepo()
    service.job_repo = FakeJobRepo()
    session = SimpleNamespace(id=7, user_id=9, workspace_id=8)
    turn = SimpleNamespace(
        id=turn_id,
        session_id=7,
        user_id=9,
        workspace_id=8,
        status="completed",
        completed_message_id=31,
    )
    monkeypatch.setattr(settings, "memory_extractor_prompt_version", "test-v2")
    # 显式关闭影子模式，验证的是正常 active 模式下的幂等键格式
    monkeypatch.setattr(settings, "memory_extraction_shadow_enabled", False)

    async def enqueue_twice():
        return (
            await service.enqueue_extract(session, turn),
            await service.enqueue_extract(session, turn),
        )

    first, second = asyncio.run(enqueue_twice())

    expected_key = f"extract:7:{turn_id}:test-v2"
    assert first.id == second.id == job_id
    assert keys == [expected_key, expected_key]


def test_enqueue_extract_raises_agent_exception_for_invalid_turn() -> None:
    """归属或状态非法时抛业务异常而不是裸 RuntimeError，避免打断主对话事务。"""
    service = MemoryJobService(SimpleNamespace())
    session = SimpleNamespace(id=7, user_id=9, workspace_id=8)
    incomplete_turn = SimpleNamespace(
        id=uuid4(),
        session_id=7,
        user_id=9,
        workspace_id=8,
        status="running",
        completed_message_id=None,
    )

    with pytest.raises(AgentException, match="只有归属一致且已完成的交互轮次"):
        asyncio.run(service.enqueue_extract(session, incomplete_turn))


@pytest.mark.parametrize("terminal_path", ["awaiting", "failed"])
def test_non_completed_runtime_paths_do_not_enqueue(
    monkeypatch: pytest.MonkeyPatch,
    terminal_path: str,
) -> None:
    complete_calls: list[str] = []
    abnormal_calls: list[str] = []
    session = SimpleNamespace(id=7, title="test", status="running")
    turn = SimpleNamespace(id=uuid4(), status="running")

    class FakeNested:
        async def commit(self) -> None:
            return None

        async def rollback(self) -> None:
            return None

    class FakeDb:
        async def commit(self) -> None:
            return None

        async def begin_nested(self) -> FakeNested:
            return FakeNested()

    class FakeSessionService:
        async def prepare_for_message(self, *args):
            return session

        async def start_turn(self, *args):
            return turn, SimpleNamespace(id=1)

    class FakeBus:
        async def emit(self, event) -> None:
            return None

        async def close(self) -> None:
            return None

    class FakeTaskService:
        def __init__(self, db, bus) -> None:
            return None

        async def emit_snapshot(self, session_id, reason) -> None:
            return None

    runtime = object.__new__(AgentRuntime)
    runtime.db = FakeDb()
    runtime.user_id = 9
    runtime.workspace_id = 8
    runtime.session_service = FakeSessionService()
    runtime.bus = FakeBus()

    async def apply_hooks(*args):
        return None

    async def run_loop(*args):
        if terminal_path == "awaiting":
            turn.status = "awaiting_approval"
            return TurnLoopResult(suspended=True)
        raise RuntimeError("模拟主循环失败")

    async def complete_turn(*args):
        complete_calls.append("complete")
        return uuid4()

    async def mark_abnormal_end(session_id, turn_id, status):
        abnormal_calls.append(status)
        turn.status = status

    runtime._apply_user_prompt_hooks = apply_hooks
    runtime._run_llm_loop = run_loop
    runtime._complete_turn = complete_turn
    runtime._mark_abnormal_end = mark_abnormal_end
    monkeypatch.setattr(agent_runtime_module, "TaskService", FakeTaskService)

    asyncio.run(runtime._produce(7, "remember this"))

    assert complete_calls == []
    assert turn.status == ("awaiting_approval" if terminal_path == "awaiting" else "failed")
    assert abnormal_calls == ([] if terminal_path == "awaiting" else ["failed"])


def test_inline_memory_job_respects_switch(monkeypatch: pytest.MonkeyPatch) -> None:
    class ForbiddenRunner:
        def __init__(self) -> None:
            raise AssertionError("关闭inline时不应创建Runner")

    runtime = object.__new__(AgentRuntime)
    monkeypatch.setattr(settings, "memory_extraction_inline", False)
    monkeypatch.setattr(agent_runtime_module, "MemoryJobRunner", ForbiddenRunner)

    assert asyncio.run(runtime._run_inline_memory_job(uuid4())) is None


def test_inline_memory_job_failure_is_fail_open(monkeypatch: pytest.MonkeyPatch) -> None:
    job_id = uuid4()
    calls: list[object] = []

    class FailingRunner:
        def __init__(self, *, claim_scope):
            calls.append(claim_scope)

        async def run_once(self, *, job_id):
            calls.append(job_id)
            raise RuntimeError("模拟inline执行失败")

    runtime = object.__new__(AgentRuntime)
    runtime.user_id = 7
    runtime.workspace_id = 9
    monkeypatch.setattr(settings, "memory_extraction_inline", True)
    monkeypatch.setattr(agent_runtime_module, "MemoryJobRunner", FailingRunner)

    assert asyncio.run(runtime._run_inline_memory_job(job_id)) is None
    assert calls == [(9, 7), job_id]


class _SessionState:
    def __init__(self) -> None:
        self.active = 0
        self.events: list[str] = []


class _ManagedSession:
    def __init__(self, state: _SessionState, label: str) -> None:
        self.state = state
        self.label = label

    async def __aenter__(self):
        self.state.active += 1
        self.state.events.append(f"enter:{self.label}")
        return self

    async def __aexit__(self, exc_type, exc, traceback) -> None:
        self.state.active -= 1
        self.state.events.append(f"close:{self.label}")

    async def commit(self) -> None:
        self.state.events.append(f"commit:{self.label}")

    async def rollback(self) -> None:
        self.state.events.append(f"rollback:{self.label}")


class _SessionFactory:
    def __init__(self, state: _SessionState) -> None:
        self.state = state
        self.labels = iter(("claim", "prepare", "apply"))

    def __call__(self) -> _ManagedSession:
        return _ManagedSession(self.state, next(self.labels))


def test_job_runner_closes_transactions_before_llm_and_applies_atomically(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state = _SessionState()
    job_id = uuid4()
    job = SimpleNamespace(id=job_id, job_type="extract", session_id=1, payload={})
    prepared = SimpleNamespace(job_id=job_id, mode="active")
    extraction_result = SimpleNamespace(memories=[])
    apply_result = MemoryApplyResult((), (), (), 0, 4)

    class FakeJobRepository:
        def __init__(self, db) -> None:
            self.db = db

        async def claim_available(self, **kwargs):
            assert self.db.label == "claim"
            state.events.append("claim")
            return [job]

        async def require_owned_lease(self, requested_job_id, worker_id):
            assert self.db.label == "apply"
            assert requested_job_id == job_id
            state.events.append("require_lease")
            return job

        async def mark_succeeded(self, target, worker_id, **kwargs):
            assert target is job
            state.events.append("succeeded")
            return target

    class FakeMemoryJobService:
        def __init__(self, db) -> None:
            assert db.label == "apply"

        async def record_extract_success_and_maybe_enqueue_dream(self, target, **kwargs):
            assert target is job
            state.events.append("dream_gate")

    class FakeExtractionService:
        def __init__(self, db, llm) -> None:
            self.db = db
            self.job_repo = FakeJobRepository(db)

        async def prepare(self, requested_job_id, worker_id):
            assert self.db.label == "prepare"
            assert requested_job_id == job_id
            state.events.append("prepare")
            return prepared

        @staticmethod
        async def extract_with_llm(target, llm):
            assert target is prepared
            assert state.active == 0, "LLM调用期间不能有打开的数据库Session"
            state.events.append("llm")
            return extraction_result

        async def apply(self, requested_job_id, worker_id, result):
            assert self.db.label == "apply"
            assert result is extraction_result
            state.events.append("apply")
            return apply_result

    monkeypatch.setattr(memory_job_module, "MemoryJobRepository", FakeJobRepository)
    monkeypatch.setattr(memory_job_module, "MemoryJobService", FakeMemoryJobService)
    monkeypatch.setattr(memory_job_module, "MemoryExtractionService", FakeExtractionService)
    runner = MemoryJobRunner(
        session_factory=_SessionFactory(state),
        llm=SimpleNamespace(),
        worker_id="test-worker",
    )

    claimed_count = asyncio.run(runner.run_once(job_id=job_id))

    assert claimed_count == 1
    assert state.active == 0
    assert state.events == [
        "enter:claim",
        "claim",
        "commit:claim",
        "close:claim",
        "enter:prepare",
        "prepare",
        "commit:prepare",
        "close:prepare",
        "llm",
        "enter:apply",
        "apply",
        "require_lease",
        "dream_gate",
        "succeeded",
        "commit:apply",
        "close:apply",
    ]
    assert job.payload == {"result": apply_result.to_payload()}
