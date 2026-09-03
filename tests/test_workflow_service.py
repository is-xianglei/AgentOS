"""WorkflowService 保存期校验与运行期成员排序的单元测试。

用替身替换 Repository 与协作 Service，只验证本域业务规则；
唯一约束、软删过滤等 PostgreSQL 特性留给集成测试。
"""

from types import SimpleNamespace

import pytest

from core.errors import AgentException
from workflow.models import WorkflowMode
from workflow.service import WorkflowMemberInput, WorkflowService

pytestmark = pytest.mark.anyio


class _FakeWorkspaceService:
    async def require_workspace_role(self, workspace_id, user_id, roles) -> None:
        return None

    async def require_active_membership(self, workspace_id, user_id) -> None:
        return None


class _FakeAgentService:
    def __init__(self, existing_ids: set[int]):
        self.existing_ids = existing_ids
        self.calls: list[list[int]] = []

    async def require_bindable_agent_ids(self, workspace_id, agent_ids):
        self.calls.append(list(agent_ids))
        missing = [agent_id for agent_id in agent_ids if agent_id not in self.existing_ids]
        if missing:
            raise AgentException.message("绑定的Agent不存在", {"agent_ids": missing})
        return list(dict.fromkeys(agent_ids))


class _FakeRepo:
    def __init__(self, record=None):
        self.record = record
        self.created: list[dict] = []
        self.replaced: list[tuple[int, dict[int, int]]] = []

    async def get_by_name(self, workspace_id, name, *, exclude_workflow_id=None):
        return None

    async def create(self, **fields):
        self.created.append(fields)
        self.record = SimpleNamespace(id=7, agents=[], **fields)
        return self.record

    async def replace_members(self, workflow_id, members):
        self.replaced.append((workflow_id, members))

    async def get(self, workspace_id, workflow_id):
        return self.record


def _build_service(repo: _FakeRepo, agent_service: _FakeAgentService) -> WorkflowService:
    service = WorkflowService.__new__(WorkflowService)
    service.db = None
    service.repo = repo
    service.workspace_service = _FakeWorkspaceService()
    service.agent_service = agent_service
    return service


async def _create(service: WorkflowService, members: list[WorkflowMemberInput]):
    return await service.create_workflow(
        workspace_id=1,
        actor_user_id=1,
        name="发布流程",
        description=None,
        mode=WorkflowMode.SEQUENTIAL,
        is_enabled=True,
        members=members,
    )


async def test_create_rejects_empty_members() -> None:
    service = _build_service(_FakeRepo(), _FakeAgentService({1, 2}))
    with pytest.raises(AgentException, match="至少需要绑定1个成员"):
        await _create(service, [])


async def test_create_rejects_duplicated_agent() -> None:
    agent_service = _FakeAgentService({1, 2})
    service = _build_service(_FakeRepo(), agent_service)
    with pytest.raises(AgentException, match="重复绑定"):
        await _create(service, [WorkflowMemberInput(1, 0), WorkflowMemberInput(1, 1)])
    assert agent_service.calls == []


async def test_create_rejects_agent_outside_workspace() -> None:
    service = _build_service(_FakeRepo(), _FakeAgentService({1}))
    with pytest.raises(AgentException, match="Agent不存在"):
        await _create(service, [WorkflowMemberInput(1, 0), WorkflowMemberInput(9, 1)])


async def test_create_persists_mode_value_and_members() -> None:
    repo = _FakeRepo()
    service = _build_service(repo, _FakeAgentService({1, 2}))
    record = await _create(service, [WorkflowMemberInput(2, 5), WorkflowMemberInput(1, 0)])
    assert record.id == 7
    assert repo.created[0]["mode"] == "sequential"
    assert repo.replaced == [(7, {2: 5, 1: 0})]


def _record(*, is_enabled: bool, members: list[tuple[int, int]]):
    return SimpleNamespace(
        id=7,
        is_enabled=is_enabled,
        agents=[SimpleNamespace(agent_id=a, sort_order=s) for a, s in members],
    )


async def test_require_runnable_rejects_disabled_and_missing() -> None:
    disabled = _build_service(_FakeRepo(_record(is_enabled=False, members=[])), None)
    with pytest.raises(AgentException, match="已停用") as exc_info:
        await disabled.require_runnable(7, 1)
    assert exc_info.value.status_code == 409

    missing = _build_service(_FakeRepo(None), None)
    with pytest.raises(AgentException, match="不存在") as exc_info:
        await missing.require_runnable(7, 1)
    assert exc_info.value.status_code == 404


async def test_get_run_members_sorted_by_sort_order_then_agent_id() -> None:
    record = _record(is_enabled=True, members=[(30, 1), (10, 1), (20, 0)])
    service = _build_service(_FakeRepo(record), None)
    members = await service.get_run_members(7, 1)
    assert [(m.agent_id, m.sort_order) for m in members] == [(20, 0), (10, 1), (30, 1)]


def test_list_modes_covers_all_enum_values() -> None:
    metas = WorkflowService.list_modes()
    assert {meta.mode for meta in metas} == set(WorkflowMode)
    assert all(meta.name and meta.description for meta in metas)
