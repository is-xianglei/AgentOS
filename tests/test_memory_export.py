from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Self
from unittest.mock import AsyncMock

import pytest
from sqlalchemy.orm import Session, make_transient_to_detached

import database.registry  # noqa: F401
from memory import service as memory_service_module
from memory.api import _to_export_response
from memory.models import MemoryRevisionRecord
from memory.repository import MemoryExportBundle
from memory.service import MemoryExportResult, MemoryExportRunner


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


class _FakeConnection:
    async def get_isolation_level(self) -> str:
        return "REPEATABLE READ"


class _FakeExportSession:
    def __init__(self, record: MemoryRevisionRecord) -> None:
        self.sync_session = Session()
        self.sync_session.add(record)

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(self, exc_type, exc, traceback) -> None:
        self.sync_session.close()

    async def connection(self, *, execution_options: dict[str, str]) -> _FakeConnection:
        assert execution_options == {"isolation_level": "REPEATABLE READ"}
        return _FakeConnection()

    def expunge_all(self) -> None:
        self.sync_session.expunge_all()

    async def rollback(self) -> None:
        self.sync_session.rollback()


@pytest.mark.anyio
async def test_导出快照回滚后ORM投影仍可转换为响应(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    now = datetime.now(UTC)
    revision = MemoryRevisionRecord(
        id=11,
        memory_id=5,
        revision=1,
        memory_type="user",
        name="偏好",
        description="用户偏好",
        body="使用简体中文",
        actor_type="user",
        actor_id="7",
        run_id=None,
        created_at=now,
        updated_at=now,
        is_deleted=False,
        deleted_at=None,
    )
    make_transient_to_detached(revision)
    export_result = MemoryExportResult(
        exported_at=now,
        bundle=MemoryExportBundle(None, (), (revision,), (), (), ()),
    )
    workspace_service = SimpleNamespace(require_active_member=AsyncMock())
    memory_service = SimpleNamespace(export_memories=AsyncMock(return_value=export_result))
    monkeypatch.setattr(
        memory_service_module,
        "WorkspaceService",
        lambda db: workspace_service,
    )
    monkeypatch.setattr(
        memory_service_module,
        "MemoryService",
        lambda db: memory_service,
    )
    fake_session = _FakeExportSession(revision)

    result = await MemoryExportRunner(session_factory=lambda: fake_session).export(3, 7)
    response = _to_export_response(result, workspace_id=3, user_id=7)

    assert response.workspace_id == 3
    workspace_service.require_active_member.assert_awaited_once_with(3, 7)
    memory_service.export_memories.assert_awaited_once_with(3, 7)
