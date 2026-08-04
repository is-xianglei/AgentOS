from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from pydantic import ValidationError

from agent import api as agent_api
from agent.models import AgentSkillRecord, AgentToolRecord
from agent.repository import AgentRepository
from agent.schemas import AgentUpdateRequest
from agent.service import AgentPage, AgentService
from core.errors import AgentException


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


class _FakeSession:
    def __init__(self) -> None:
        self.add_all = MagicMock()
        self.flush = AsyncMock()


def _agent_record(
    *,
    agent_id: int = 11,
    tool_ids: tuple[int, ...] = (2,),
    skill_ids: tuple[int, ...] = (7,),
) -> SimpleNamespace:
    now = datetime.now(UTC)
    return SimpleNamespace(
        id=agent_id,
        workspace_id=3,
        name="研究助手",
        description=None,
        system_prompt="负责严谨研究",
        model_name=None,
        is_enabled=True,
        created_by_user_id=5,
        created_at=now,
        updated_at=now,
        tool_bindings=[SimpleNamespace(tool_id=value) for value in tool_ids],
        skill_bindings=[SimpleNamespace(skill_id=value) for value in skill_ids],
    )


def test_PATCH只接受标量字段并允许显式清空可空字段() -> None:
    payload = AgentUpdateRequest.model_validate(
        {
            "description": None,
            "model_name": None,
        }
    )

    assert payload.model_dump(exclude_unset=True) == {
        "description": None,
        "model_name": None,
    }
    with pytest.raises(ValidationError):
        AgentUpdateRequest.model_validate({"tool_ids": [1]})
    with pytest.raises(ValidationError):
        AgentUpdateRequest.model_validate({})


@pytest.mark.anyio
async def test_创建Agent校验管理员权限和跨域绑定后一次写入() -> None:
    service = AgentService(_FakeSession())
    created = SimpleNamespace(id=11)
    expected = _agent_record()
    service.workspace_service.require_workspace_role = AsyncMock()
    service.repo.get_by_name = AsyncMock(return_value=None)
    service.tool_catalog_service.require_bindable_tool_ids = AsyncMock(return_value=[2, 1])
    service.skill_service.require_bindable_skill_ids = AsyncMock(return_value=[7])
    service.repo.create = AsyncMock(return_value=created)
    service.repo.replace_tool_bindings = AsyncMock()
    service.repo.replace_skill_bindings = AsyncMock()
    service.repo.get = AsyncMock(return_value=expected)

    result = await service.create_agent(
        workspace_id=3,
        actor_user_id=5,
        name="研究助手",
        description=None,
        system_prompt="负责严谨研究",
        model_name=None,
        is_enabled=True,
        tool_ids=[2, 1, 2],
        skill_ids=[7, 7],
    )

    assert result is expected
    service.workspace_service.require_workspace_role.assert_awaited_once_with(
        3,
        5,
        {"owner", "admin"},
    )
    service.tool_catalog_service.require_bindable_tool_ids.assert_awaited_once_with([2, 1, 2])
    service.skill_service.require_bindable_skill_ids.assert_awaited_once_with(3, [7, 7])
    service.repo.replace_tool_bindings.assert_awaited_once_with(11, [2, 1])
    service.repo.replace_skill_bindings.assert_awaited_once_with(11, [7])


@pytest.mark.anyio
async def test_普通成员可以分页读取Agent() -> None:
    service = AgentService(_FakeSession())
    record = _agent_record()
    service.workspace_service.require_active_membership = AsyncMock()
    service.repo.list_and_count = AsyncMock(return_value=([record], 9))

    page = await service.list_agents(3, 8, limit=20, offset=4)

    assert page == AgentPage(items=(record,), total=9, limit=20, offset=4)
    service.workspace_service.require_active_membership.assert_awaited_once_with(3, 8)
    service.repo.list_and_count.assert_awaited_once_with(
        3,
        keyword=None,
        is_enabled=None,
        limit=20,
        offset=4,
    )


@pytest.mark.anyio
async def test_非管理员替换绑定时不会发生写入() -> None:
    service = AgentService(_FakeSession())
    service.workspace_service.require_workspace_role = AsyncMock(
        side_effect=AgentException.message("无权执行该工作区操作", status_code=403)
    )
    service.repo.get_for_update = AsyncMock()
    service.repo.replace_tool_bindings = AsyncMock()

    with pytest.raises(AgentException) as exc_info:
        await service.replace_tool_bindings(3, 11, 8, [2])

    assert exc_info.value.status_code == 403
    service.repo.get_for_update.assert_not_awaited()
    service.repo.replace_tool_bindings.assert_not_awaited()


@pytest.mark.anyio
async def test_更新Agent支持清空可空字段且保留绑定() -> None:
    service = AgentService(_FakeSession())
    agent = _agent_record()
    agent.description = "旧描述"
    agent.model_name = "旧模型"
    service.workspace_service.require_workspace_role = AsyncMock()
    service.repo.get_for_update = AsyncMock(return_value=agent)
    service.repo.update = AsyncMock(return_value=agent)
    service.repo.get = AsyncMock(return_value=agent)
    service.repo.replace_tool_bindings = AsyncMock()
    service.repo.replace_skill_bindings = AsyncMock()

    result = await service.update_agent(
        3,
        11,
        5,
        {"description": None, "model_name": None},
    )

    assert result is agent
    assert agent.description is None
    assert agent.model_name is None
    service.repo.replace_tool_bindings.assert_not_awaited()
    service.repo.replace_skill_bindings.assert_not_awaited()


@pytest.mark.anyio
async def test_全量替换绑定会软删移除项复活旧项并只创建缺失项() -> None:
    db = _FakeSession()
    repo = AgentRepository(db)
    removed = AgentToolRecord(agent_id=11, tool_id=1)
    removed.is_deleted = False
    removed.deleted_at = None
    restored = AgentToolRecord(agent_id=11, tool_id=2)
    restored.is_deleted = True
    restored.deleted_at = datetime.now(UTC)
    repo._list_bindings = AsyncMock(return_value=[removed, restored])

    await repo.replace_tool_bindings(11, [2, 3, 3])

    assert removed.is_deleted is True
    assert removed.deleted_at is not None
    assert restored.is_deleted is False
    assert restored.deleted_at is None
    new_records = db.add_all.call_args.args[0]
    assert [(item.agent_id, item.tool_id) for item in new_records] == [(11, 3)]
    db.flush.assert_awaited_once()


@pytest.mark.anyio
async def test_Agent列表API返回统一分页结构(monkeypatch: pytest.MonkeyPatch) -> None:
    record = _agent_record(tool_ids=(5, 2), skill_ids=(9, 7))
    service = SimpleNamespace(
        list_agents=AsyncMock(
            return_value=AgentPage(items=(record,), total=1, limit=10, offset=0)
        )
    )
    monkeypatch.setattr(agent_api, "AgentService", lambda db: service)
    request = SimpleNamespace(state=SimpleNamespace(request_id="req-1"))

    response = await agent_api.list_agents(
        workspace_id=3,
        request=request,
        db=SimpleNamespace(),
        current_user=SimpleNamespace(id=8),
        keyword=None,
        is_enabled=None,
        limit=10,
        offset=0,
    )

    assert response["request_id"] == "req-1"
    assert response["data"].total == 1
    assert response["data"].limit == 10
    assert response["data"].items[0].tool_ids == [2, 5]
    assert response["data"].items[0].skill_ids == [7, 9]


def test_Agent路由使用Token工作区且不重复携带路径工作区() -> None:
    paths = {route.path for route in agent_api.router.routes}

    assert paths == {"", "/{agent_id}", "/{agent_id}/tools", "/{agent_id}/skills"}


def test_Agent表唯一约束和外键符合控制面契约() -> None:
    from agent.models import AgentRecord

    name_index = next(
        index
        for index in AgentRecord.__table__.indexes
        if index.name == "uq_agents_active_workspace_name"
    )
    assert name_index.unique
    assert "is_deleted = false" in str(name_index.dialect_options["postgresql"]["where"])

    tool_constraint = next(
        constraint
        for constraint in AgentToolRecord.__table__.constraints
        if constraint.name == "uq_agent_tools_agent_tool"
    )
    skill_constraint = next(
        constraint
        for constraint in AgentSkillRecord.__table__.constraints
        if constraint.name == "uq_agent_skills_agent_skill"
    )
    assert {column.name for column in tool_constraint.columns} == {"agent_id", "tool_id"}
    assert {column.name for column in skill_constraint.columns} == {"agent_id", "skill_id"}
    tool_foreign_key = next(iter(AgentToolRecord.__table__.c.tool_id.foreign_keys))
    skill_foreign_key = next(iter(AgentSkillRecord.__table__.c.skill_id.foreign_keys))
    assert tool_foreign_key.target_fullname == "tools.id"
    assert skill_foreign_key.target_fullname == "skills.id"
