from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from sqlalchemy.dialects import postgresql

import database.registry  # noqa: F401
from core.errors import AgentException
from department.service import DepartmentService
from group.service import GroupService
from session import service as session_service_module
from session.repository import SessionRepository
from session.service import SessionService
from task.service import TaskService
from team.service import TeamService


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


class _FakeSession:
    pass


class _CaptureSession:
    def __init__(self) -> None:
        self.statement = None

    async def scalars(self, statement):
        self.statement = statement
        return []


def _session(**overrides: object) -> SimpleNamespace:
    values = {
        "id": 11,
        "workspace_id": 8,
        "user_id": 1,
        "visibility": "private",
        "shared_with": [],
        "shared_with_departments": [],
        "shared_with_groups": [],
    }
    values.update(overrides)
    return SimpleNamespace(**values)


class _WorkspaceService:
    def __init__(self, db: object):
        self.db = db

    async def is_active_member(self, workspace_id: int, user_id: int) -> bool:
        return True

    async def require_active_membership(self, workspace_id: int, user_id: int):
        return SimpleNamespace(department_id=3)

    async def require_active_user_ids(self, workspace_id: int, user_ids: list[int]):
        return []


@pytest.mark.anyio
async def test_父部门共享授予读取但不授予写入(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(session_service_module, "WorkspaceService", _WorkspaceService)
    department_match = AsyncMock(return_value=True)
    monkeypatch.setattr(
        DepartmentService,
        "is_department_in_shared_departments",
        department_match,
    )
    service = SessionService(_FakeSession())
    session = _session(shared_with_departments=[2])

    assert await service.check_access(session, user_id=7, workspace_id=8)
    assert not await service.check_write_access(session, user_id=7, workspace_id=8)
    department_match.assert_awaited_once_with(8, 3, [2])


@pytest.mark.anyio
async def test_有效群组成员可以读取私有共享会话(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(session_service_module, "WorkspaceService", _WorkspaceService)
    group_match = AsyncMock(return_value=True)
    monkeypatch.setattr(GroupService, "is_user_in_groups", group_match)
    service = SessionService(_FakeSession())
    session = _session(shared_with_groups=[4])

    assert await service.check_access(session, user_id=7, workspace_id=8)
    group_match.assert_awaited_once_with(8, 7, [4])


@pytest.mark.anyio
async def test_跨工作区即使_public_也不可读(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(session_service_module, "WorkspaceService", _WorkspaceService)
    service = SessionService(_FakeSession())

    assert not await service.check_access(
        _session(visibility="public"),
        user_id=7,
        workspace_id=99,
    )


@pytest.mark.anyio
async def test_覆盖共享范围时去重并移除创建者自身(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace_service = _WorkspaceService(_FakeSession())
    workspace_service.require_active_user_ids = AsyncMock(return_value=[])
    monkeypatch.setattr(
        session_service_module,
        "WorkspaceService",
        lambda db: workspace_service,
    )
    department_validation = AsyncMock(return_value=[])
    group_validation = AsyncMock(return_value=[])
    monkeypatch.setattr(
        DepartmentService,
        "require_department_ids",
        department_validation,
    )
    monkeypatch.setattr(GroupService, "require_active_group_ids", group_validation)

    service = SessionService(_FakeSession())
    session = _session()
    service.get_required = AsyncMock(return_value=session)
    service.check_write_access = AsyncMock(return_value=True)
    service.repo.update_sharing = AsyncMock(return_value=session)

    await service.share(
        11,
        actor_user_id=1,
        workspace_id=8,
        visibility="private",
        user_ids=[1, 7, 7],
        department_ids=[2, 2],
        group_ids=[4, 4],
    )

    workspace_service.require_active_user_ids.assert_awaited_once_with(8, [7])
    department_validation.assert_awaited_once_with(8, [2])
    group_validation.assert_awaited_once_with(8, [4])
    update_args = service.repo.update_sharing.await_args.kwargs
    assert update_args["user_ids"] == [7]
    assert update_args["department_ids"] == [2]
    assert update_args["group_ids"] == [4]


@pytest.mark.anyio
async def test_会话列表使用当前部门祖先与有效群组范围(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(session_service_module, "WorkspaceService", _WorkspaceService)
    department_scope = AsyncMock(return_value=[3, 2, 1])
    group_scope = AsyncMock(return_value=[SimpleNamespace(id=4), SimpleNamespace(id=6)])
    monkeypatch.setattr(DepartmentService, "get_department_scope_ids", department_scope)
    monkeypatch.setattr(GroupService, "list_groups_for_active_member", group_scope)
    service = SessionService(_FakeSession())
    service.repo.list_accessible = AsyncMock(return_value=[])

    await service.list_accessible(user_id=7, workspace_id=8)

    service.repo.list_accessible.assert_awaited_once_with(
        7,
        8,
        department_scope_ids=[3, 2, 1],
        group_ids=[4, 6],
    )


@pytest.mark.anyio
async def test_可读列表排除无创建者历史会话并使用_jsonb_包含查询() -> None:
    db = _CaptureSession()

    await SessionRepository(db).list_accessible(
        user_id=7,
        workspace_id=8,
        department_scope_ids=[3],
        group_ids=[4],
    )

    sql = str(db.statement.compile(dialect=postgresql.dialect()))
    assert "sessions.user_id IS NOT NULL" in sql
    assert sql.count(" @> ") == 3


@pytest.mark.anyio
async def test_共享读取者不能通过_service_归档会话() -> None:
    service = SessionService(_FakeSession())
    service.get_required = AsyncMock(return_value=_session())
    service.check_write_access = AsyncMock(return_value=False)
    service.repo.update_status = AsyncMock()

    with pytest.raises(AgentException, match="只有会话创建者"):
        await service.archive(11, actor_user_id=7, workspace_id=8)

    service.repo.update_status.assert_not_awaited()


@pytest.mark.anyio
async def test_任务与团队_http_读取均委托会话_acl() -> None:
    task_service = TaskService(_FakeSession())
    task_service.session_service.require_read_access = AsyncMock()
    task_service.repo.list_by_session = AsyncMock(return_value=[])
    await task_service.list_by_session_for_user(11, user_id=7, workspace_id=8)
    task_service.session_service.require_read_access.assert_awaited_once_with(11, 7, 8)

    team_service = TeamService(_FakeSession())
    team_service.session_service.require_read_access = AsyncMock()
    team_service.repo.list_messages = AsyncMock(return_value=[])
    await team_service.list_messages_for_user(11, user_id=7, workspace_id=8)
    team_service.session_service.require_read_access.assert_awaited_once_with(11, 7, 8)


def test_会话共享_jsonb_列均配置_gin_索引() -> None:
    from database.base import Base

    indexes = {item.name: item for item in Base.metadata.tables["sessions"].indexes}
    expected = {
        "ix_sessions_shared_with_gin",
        "ix_sessions_shared_with_departments_gin",
        "ix_sessions_shared_with_groups_gin",
    }
    assert expected.issubset(indexes)
    assert all(indexes[name].dialect_options["postgresql"]["using"] == "gin" for name in expected)


def test_部门树静态路由先于动态详情路由注册() -> None:
    from main import app

    paths = [route.path for route in app.routes]
    tree_index = paths.index("/api/workspaces/{workspace_id}/departments/tree")
    detail_index = paths.index("/api/workspaces/{workspace_id}/departments/{department_id}")
    assert tree_index < detail_index
