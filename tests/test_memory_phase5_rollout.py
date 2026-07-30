import asyncio
from datetime import UTC, datetime
from types import SimpleNamespace
from uuid import uuid4

import pytest

import services.agent_runtime as agent_runtime_module
import services.memory_job_service as memory_job_module
from core.config import settings
from services.agent_runtime import AgentRuntime
from services.memory_job_service import MemoryJobRunner, MemoryJobService
from services.memory_recall_service import MemoryRecallService
from services.memory_rollout_service import MemoryRolloutService


def test_rollout_bucket_is_stable_and_separates_feature_and_salt() -> None:
    first = MemoryRolloutService.bucket(42, "recall", salt="salt-a")

    assert first == MemoryRolloutService.bucket(42, "recall", salt="salt-a")
    assert 0 <= first < 100
    assert first != MemoryRolloutService.bucket(42, "dream", salt="salt-a")
    assert first != MemoryRolloutService.bucket(42, "recall", salt="salt-b")


def test_rollout_percentage_boundaries() -> None:
    assert MemoryRolloutService.is_enabled(7, "recall", 0) is False
    assert MemoryRolloutService.is_enabled(7, "recall", 100) is True
    with pytest.raises(ValueError, match="0 到 100"):
        MemoryRolloutService.is_enabled(7, "recall", 101)


def test_recall_rollout_skips_new_context_but_keeps_frozen_context(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "memory_recall_rollout_percent", 0)
    service = MemoryRecallService(SimpleNamespace())
    frozen = SimpleNamespace(selected_revision_ids=[11])
    contexts: list[object | None] = [None, frozen]

    class FakeRepo:
        async def get_context(self, *args):
            return contexts.pop(0)

    class ForbiddenMemoryService:
        async def get_rendered_catalog(self, *args):
            raise AssertionError("未命中Recall灰度时不应读取Catalog")

    service.repo = FakeRepo()
    service.memory_service = ForbiddenMemoryService()
    turn = SimpleNamespace(workspace_id=1, user_id=2, id=uuid4())

    assert asyncio.run(service.get_or_create_context(turn)) is None
    assert asyncio.run(service.get_or_create_context(turn)) is frozen


def test_dream_rollout_skips_gate_before_catalog_scan(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "memory_dream_enabled", True)
    monkeypatch.setattr(settings, "memory_dream_rollout_percent", 0)
    service = MemoryJobService(SimpleNamespace())
    space = SimpleNamespace(id=5, workspace_id=8, user_id=9, last_dream_at=None)

    class FakeMemoryRepo:
        async def get_space_for_worker(self, space_id, for_update):
            return space

        async def count_active_items(self, *args):
            raise AssertionError("未命中Dream灰度时不应扫描Catalog")

    class FakeJobRepo:
        async def has_succeeded_extract_for_session_since(self, *args):
            return True

        async def enqueue_dream(self, **kwargs):
            raise AssertionError("未命中Dream灰度时不应入队")

    service.memory_repo = FakeMemoryRepo()
    service.job_repo = FakeJobRepo()
    job = SimpleNamespace(job_type="extract", session_id=3, space_id=5)

    result = asyncio.run(
        service.record_extract_success_and_maybe_enqueue_dream(
            job,
            completed_at=datetime.now(UTC),
        )
    )

    assert result is None


def test_shadow_mode_is_frozen_in_extract_job(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "memory_extraction_shadow_enabled", True)
    monkeypatch.setattr(settings, "memory_extractor_prompt_version", "shadow-v1")
    service = MemoryJobService(SimpleNamespace())
    captured: dict[str, object] = {}
    turn_id = uuid4()

    class FakeMemoryRepo:
        async def get_or_create_space_for_update(self, workspace_id, user_id):
            return SimpleNamespace(id=5, catalog_version=7)

    class FakeJobRepo:
        async def enqueue_extract(self, **kwargs):
            captured.update(kwargs)
            return SimpleNamespace(id=uuid4())

    service.memory_repo = FakeMemoryRepo()
    service.job_repo = FakeJobRepo()
    session = SimpleNamespace(id=2, user_id=9, workspace_id=8)
    turn = SimpleNamespace(
        id=turn_id,
        session_id=2,
        user_id=9,
        workspace_id=8,
        status="completed",
        completed_message_id=12,
    )

    asyncio.run(service.enqueue_extract(session, turn))

    assert captured["payload"] == {"mode": "shadow"}
    assert captured["idempotency_key"] == f"extract:2:{turn_id}:shadow-v1:shadow"


def test_runtime_enqueues_shadow_extract_when_shadow_mode_is_enabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    job_id = uuid4()
    events: list[str] = []

    class FakeNested:
        async def commit(self) -> None:
            return None

        async def rollback(self) -> None:
            return None

    class FakeDb:
        async def commit(self) -> None:
            events.append("commit")

        async def begin_nested(self) -> FakeNested:
            return FakeNested()

    class FakeSessionService:
        repo: object

        def __init__(self) -> None:
            self.repo = self

        async def mark_turn_status(self, turn, status, *, completed_message_id=None) -> None:
            turn.status = status
            turn.completed_message_id = completed_message_id
            events.append(f"turn:{status}")

        async def update_status(self, session, status) -> None:
            session.status = status
            events.append(f"session:{status}")

    class FakeMemoryJobService:
        def __init__(self, db) -> None:
            assert db is runtime.db

        async def enqueue_extract(self, session, turn):
            assert session.status == "idle"
            assert turn.status == "completed"
            events.append("enqueue:shadow")
            return SimpleNamespace(id=job_id)

    runtime = object.__new__(AgentRuntime)
    runtime.db = FakeDb()
    runtime.session_service = FakeSessionService()
    session = SimpleNamespace(id=2, status="running")
    turn = SimpleNamespace(id=uuid4(), status="running", completed_message_id=None)
    monkeypatch.setattr(settings, "memory_extraction_enabled", True)
    monkeypatch.setattr(settings, "memory_extraction_shadow_enabled", True)
    monkeypatch.setattr(agent_runtime_module, "MemoryJobService", FakeMemoryJobService)

    result = asyncio.run(runtime._complete_turn(session, turn, 12))

    assert result == job_id
    assert events == ["turn:completed", "session:idle", "enqueue:shadow", "commit"]


class _ManagedSession:
    def __init__(self) -> None:
        self.committed = False

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, traceback) -> None:
        return None

    async def commit(self) -> None:
        self.committed = True

    async def rollback(self) -> None:
        raise AssertionError("影子成功路径不应回滚")


def test_shadow_apply_only_persists_count_summary(monkeypatch: pytest.MonkeyPatch) -> None:
    job_id = uuid4()
    job = SimpleNamespace(
        id=job_id,
        job_type="extract",
        payload={"mode": "shadow"},
        base_catalog_version=7,
    )
    managed = _ManagedSession()
    events: list[str] = []

    class FakeJobRepo:
        async def require_owned_lease(self, requested_job_id, worker_id):
            events.append("lease")
            return job

        async def mark_succeeded(self, target, worker_id):
            events.append("succeeded")

    class FakeExtractionService:
        def __init__(self, db, llm) -> None:
            self.job_repo = FakeJobRepo()

        async def apply(self, *args):
            raise AssertionError("影子提取不得调用Memory Apply")

    class ForbiddenJobService:
        def __init__(self, db) -> None:
            raise AssertionError("影子提取不得推进Dream门控")

    monkeypatch.setattr(memory_job_module, "MemoryExtractionService", FakeExtractionService)
    monkeypatch.setattr(memory_job_module, "MemoryJobService", ForbiddenJobService)
    runner = MemoryJobRunner(
        session_factory=lambda: managed,
        llm=SimpleNamespace(),
        worker_id="shadow-worker",
    )
    result = SimpleNamespace(memories=[SimpleNamespace(body="不得落库的正文")])

    applied = asyncio.run(runner._apply(job_id, result, mode="shadow"))

    assert applied.mode == "shadow"
    assert events == ["lease", "succeeded"]
    assert managed.committed is True
    assert job.payload == {
        "mode": "shadow",
        "result": {"candidate_count": 1, "catalog_version": 7},
    }
    assert "不得落库的正文" not in str(job.payload)
