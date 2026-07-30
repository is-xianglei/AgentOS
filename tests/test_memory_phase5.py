"""Memory Phase 5 数据生命周期的服务层回归测试。"""

import asyncio
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from core.config import settings
from core.errors import AgentException
from memory.worker import MemoryWorker
from repositories.memory_repo import MemoryExportBundle, MemoryPhysicalDeleteCounts
from services.memory_job_service import MemoryJobRunner
from services.memory_retention_service import MemoryRetentionResult, MemoryRetentionService
from services.memory_service import MemoryExportResult, MemoryExportRunner, MemoryService


class _LifecycleRepository:
    def __init__(self) -> None:
        self.space = SimpleNamespace(
            id=7,
            workspace_id=11,
            user_id=13,
            catalog_version=17,
            is_deleted=False,
        )
        self.purge_calls = 0

    async def get_space(self, workspace_id, user_id, *, for_update=False):
        if (workspace_id, user_id) != (11, 13):
            return None
        return self.space

    async def get_export_bundle(self, workspace_id, user_id, *, space):
        assert (workspace_id, user_id) == (11, 13)
        assert space is self.space
        return MemoryExportBundle(self.space, (), (), (), (), ())

    async def get_catalog_version(self, workspace_id, user_id, space_id):
        assert (workspace_id, user_id, space_id) == (11, 13, 7)
        return self.space.catalog_version

    async def purge_space(self, workspace_id, user_id, space):
        assert (workspace_id, user_id, space.id) == (11, 13, 7)
        self.purge_calls += 1
        return MemoryPhysicalDeleteCounts(
            spaces=1,
            items=2,
            revisions=3,
            sources=4,
            jobs=5,
            contexts=6,
        )


class _ConcurrentExportRepository(_LifecycleRepository):
    async def get_export_bundle(self, workspace_id, user_id, *, space):
        bundle = await super().get_export_bundle(workspace_id, user_id, space=space)
        self.space.catalog_version += 1
        return bundle


class _RetentionRepository:
    def __init__(self, now: datetime) -> None:
        self.now = now
        self.spaces = [
            SimpleNamespace(
                id=1,
                settings={
                    "archived_item_retention_days": 2,
                    "terminal_job_retention_days": 3,
                },
            ),
            SimpleNamespace(
                id=2,
                settings={"archived_item_retention_days": "invalid"},
            ),
        ]
        self.item_calls = []
        self.job_calls = []

    async def list_spaces_for_retention(self, *, after_space_id, limit, space_id=None):
        return [space for space in self.spaces if space.id > after_space_id][:limit]

    async def purge_archived_items_before(self, space_id, *, cutoff, limit):
        self.item_calls.append((space_id, cutoff, limit))
        return MemoryPhysicalDeleteCounts(
            items=1,
            revisions=2,
            sources=3,
        )

    async def purge_terminal_jobs_before(self, space_id, *, cutoff, limit):
        self.job_calls.append((space_id, cutoff, limit))
        return MemoryPhysicalDeleteCounts(jobs=1)


class _WorkerJobRunner:
    worker_id = "phase5-test-worker"

    async def run_once(self):
        return 0


class _StoppingWorkerJobRunner(_WorkerJobRunner):
    worker = None
    calls = 0

    async def run_once(self):
        self.calls += 1
        assert self.worker is not None
        self.worker.request_stop()
        return 0


class _WorkerRetentionRunner:
    def __init__(self) -> None:
        self.calls = 0

    async def run_once(self):
        self.calls += 1
        return MemoryRetentionResult(2, MemoryPhysicalDeleteCounts(items=1, jobs=1))


class _StoppingWorkerRetentionRunner(_WorkerRetentionRunner):
    worker = None

    async def run_once(self):
        result = await super().run_once()
        assert self.worker is not None
        self.worker.request_stop()
        return result


class _ExportConnection:
    async def get_isolation_level(self):
        return "REPEATABLE READ"


class _ExportSession:
    def __init__(self) -> None:
        self.info = {}
        self.events = []

    async def connection(self, *, execution_options):
        self.events.append(("connection", execution_options))
        return _ExportConnection()

    async def execute(self, statement, parameters):
        self.events.append(("scope", parameters))

    async def commit(self):
        self.events.append(("commit", None))

    async def rollback(self):
        self.events.append(("rollback", None))


class _ExportSessionContext:
    def __init__(self, session):
        self.session = session

    async def __aenter__(self):
        return self.session

    async def __aexit__(self, exc_type, exc, traceback):
        return False


class _ExportSessionFactory:
    def __init__(self, session):
        self.session = session

    def __call__(self):
        return _ExportSessionContext(self.session)


def test_export_and_purge_confirmation_are_tenant_and_version_bound():
    async def run() -> None:
        service = MemoryService(None)
        repo = _LifecycleRepository()
        service.repo = repo

        exported = await service.export_memories(11, 13)
        assert exported.bundle.space is repo.space
        confirmation = await service.issue_purge_confirmation(11, 13)
        assert confirmation.expected_catalog_version == 17
        counts = await service.purge_memories(
            11,
            13,
            confirmation_token=confirmation.confirmation_token,
            expected_catalog_version=17,
        )
        assert counts == MemoryPhysicalDeleteCounts(1, 2, 3, 4, 5, 6)
        assert repo.purge_calls == 1

        repo.space.catalog_version = 18
        with pytest.raises(AgentException) as version_error:
            await service.purge_memories(
                11,
                13,
                confirmation_token=confirmation.confirmation_token,
                expected_catalog_version=17,
            )
        assert version_error.value.status_code == 409

        repo.space.catalog_version = 17
        first = "A" if confirmation.confirmation_token[0] != "A" else "B"
        tampered = first + confirmation.confirmation_token[1:]
        with pytest.raises(AgentException) as token_error:
            await service.purge_memories(
                11,
                13,
                confirmation_token=tampered,
                expected_catalog_version=17,
            )
        assert token_error.value.status_code == 409

        expired = service._sign_purge_claims(
            {
                "v": 1,
                "workspace_id": 11,
                "user_id": 13,
                "space_id": 7,
                "catalog_version": 17,
                "expires_at": int(datetime.now(UTC).timestamp()) - 1,
            }
        )
        with pytest.raises(AgentException) as expired_error:
            await service.purge_memories(
                11,
                13,
                confirmation_token=expired,
                expected_catalog_version=17,
            )
        assert expired_error.value.status_code == 409
        assert repo.purge_calls == 1

    asyncio.run(run())


def test_export_rejects_catalog_change_during_multi_query_projection():
    async def run() -> None:
        service = MemoryService(None)
        service.repo = _ConcurrentExportRepository()
        with pytest.raises(AgentException) as error:
            await service.export_memories(11, 13)
        assert error.value.status_code == 409
        assert error.value.details == {
            "base_catalog_version": 17,
            "current_catalog_version": 18,
        }

    asyncio.run(run())


def test_export_runner_uses_independent_repeatable_read_session(monkeypatch):
    async def run() -> None:
        session = _ExportSession()
        expected = MemoryExportResult(
            datetime.now(UTC),
            MemoryExportBundle(None, (), (), (), (), ()),
        )

        async def export_memories(self, workspace_id, user_id):
            assert self.repo.db is session
            assert (workspace_id, user_id) == (11, 13)
            session.events.append(("export", None))
            return expected

        async def require_active_member(self, workspace_id, user_id):
            assert self.db is session
            assert (workspace_id, user_id) == (11, 13)
            session.events.append(("member_check", None))

        monkeypatch.setattr(MemoryService, "export_memories", export_memories)
        monkeypatch.setattr(
            "services.memory_service.WorkspaceService.require_active_member",
            require_active_member,
        )
        result = await MemoryExportRunner(session_factory=_ExportSessionFactory(session)).export(
            11, 13
        )
        assert result is expected
        assert session.events == [
            ("connection", {"isolation_level": "REPEATABLE READ"}),
            ("member_check", None),
            ("export", None),
            ("rollback", None),
        ]

    asyncio.run(run())


def test_retention_uses_space_overrides_and_returns_cascade_counts():
    async def run() -> None:
        now = datetime(2026, 7, 25, tzinfo=UTC)
        old_item_days = settings.memory_archived_item_retention_days
        old_job_days = settings.memory_terminal_job_retention_days
        try:
            settings.memory_archived_item_retention_days = 10
            settings.memory_terminal_job_retention_days = 20
            service = MemoryRetentionService(None)
            repo = _RetentionRepository(now)
            service.repo = repo

            result = await service.cleanup_batch(now=now, batch_size=5)
            assert result.spaces_scanned == 2
            assert result.counts == MemoryPhysicalDeleteCounts(
                items=2,
                revisions=4,
                sources=6,
                jobs=2,
            )
            assert repo.item_calls == [
                (1, now - timedelta(days=2), 5),
                (2, now - timedelta(days=10), 4),
            ]
            assert repo.job_calls == [
                (1, now - timedelta(days=3), 5),
                (2, now - timedelta(days=20), 4),
            ]
        finally:
            settings.memory_archived_item_retention_days = old_item_days
            settings.memory_terminal_job_retention_days = old_job_days

    asyncio.run(run())


def test_retention_cursor_prevents_low_id_space_starvation():
    async def run() -> None:
        now = datetime(2026, 7, 25, tzinfo=UTC)
        service = MemoryRetentionService(None)
        repo = _RetentionRepository(now)
        service.repo = repo

        first = await service.cleanup_batch(now=now, batch_size=1)
        assert first.next_space_id == 1
        assert [call[0] for call in repo.item_calls] == [1]

        repo.item_calls.clear()
        repo.job_calls.clear()
        second = await service.cleanup_batch(
            now=now,
            batch_size=1,
            after_space_id=first.next_space_id,
        )
        assert second.next_space_id == 2
        assert [call[0] for call in repo.item_calls] == [2]

        repo.item_calls.clear()
        repo.job_calls.clear()
        wrapped = await service.cleanup_batch(
            now=now,
            batch_size=1,
            after_space_id=second.next_space_id,
        )
        assert wrapped.next_space_id == 0
        assert repo.item_calls == []

    asyncio.run(run())


def test_worker_runs_retention_immediately_and_then_throttles():
    async def run() -> None:
        old_enabled = settings.memory_retention_enabled
        old_interval = settings.memory_retention_interval_seconds
        try:
            settings.memory_retention_enabled = True
            settings.memory_retention_interval_seconds = 3600
            retention_runner = _WorkerRetentionRunner()
            worker = MemoryWorker(
                runner=_WorkerJobRunner(),
                retention_runner=retention_runner,
            )
            await worker._run_retention_if_due()
            await worker._run_retention_if_due()
            assert retention_runner.calls == 1

            worker._next_retention_at = 0
            await worker._run_retention_if_due()
            assert retention_runner.calls == 2
        finally:
            settings.memory_retention_enabled = old_enabled
            settings.memory_retention_interval_seconds = old_interval

    asyncio.run(run())


def test_worker_runs_retention_when_job_consumption_is_disabled():
    async def run() -> None:
        old_extraction_enabled = settings.memory_extraction_enabled
        old_dream_enabled = settings.memory_dream_enabled
        old_retention_enabled = settings.memory_retention_enabled
        try:
            settings.memory_extraction_enabled = False
            settings.memory_dream_enabled = False
            settings.memory_retention_enabled = True
            retention_runner = _StoppingWorkerRetentionRunner()
            worker = MemoryWorker(
                runner=_WorkerJobRunner(),
                retention_runner=retention_runner,
            )
            retention_runner.worker = worker
            worker._install_signal_handlers = lambda: None
            await worker.run()
            assert retention_runner.calls == 1
        finally:
            settings.memory_extraction_enabled = old_extraction_enabled
            settings.memory_dream_enabled = old_dream_enabled
            settings.memory_retention_enabled = old_retention_enabled

    asyncio.run(run())


def test_worker_claims_extract_but_not_dream_when_only_extraction_enabled():
    async def run() -> None:
        names = (
            "memory_extraction_enabled",
            "memory_dream_enabled",
            "memory_retention_enabled",
        )
        original = {name: getattr(settings, name) for name in names}
        try:
            settings.memory_extraction_enabled = True
            settings.memory_dream_enabled = False
            settings.memory_retention_enabled = False

            job_runner = _StoppingWorkerJobRunner()
            worker = MemoryWorker(
                runner=job_runner,
                retention_runner=_WorkerRetentionRunner(),
            )
            job_runner.worker = worker
            worker._install_signal_handlers = lambda: None
            await worker.run()
            assert job_runner.calls == 1

            claimed_requests = []
            runner = object.__new__(MemoryJobRunner)

            async def claim_type(**values):
                claimed_requests.append(values)
                return ()

            runner._claim_type = claim_type
            assert await runner._claim(job_id=None) == ()
            assert [request["job_type"] for request in claimed_requests] == ["extract"]
            assert claimed_requests[0]["payload_mode"] is None
        finally:
            for name, value in original.items():
                setattr(settings, name, value)

    asyncio.run(run())
