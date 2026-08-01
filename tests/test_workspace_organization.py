from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

import database.registry  # noqa: F401
from core.errors import AgentException
from department.service import DepartmentService
from group.models import GroupMemberRecord
from group.service import GroupService
from workspace.service import WorkspaceService


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


class _FakeSession:
    pass


@pytest.mark.anyio
async def test_创建工作区时创建者落为_owner() -> None:
    service = WorkspaceService(_FakeSession())
    workspace = SimpleNamespace(id=17)
    service.repo.get_by_name = AsyncMock(return_value=None)
    service.repo.get_by_slug = AsyncMock(return_value=None)
    service.repo.create = AsyncMock(return_value=workspace)
    service.member_repo.create = AsyncMock()

    result = await service.create_workspace(
        creator_user_id=9,
        name="acme",
        slug="acme",
        display_name="Acme",
    )

    assert result is workspace
    assert service.member_repo.create.await_args.kwargs["role"] == "owner"
    assert service.member_repo.create.await_args.kwargs["user_id"] == 9


@pytest.mark.anyio
async def test_部门不能移动到自身子孙节点() -> None:
    service = DepartmentService(_FakeSession())
    department = SimpleNamespace(id=1, parent_id=None, manager_id=None)
    descendant = SimpleNamespace(id=3, parent_id=1, is_active=True)
    service.get_required = AsyncMock(side_effect=[department, descendant])
    service._require_manager_or_roles = AsyncMock()
    service.repo.list_descendants = AsyncMock(return_value=[descendant])
    service.repo.update = AsyncMock()

    with pytest.raises(AgentException, match="子孙部门"):
        await service.update_department(
            workspace_id=8,
            department_id=1,
            actor_user_id=5,
            updates={"parent_id": 3},
        )

    service.repo.update.assert_not_awaited()


@pytest.mark.anyio
async def test_共享父部门动态包含子部门成员() -> None:
    service = DepartmentService(_FakeSession())
    membership = SimpleNamespace(department_id=3)
    department = SimpleNamespace(id=3, is_active=True)
    parent = SimpleNamespace(id=2, is_active=True)
    root = SimpleNamespace(id=1, is_active=True)
    service.workspace_service.require_active_membership = AsyncMock(return_value=membership)
    service.repo.get_by_id = AsyncMock(return_value=department)
    service.repo.list_ancestors = AsyncMock(return_value=[parent, root])

    assert await service.is_user_in_shared_departments(8, 5, [1])
    assert not await service.is_user_in_shared_departments(8, 5, [7])


@pytest.mark.anyio
async def test_创建群组同步创建_owner_成员() -> None:
    service = GroupService(_FakeSession())
    group = SimpleNamespace(id=21)
    service.workspace_service.require_active_membership = AsyncMock()
    service.repo.get_by_slug = AsyncMock(return_value=None)
    service.repo.create = AsyncMock(return_value=group)
    service.member_repo.create_or_restore = AsyncMock()

    result = await service.create_group(
        8,
        5,
        name="Alpha",
        slug="alpha",
        group_type="team",
        visibility="workspace",
        join_mode="invite",
        settings={},
    )

    assert result is group
    assert service.repo.create.await_args.kwargs["owner_id"] == 5
    member_args = service.member_repo.create_or_restore.await_args.kwargs
    assert member_args["user_id"] == 5
    assert member_args["role"] == "owner"
    assert member_args["joined_at"].tzinfo is UTC


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("join_mode", "is_pending"),
    [("open", False), ("approval", True)],
)
async def test_开放与审批加入模式形成对应成员状态(
    join_mode: str,
    is_pending: bool,
) -> None:
    service = GroupService(_FakeSession())
    group = SimpleNamespace(id=21, visibility="workspace", join_mode=join_mode)
    service.workspace_service.require_active_membership = AsyncMock()
    service.require_active_group = AsyncMock(return_value=group)
    service.member_repo.get = AsyncMock(return_value=None)
    created = GroupMemberRecord(
        group_id=21,
        user_id=5,
        role="member",
        joined_at=None if is_pending else datetime.now(UTC),
    )
    service.member_repo.create_or_restore = AsyncMock(return_value=created)

    result = await service.join_group(8, 21, 5)

    assert result.status == ("pending" if is_pending else "active")
    assert (
        service.member_repo.create_or_restore.await_args.kwargs["joined_at"] is None
    ) is is_pending


@pytest.mark.anyio
async def test_邀请制群组拒绝自行加入() -> None:
    service = GroupService(_FakeSession())
    group = SimpleNamespace(id=21, visibility="workspace", join_mode="invite")
    service.workspace_service.require_active_membership = AsyncMock()
    service.require_active_group = AsyncMock(return_value=group)

    with pytest.raises(AgentException, match="邀请加入"):
        await service.join_group(8, 21, 5)


@pytest.mark.anyio
async def test_群组_owner_不能被直接移除() -> None:
    service = GroupService(_FakeSession())
    group = SimpleNamespace(id=21, owner_id=5)
    owner = SimpleNamespace(user_id=5, role="owner", joined_at=datetime.now(UTC))
    service.require_active_group = AsyncMock(return_value=group)
    service._require_group_member = AsyncMock(return_value=owner)
    service.member_repo.soft_delete = AsyncMock()

    with pytest.raises(AgentException, match="转让"):
        await service.remove_member(8, 21, 5, 9)

    service.member_repo.soft_delete.assert_not_awaited()


@pytest.mark.anyio
async def test_转让群组_owner_先锁定群组并同步双份状态() -> None:
    service = GroupService(_FakeSession())
    group = SimpleNamespace(id=21, owner_id=5, is_active=True)
    new_owner = SimpleNamespace(id=102, user_id=9, role="member", joined_at=datetime.now(UTC))
    current_owner = SimpleNamespace(id=101, user_id=5, role="owner", joined_at=datetime.now(UTC))
    service.repo.get_for_update = AsyncMock(return_value=group)
    service._require_group_owner = AsyncMock()
    service._require_group_member = AsyncMock(side_effect=[new_owner, current_owner])
    service.member_repo.update = AsyncMock(side_effect=lambda member: member)
    service.repo.update = AsyncMock(side_effect=lambda record: record)

    result = await service.transfer_owner(8, 21, new_owner_user_id=9, actor_user_id=5)

    assert result is group
    service.repo.get_for_update.assert_awaited_once_with(8, 21)
    assert current_owner.role == "admin"
    assert new_owner.role == "owner"
    assert group.owner_id == 9


@pytest.mark.anyio
async def test_移除工作区成员前锁定其群组并阻止遗留负责人() -> None:
    service = GroupService(_FakeSession())
    owned_group = SimpleNamespace(id=21, owner_id=5)
    service.member_repo.list_user_groups_for_update = AsyncMock(return_value=[owned_group])
    service.member_repo.soft_delete_by_workspace_user = AsyncMock()

    with pytest.raises(AgentException, match="先转让负责人"):
        await service.remove_user_from_workspace_groups(8, 5)

    service.member_repo.list_user_groups_for_update.assert_awaited_once_with(8, 5)
    service.member_repo.soft_delete_by_workspace_user.assert_not_awaited()


def test_组织模型已登记且群组成员唯一索引仅约束有效记录() -> None:
    from database.base import Base

    assert {"departments", "groups", "group_members"}.issubset(Base.metadata.tables)
    table = Base.metadata.tables["group_members"]
    index = next(item for item in table.indexes if item.name == "ix_group_members_group_user")
    assert index.unique
    assert "is_deleted = false" in str(index.dialect_options["postgresql"]["where"])
