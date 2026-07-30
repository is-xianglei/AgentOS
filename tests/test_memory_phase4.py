"""Memory Phase 4 Dream 严格操作、门控、原子应用与回滚测试。"""

import asyncio
from copy import deepcopy
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest
from pydantic import ValidationError

from core.config import settings
from core.errors import AgentException
from repositories.memory_repo import MemoryDreamEntry
from services.memory_dream_service import (
    MemoryDreamFailure,
    MemoryDreamResult,
    MemoryDreamService,
    PreparedDream,
)
from services.memory_job_service import MemoryJobService
from services.memory_service import MEMORY_BODY_MAX_BYTES


def _target_payload(**overrides: object) -> dict[str, object]:
    payload: dict[str, object] = {
        "memory_key": "user-preference-tabs",
        "type": "user",
        "name": "缩进偏好",
        "description": "用户偏好使用制表符缩进",
        "body": "用户编写代码时偏好使用制表符，而不是空格。",
    }
    payload.update(overrides)
    return payload


def _update_payload(**overrides: object) -> dict[str, object]:
    payload: dict[str, object] = {
        "action": "update",
        "item_id": 1,
        "expected_version": 1,
        "target": _target_payload(),
    }
    payload.update(overrides)
    return payload


@pytest.mark.parametrize(
    "operation",
    [
        {**_update_payload(), "unexpected": True},
        _update_payload(item_id="1"),
        {
            "action": "merge",
            "source_ids": [1, 1],
            "target": _target_payload(),
        },
        {"action": "supersede", "item_id": 1, "by_item_id": 1},
        _update_payload(target=_target_payload(memory_key="Invalid Key")),
        _update_payload(target=_target_payload(body="记" * (MEMORY_BODY_MAX_BYTES // 3 + 1))),
    ],
)
def test_dream_schema_rejects_non_strict_operations(operation: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        MemoryDreamResult.model_validate({"operations": [operation]})


def test_dream_schema_rejects_extra_top_level_field() -> None:
    with pytest.raises(ValidationError):
        MemoryDreamResult.model_validate({"operations": [_update_payload()], "unexpected": True})


def test_dream_schema_counts_body_by_utf8_bytes() -> None:
    body_at_limit = "记" * (MEMORY_BODY_MAX_BYTES // 3) + "a"
    operation = deepcopy(_update_payload())
    operation["target"]["body"] = body_at_limit

    result = MemoryDreamResult.model_validate({"operations": [operation]})

    assert result.operations[0].target.body == body_at_limit
    assert len(body_at_limit.encode("utf-8")) == MEMORY_BODY_MAX_BYTES


class _DreamGateMemoryRepo:
    def __init__(self, space: SimpleNamespace, active_count: int) -> None:
        self.space = space
        self.active_count = active_count
        self.increment_calls = 0
        self.scan_calls: list[datetime] = []

    async def get_space_for_worker(self, space_id: int, *, for_update: bool = False):
        assert space_id == self.space.id
        assert for_update is True
        return self.space

    async def increment_sessions_since_dream(
        self,
        workspace_id: int,
        user_id: int,
        space_id: int,
    ) -> int:
        assert (workspace_id, user_id, space_id) == (
            self.space.workspace_id,
            self.space.user_id,
            self.space.id,
        )
        self.increment_calls += 1
        self.space.sessions_since_dream += 1
        return self.space.sessions_since_dream

    async def count_active_items(
        self,
        workspace_id: int,
        user_id: int,
        space_id: int,
    ) -> int:
        assert (workspace_id, user_id, space_id) == (
            self.space.workspace_id,
            self.space.user_id,
            self.space.id,
        )
        return self.active_count

    async def mark_dream_scan(
        self,
        workspace_id: int,
        user_id: int,
        space: SimpleNamespace,
        *,
        scanned_at: datetime,
    ) -> None:
        assert (workspace_id, user_id, space.id) == (
            self.space.workspace_id,
            self.space.user_id,
            self.space.id,
        )
        self.scan_calls.append(scanned_at)
        space.last_scan_at = scanned_at


class _DreamGateJobRepo:
    def __init__(self, *, already_counted: bool = False, has_live_dream: bool = False) -> None:
        self.already_counted = already_counted
        self.live_dream = has_live_dream
        self.enqueue_kwargs: dict[str, object] | None = None

    async def has_succeeded_extract_for_session_since(
        self,
        space_id: int,
        session_id: int,
        since: datetime,
    ) -> bool:
        assert space_id == 41
        assert session_id == 61
        assert since.tzinfo is not None
        return self.already_counted

    async def has_live_dream(self, space_id: int) -> bool:
        assert space_id == 41
        return self.live_dream

    async def enqueue_dream(self, **kwargs: object):
        self.enqueue_kwargs = kwargs
        return SimpleNamespace(id="dream-job")


def _run_dream_gate(
    monkeypatch: pytest.MonkeyPatch,
    *,
    active_count: int = 10,
    sessions_since_dream: int = 5,
    last_dream_at: datetime | None = None,
    last_scan_at: datetime | None = None,
    already_counted: bool = True,
    has_live_dream: bool = False,
) -> tuple[object | None, _DreamGateMemoryRepo, _DreamGateJobRepo, datetime]:
    completed_at = datetime(2026, 7, 25, 12, 0, tzinfo=UTC)
    monkeypatch.setattr(settings, "memory_dream_enabled", True)
    monkeypatch.setattr(settings, "memory_dream_rollout_percent", 100)
    monkeypatch.setattr(settings, "memory_dream_min_items", 10)
    monkeypatch.setattr(settings, "memory_dream_min_sessions", 5)
    monkeypatch.setattr(settings, "memory_dream_interval_seconds", 24 * 60 * 60)
    monkeypatch.setattr(settings, "memory_dream_scan_interval_seconds", 60 * 60)
    monkeypatch.setattr(settings, "memory_dream_prompt_version", "dream-v7")
    monkeypatch.setattr(settings, "memory_dream_model", "dream-model")
    space = SimpleNamespace(
        id=41,
        workspace_id=51,
        user_id=71,
        catalog_version=13,
        sessions_since_dream=sessions_since_dream,
        last_dream_at=last_dream_at,
        last_scan_at=last_scan_at,
    )
    memory_repo = _DreamGateMemoryRepo(space, active_count)
    job_repo = _DreamGateJobRepo(
        already_counted=already_counted,
        has_live_dream=has_live_dream,
    )
    service = MemoryJobService(SimpleNamespace())
    service.memory_repo = memory_repo
    service.job_repo = job_repo
    job = SimpleNamespace(job_type="extract", session_id=61, space_id=41)

    result = asyncio.run(
        service.record_extract_success_and_maybe_enqueue_dream(
            job,
            completed_at=completed_at,
        )
    )
    return result, memory_repo, job_repo, completed_at


def test_same_session_is_counted_only_once(monkeypatch: pytest.MonkeyPatch) -> None:
    result, memory_repo, job_repo, _ = _run_dream_gate(
        monkeypatch,
        active_count=9,
        already_counted=True,
    )

    assert result is None
    assert memory_repo.increment_calls == 0
    assert job_repo.enqueue_kwargs is None


@pytest.mark.parametrize(
    ("overrides", "should_enqueue"),
    [
        ({"active_count": 9}, False),
        ({"active_count": 10}, True),
        ({"sessions_since_dream": 4}, False),
        ({"sessions_since_dream": 5}, True),
        ({"last_dream_hours": 23}, False),
        ({"last_dream_hours": 24}, True),
        ({"last_scan_seconds": 3599}, False),
        ({"last_scan_seconds": 3600}, True),
        ({"has_live_dream": True}, False),
    ],
)
def test_dream_gate_boundaries(
    monkeypatch: pytest.MonkeyPatch,
    overrides: dict[str, object],
    should_enqueue: bool,
) -> None:
    completed_at = datetime(2026, 7, 25, 12, 0, tzinfo=UTC)
    kwargs = dict(overrides)
    last_dream_hours = kwargs.pop("last_dream_hours", None)
    last_scan_seconds = kwargs.pop("last_scan_seconds", None)
    if last_dream_hours is not None:
        kwargs["last_dream_at"] = completed_at - timedelta(hours=last_dream_hours)
    if last_scan_seconds is not None:
        kwargs["last_scan_at"] = completed_at - timedelta(seconds=last_scan_seconds)

    result, _, job_repo, _ = _run_dream_gate(monkeypatch, **kwargs)

    assert (result is not None) is should_enqueue
    assert (job_repo.enqueue_kwargs is not None) is should_enqueue


def test_fifth_new_session_can_open_gate(monkeypatch: pytest.MonkeyPatch) -> None:
    result, memory_repo, _, _ = _run_dream_gate(
        monkeypatch,
        sessions_since_dream=4,
        already_counted=False,
    )

    assert result is not None
    assert memory_repo.increment_calls == 1
    assert memory_repo.space.sessions_since_dream == 5


def test_dream_idempotency_key_contains_snapshot_dimensions(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    result, _, job_repo, completed_at = _run_dream_gate(monkeypatch)
    expected_window = int(completed_at.timestamp()) // 3600

    assert result is not None
    assert job_repo.enqueue_kwargs == {
        "space_id": 41,
        "idempotency_key": f"dream:41:13:dream-v7:{expected_window}",
        "max_attempts": settings.memory_job_max_attempts,
        "model": "dream-model",
        "prompt_version": "dream-v7",
        "base_catalog_version": 13,
        "payload": {},
    }


def _entry(
    item_id: int,
    memory_type: str,
    memory_key: str,
) -> MemoryDreamEntry:
    return MemoryDreamEntry(
        item_id=item_id,
        revision_id=100 + item_id,
        version=1,
        memory_key=memory_key,
        memory_type=memory_type,
        name=f"原始标题{item_id}",
        description=f"原始摘要{item_id}",
        body=f"原始正文{item_id}",
        source_kind="extracted",
        last_used_at=None,
        use_count=0,
    )


class _DreamApplyMemoryRepo:
    def __init__(self, space: SimpleNamespace, entries: tuple[MemoryDreamEntry, ...]) -> None:
        self.space = space
        self.entries = entries
        self.items = {
            entry.item_id: SimpleNamespace(
                id=entry.item_id,
                space_id=space.id,
                memory_key=entry.memory_key,
                memory_type=entry.memory_type,
                name=entry.name,
                description=entry.description,
                body=entry.body,
                status="active",
                version=entry.version,
                superseded_by_id=None,
            )
            for entry in entries
        }
        self.revisions = {
            entry.revision_id: SimpleNamespace(
                id=entry.revision_id,
                memory_id=entry.item_id,
                revision=entry.version,
                memory_type=entry.memory_type,
                name=entry.name,
                description=entry.description,
                body=entry.body,
            )
            for entry in entries
        }
        self.current_revision_ids = {entry.item_id: entry.revision_id for entry in entries}
        self.next_revision_id = 1000
        self.finalize_calls = 0
        self.catalog_increment_calls = 0
        self.source_calls = 0

    async def get_space_for_worker(self, space_id: int, *, for_update: bool = False):
        assert space_id == self.space.id
        assert for_update is True
        return self.space

    async def get_space(
        self,
        workspace_id: int,
        user_id: int,
        *,
        for_update: bool = False,
    ):
        assert (workspace_id, user_id) == (self.space.workspace_id, self.space.user_id)
        assert for_update is True
        return self.space

    async def list_active_dream_entries(
        self,
        workspace_id: int,
        user_id: int,
        space_id: int,
    ) -> list[MemoryDreamEntry]:
        assert (workspace_id, user_id, space_id) == (
            self.space.workspace_id,
            self.space.user_id,
            self.space.id,
        )
        return list(self.entries)

    async def get_active_items_by_ids_for_update(
        self,
        workspace_id: int,
        user_id: int,
        space_id: int,
        item_ids: list[int],
    ) -> list[SimpleNamespace]:
        assert (workspace_id, user_id, space_id) == (
            self.space.workspace_id,
            self.space.user_id,
            self.space.id,
        )
        return [
            self.items[item_id]
            for item_id in item_ids
            if item_id in self.items and self.items[item_id].status == "active"
        ]

    async def update_item(
        self,
        workspace_id: int,
        user_id: int,
        space: SimpleNamespace,
        item: SimpleNamespace,
        *,
        memory_type: str,
        name: str,
        description: str,
        body: str,
    ) -> SimpleNamespace:
        item.memory_type = memory_type
        item.name = name
        item.description = description
        item.body = body
        item.version += 1
        return item

    async def update_item_state(
        self,
        workspace_id: int,
        user_id: int,
        space: SimpleNamespace,
        item: SimpleNamespace,
        *,
        status: str,
        superseded_by_id: int | None,
    ) -> SimpleNamespace:
        item.status = status
        item.superseded_by_id = superseded_by_id
        item.version += 1
        return item

    async def create_revision(
        self,
        workspace_id: int,
        user_id: int,
        space: SimpleNamespace,
        item: SimpleNamespace,
        *,
        actor_type: str,
        actor_id: str | None,
        run_id: UUID | None = None,
    ) -> SimpleNamespace:
        assert actor_type == "dream"
        assert run_id is not None
        revision = SimpleNamespace(
            id=self.next_revision_id,
            memory_id=item.id,
            revision=item.version,
            memory_type=item.memory_type,
            name=item.name,
            description=item.description,
            body=item.body,
        )
        self.next_revision_id += 1
        self.revisions[revision.id] = revision
        self.current_revision_ids[item.id] = revision.id
        return revision

    async def create_source(
        self,
        workspace_id: int,
        user_id: int,
        *,
        space: SimpleNamespace,
        item: SimpleNamespace,
        revision: SimpleNamespace,
        source_kind: str,
    ) -> SimpleNamespace:
        assert source_kind == "dream"
        assert revision.memory_id == item.id
        self.source_calls += 1
        return SimpleNamespace(id=self.source_calls)

    async def finalize_dream(
        self,
        workspace_id: int,
        user_id: int,
        space: SimpleNamespace,
        *,
        completed_at: datetime,
        catalog_changed: bool,
    ) -> int:
        self.finalize_calls += 1
        space.last_dream_at = completed_at
        space.sessions_since_dream = 0
        if catalog_changed:
            space.catalog_version += 1
        return space.catalog_version

    async def get_item(
        self,
        workspace_id: int,
        user_id: int,
        item_id: int,
        *,
        for_update: bool = False,
    ) -> SimpleNamespace | None:
        assert for_update is True
        return self.items.get(item_id)

    async def get_revision_by_id_for_scope(
        self,
        workspace_id: int,
        user_id: int,
        space_id: int,
        revision_id: int,
    ) -> SimpleNamespace | None:
        return self.revisions.get(revision_id)

    async def get_current_revision(
        self,
        workspace_id: int,
        user_id: int,
        item: SimpleNamespace,
    ) -> SimpleNamespace | None:
        revision_id = self.current_revision_ids.get(item.id)
        return self.revisions.get(revision_id)

    async def restore_item_from_revision(
        self,
        workspace_id: int,
        user_id: int,
        space: SimpleNamespace,
        item: SimpleNamespace,
        revision: SimpleNamespace,
    ) -> SimpleNamespace:
        item.memory_type = revision.memory_type
        item.name = revision.name
        item.description = revision.description
        item.body = revision.body
        item.status = "active"
        item.superseded_by_id = None
        item.version += 1
        return item

    async def increment_catalog_version(
        self,
        workspace_id: int,
        user_id: int,
        space: SimpleNamespace,
    ) -> int:
        self.catalog_increment_calls += 1
        space.catalog_version += 1
        return space.catalog_version


class _DreamApplyJobRepo:
    def __init__(self, job: SimpleNamespace) -> None:
        self.job = job
        self.merge_payload_calls = 0

    async def require_owned_lease(self, job_id: UUID, worker_id: str):
        assert job_id == self.job.id
        assert worker_id == "dream-worker"
        return self.job

    async def get_by_id_for_scope(
        self,
        workspace_id: int,
        user_id: int,
        job_id: UUID,
        *,
        for_update: bool = False,
    ):
        assert (workspace_id, user_id, job_id) == (51, 71, self.job.id)
        assert for_update is True
        return self.job

    async def merge_payload(self, job: SimpleNamespace, values: dict[str, object]):
        self.merge_payload_calls += 1
        job.payload = {**(job.payload or {}), **values}
        return job


def _dream_apply_fixture():
    entries = (
        _entry(1, "user", "user-preference-tabs"),
        _entry(2, "feedback", "duplicate-tabs"),
        _entry(3, "project", "project-format"),
        _entry(4, "reference", "obsolete-reference"),
        _entry(5, "reference", "current-reference"),
        _entry(6, "reference", "unused-reference"),
    )
    space = SimpleNamespace(
        id=41,
        workspace_id=51,
        user_id=71,
        catalog_version=13,
        sessions_since_dream=7,
        last_dream_at=None,
    )
    job_id = uuid4()
    job = SimpleNamespace(
        id=job_id,
        job_type="dream",
        space_id=space.id,
        status="running",
        base_catalog_version=13,
        payload={"revision_ids": [entry.revision_id for entry in entries]},
    )
    prepared = PreparedDream(
        job_id=job_id,
        space_id=space.id,
        user_id=space.user_id,
        workspace_id=space.workspace_id,
        model="dream-model",
        prompt_version="v1",
        base_catalog_version=13,
        entries=entries,
    )
    memory_repo = _DreamApplyMemoryRepo(space, entries)
    job_repo = _DreamApplyJobRepo(job)
    service = MemoryDreamService(SimpleNamespace(), SimpleNamespace())
    service.memory_repo = memory_repo
    service.job_repo = job_repo
    return service, memory_repo, job_repo, job, prepared


def _complete_operation_set() -> MemoryDreamResult:
    return MemoryDreamResult.model_validate(
        {
            "operations": [
                {
                    "action": "merge",
                    "source_ids": [1, 2],
                    "target": _target_payload(
                        body="合并后的用户偏好",
                        description="用户稳定偏好使用制表符",
                    ),
                },
                {
                    "action": "update",
                    "item_id": 3,
                    "expected_version": 1,
                    "target": _target_payload(
                        memory_key="project-format",
                        type="project",
                        name="项目格式",
                        description="项目采用统一格式",
                        body="项目统一使用 Ruff 格式化。",
                    ),
                },
                {"action": "supersede", "item_id": 4, "by_item_id": 5},
                {"action": "archive", "item_id": 6},
            ]
        }
    )


def _item_state(memory_repo: _DreamApplyMemoryRepo) -> dict[int, dict[str, object]]:
    return {item_id: deepcopy(vars(item)) for item_id, item in memory_repo.items.items()}


def test_bad_operation_set_has_zero_side_effects_and_protects_user_memory() -> None:
    service, memory_repo, job_repo, job, prepared = _dream_apply_fixture()
    result = MemoryDreamResult.model_validate(
        {
            "operations": [
                {
                    "action": "update",
                    "item_id": 3,
                    "expected_version": 1,
                    "target": _target_payload(
                        memory_key="project-format",
                        type="project",
                    ),
                },
                {"action": "archive", "item_id": 1},
            ]
        }
    )
    before = _item_state(memory_repo)

    with pytest.raises(MemoryDreamFailure) as exc_info:
        asyncio.run(service.apply(job.id, "dream-worker", prepared, result))

    assert exc_info.value.code == "user_memory_protected"
    assert exc_info.value.retryable is False
    assert _item_state(memory_repo) == before
    assert len(memory_repo.revisions) == len(prepared.entries)
    assert memory_repo.source_calls == 0
    assert memory_repo.finalize_calls == 0
    assert job_repo.merge_payload_calls == 0
    assert job.payload == {"revision_ids": list(prepared.revision_ids)}


def test_dream_applies_all_operations_and_increments_catalog_once() -> None:
    service, memory_repo, job_repo, job, prepared = _dream_apply_fixture()

    applied = asyncio.run(
        service.apply(job.id, "dream-worker", prepared, _complete_operation_set())
    )

    assert memory_repo.items[1].body == "合并后的用户偏好"
    assert memory_repo.items[1].status == "active"
    assert memory_repo.items[2].status == "superseded"
    assert memory_repo.items[2].superseded_by_id == 1
    assert memory_repo.items[3].body == "项目统一使用 Ruff 格式化。"
    assert memory_repo.items[4].status == "superseded"
    assert memory_repo.items[4].superseded_by_id == 5
    assert memory_repo.items[5].status == "active"
    assert memory_repo.items[5].version == 1
    assert memory_repo.items[6].status == "archived"
    assert applied.changed_item_ids == (1, 2, 3, 4, 6)
    assert applied.operation_count == 4
    assert applied.base_catalog_version == 13
    assert applied.catalog_version == 14
    assert memory_repo.space.catalog_version == 14
    assert memory_repo.finalize_calls == 1
    assert memory_repo.source_calls == 5
    assert job_repo.merge_payload_calls == 1
    assert "dream_audit" in job.payload
    assert "body" not in str(job.payload["dream_audit"])


def test_dream_replay_is_rejected_without_additional_side_effects() -> None:
    service, memory_repo, job_repo, job, prepared = _dream_apply_fixture()
    result = _complete_operation_set()
    asyncio.run(service.apply(job.id, "dream-worker", prepared, result))
    state_after_first_apply = _item_state(memory_repo)
    payload_after_first_apply = deepcopy(job.payload)
    revision_count = len(memory_repo.revisions)

    with pytest.raises(MemoryDreamFailure) as exc_info:
        asyncio.run(service.apply(job.id, "dream-worker", prepared, result))

    assert exc_info.value.code == "catalog_changed"
    assert exc_info.value.retryable is True
    assert _item_state(memory_repo) == state_after_first_apply
    assert job.payload == payload_after_first_apply
    assert len(memory_repo.revisions) == revision_count
    assert memory_repo.finalize_calls == 1
    assert job_repo.merge_payload_calls == 1


def test_dream_rollback_restores_all_changed_items() -> None:
    service, memory_repo, _, job, prepared = _dream_apply_fixture()
    original_state = _item_state(memory_repo)
    asyncio.run(service.apply(job.id, "dream-worker", prepared, _complete_operation_set()))
    job.status = "succeeded"

    rolled_back = asyncio.run(
        service.rollback_dream(
            51,
            71,
            job.id,
            expected_after_catalog_version=14,
            actor_id="operator-1",
        )
    )

    assert rolled_back.restored_item_ids == (1, 2, 3, 4, 6)
    assert rolled_back.rolled_back_catalog_version == 14
    assert rolled_back.catalog_version == 15
    for item_id in rolled_back.restored_item_ids:
        item = memory_repo.items[item_id]
        original = original_state[item_id]
        assert item.memory_type == original["memory_type"]
        assert item.name == original["name"]
        assert item.description == original["description"]
        assert item.body == original["body"]
        assert item.status == "active"
        assert item.superseded_by_id is None
        assert item.version == 3
    assert memory_repo.items[5].version == 1
    assert memory_repo.space.catalog_version == 15
    assert memory_repo.catalog_increment_calls == 1
    assert memory_repo.source_calls == 10
    assert job.payload["dream_rollback"]["catalog_version"] == 15


def test_dream_rollback_rejects_concurrent_catalog_change() -> None:
    service, memory_repo, job_repo, job, prepared = _dream_apply_fixture()
    asyncio.run(service.apply(job.id, "dream-worker", prepared, _complete_operation_set()))
    job.status = "succeeded"
    memory_repo.space.catalog_version = 15
    state_before_rollback = _item_state(memory_repo)
    revision_count = len(memory_repo.revisions)
    payload_before_rollback = deepcopy(job.payload)

    with pytest.raises(MemoryDreamFailure) as exc_info:
        asyncio.run(
            service.rollback_dream(
                51,
                71,
                job.id,
                expected_after_catalog_version=14,
            )
        )

    assert exc_info.value.code == "catalog_changed"
    assert _item_state(memory_repo) == state_before_rollback
    assert len(memory_repo.revisions) == revision_count
    assert memory_repo.catalog_increment_calls == 0
    assert job.payload == payload_before_rollback
    assert job_repo.merge_payload_calls == 1


class _ScopedDreamJobRepo(_DreamApplyJobRepo):
    """严格按租户作用域返回 Job 的 Fake，用于跨 workspace 越权测试。"""

    async def get_by_id_for_scope(
        self,
        workspace_id: int,
        user_id: int,
        job_id: UUID,
        *,
        for_update: bool = False,
    ):
        assert for_update is True
        if (workspace_id, user_id) != (51, 71) or job_id != self.job.id:
            return None
        return self.job


def _rolled_back_dream_fixture():
    """完成一次 Dream Apply 并把 Job 置为可回滚状态。"""
    service, memory_repo, job_repo, job, prepared = _dream_apply_fixture()
    asyncio.run(service.apply(job.id, "dream-worker", prepared, _complete_operation_set()))
    job.status = "succeeded"
    return service, memory_repo, job_repo, job


def test_rollback_dream_for_api_restores_items_in_scope() -> None:
    service, memory_repo, _, job = _rolled_back_dream_fixture()

    result = asyncio.run(
        service.rollback_dream_for_api(
            51,
            71,
            job.id,
            expected_after_catalog_version=14,
            actor_id="user:71",
        )
    )

    assert result.restored_item_ids == (1, 2, 3, 4, 6)
    assert result.rolled_back_catalog_version == 14
    assert result.catalog_version == 15
    assert memory_repo.space.catalog_version == 15
    assert job.payload["dream_rollback"]["catalog_version"] == 15


def test_rollback_dream_for_api_rejects_cross_workspace_access() -> None:
    service, memory_repo, _, job = _rolled_back_dream_fixture()
    service.job_repo = _ScopedDreamJobRepo(job)
    state_before = _item_state(memory_repo)

    with pytest.raises(AgentException) as exc_info:
        asyncio.run(
            service.rollback_dream_for_api(
                52,
                71,
                job.id,
                expected_after_catalog_version=14,
            )
        )

    assert exc_info.value.status_code == 409
    assert _item_state(memory_repo) == state_before
    assert memory_repo.catalog_increment_calls == 0
    assert "dream_rollback" not in job.payload


def test_rollback_dream_for_api_rejects_foreign_space_job() -> None:
    service, memory_repo, _, job = _rolled_back_dream_fixture()
    job.space_id = 999
    state_before = _item_state(memory_repo)

    with pytest.raises(AgentException) as exc_info:
        asyncio.run(
            service.rollback_dream_for_api(
                51,
                71,
                job.id,
                expected_after_catalog_version=14,
            )
        )

    assert exc_info.value.status_code == 403
    assert exc_info.value.message == "无权限回滚该 Dream"
    assert _item_state(memory_repo) == state_before
    assert memory_repo.catalog_increment_calls == 0


def test_rollback_dream_for_api_returns_409_on_catalog_conflict() -> None:
    service, memory_repo, _, job = _rolled_back_dream_fixture()
    memory_repo.space.catalog_version = 15
    state_before = _item_state(memory_repo)

    with pytest.raises(AgentException) as exc_info:
        asyncio.run(
            service.rollback_dream_for_api(
                51,
                71,
                job.id,
                expected_after_catalog_version=14,
            )
        )

    assert exc_info.value.status_code == 409
    assert exc_info.value.message == "Dream回滚版本冲突，请刷新后重试"
    assert _item_state(memory_repo) == state_before
    assert memory_repo.catalog_increment_calls == 0


def test_rollback_dream_for_api_returns_409_on_stale_expected_version() -> None:
    service, memory_repo, _, job = _rolled_back_dream_fixture()

    with pytest.raises(AgentException) as exc_info:
        asyncio.run(
            service.rollback_dream_for_api(
                51,
                71,
                job.id,
                expected_after_catalog_version=13,
            )
        )

    assert exc_info.value.status_code == 409
    assert memory_repo.catalog_increment_calls == 0


def test_rollback_dream_for_api_rejects_second_rollback() -> None:
    service, memory_repo, _, job = _rolled_back_dream_fixture()
    asyncio.run(
        service.rollback_dream_for_api(
            51,
            71,
            job.id,
            expected_after_catalog_version=14,
        )
    )
    state_before = _item_state(memory_repo)

    with pytest.raises(AgentException) as exc_info:
        asyncio.run(
            service.rollback_dream_for_api(
                51,
                71,
                job.id,
                expected_after_catalog_version=15,
            )
        )

    assert exc_info.value.status_code == 409
    assert _item_state(memory_repo) == state_before
    assert memory_repo.catalog_increment_calls == 1
