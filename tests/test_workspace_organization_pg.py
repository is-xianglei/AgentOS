"""部门、群组与会话共享的真实 PostgreSQL 集成测试。"""

from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any
from uuid import uuid4

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

import database.registry  # noqa: F401
from core.errors import AgentException
from database.engine import engine
from department.service import DepartmentService
from group.service import GroupService
from session.service import SessionService
from user.models import UserRecord
from workspace.service import WorkspaceService


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


@pytest.fixture
async def db() -> Any:
    """使用外层事务隔离真实 PostgreSQL 数据，测试结束统一回滚。"""
    async with engine.connect() as connection:
        transaction = await connection.begin()
        session = AsyncSession(
            bind=connection,
            expire_on_commit=False,
            join_transaction_mode="create_savepoint",
        )
        try:
            yield session
        finally:
            await session.close()
            await transaction.rollback()


@pytest.fixture
async def organization(db: AsyncSession) -> SimpleNamespace:
    suffix = uuid4().hex
    owner = UserRecord(
        email=f"org-owner-{suffix}@example.invalid",
        username=f"org-owner-{suffix[:16]}",
        password="test-only-hash",
    )
    reader = UserRecord(
        email=f"org-reader-{suffix}@example.invalid",
        username=f"org-reader-{suffix[:16]}",
        password="test-only-hash",
    )
    db.add_all([owner, reader])
    await db.flush()

    workspaces = WorkspaceService(db)
    workspace = await workspaces.create_workspace(
        creator_user_id=owner.id,
        name=f"org-{suffix}",
        slug=f"org-{suffix}",
        display_name="组织集成测试",
    )
    await workspaces.member_repo.create(
        workspace_id=workspace.id,
        user_id=reader.id,
        invited_by=owner.id,
        invited_at=datetime.now(UTC),
        joined_at=datetime.now(UTC),
        role="member",
    )

    departments = DepartmentService(db)
    root = await departments.create_department(
        workspace.id,
        owner.id,
        "工程部",
    )
    child = await departments.create_department(
        workspace.id,
        owner.id,
        "后端组",
        parent_id=root.id,
    )
    await departments.set_member_department(
        workspace.id,
        reader.id,
        owner.id,
        child.id,
        "工程师",
    )

    groups = GroupService(db)
    group = await groups.create_group(
        workspace.id,
        owner.id,
        name="项目组",
        slug=f"project-{suffix}",
        group_type="team",
        visibility="workspace",
        join_mode="invite",
        settings={},
    )
    group_member = await groups.invite_member(
        workspace.id,
        group.id,
        owner.id,
        reader.id,
    )
    return SimpleNamespace(
        owner=owner,
        reader=reader,
        workspace=workspace,
        root=root,
        child=child,
        group=group,
        group_member=group_member,
    )


@pytest.mark.anyio
async def test_递归部门查询保持层级顺序并隔离工作区(
    db: AsyncSession,
    organization: SimpleNamespace,
) -> None:
    context = organization
    service = DepartmentService(db)
    sibling = await service.create_department(
        context.workspace.id,
        context.owner.id,
        "前端组",
        parent_id=context.root.id,
        sort_order=1,
    )
    grandchild = await service.create_department(
        context.workspace.id,
        context.owner.id,
        "基础设施组",
        parent_id=context.child.id,
    )

    descendants = await service.repo.list_descendants(context.workspace.id, context.root.id)
    ancestors = await service.repo.list_ancestors(context.workspace.id, grandchild.id)

    assert [item.id for item in descendants] == [context.child.id, sibling.id, grandchild.id]
    assert [item.id for item in ancestors] == [context.child.id, context.root.id]
    assert await service.repo.list_descendants(context.workspace.id + 1, context.root.id) == []


@pytest.mark.anyio
async def test_jsonb_共享范围动态撤权且群组成员可复活(
    db: AsyncSession,
    organization: SimpleNamespace,
) -> None:
    context = organization
    sessions = SessionService(db)

    async def create_shared(
        title: str,
        *,
        visibility: str = "private",
        users: list[int] | None = None,
        departments: list[int] | None = None,
        groups: list[int] | None = None,
    ):
        session = await sessions.create(
            title=title,
            user_id=context.owner.id,
            workspace_id=context.workspace.id,
        )
        return await sessions.share(
            session.id,
            context.owner.id,
            context.workspace.id,
            visibility=visibility,
            user_ids=users or [],
            department_ids=departments or [],
            group_ids=groups or [],
        )

    direct = await create_shared("直接共享", users=[context.reader.id])
    department = await create_shared("部门共享", departments=[context.root.id])
    group = await create_shared("群组共享", groups=[context.group.id])
    workspace = await create_shared("工作区共享", visibility="workspace")
    private = await create_shared("保持私有")
    orphan = await sessions.create(
        title="历史无创建者会话",
        user_id=None,
        workspace_id=context.workspace.id,
    )
    orphan.visibility = "workspace"
    await db.flush()

    accessible = await sessions.list_accessible(context.reader.id, context.workspace.id)
    assert {item.id for item in accessible} == {
        direct.id,
        department.id,
        group.id,
        workspace.id,
    }
    assert private.id not in {item.id for item in accessible}
    assert orphan.id not in {item.id for item in accessible}

    groups = GroupService(db)
    await groups.remove_member(
        context.workspace.id,
        context.group.id,
        context.reader.id,
        context.owner.id,
    )
    after_leave = await sessions.list_accessible(context.reader.id, context.workspace.id)
    assert group.id not in {item.id for item in after_leave}

    restored = await groups.invite_member(
        context.workspace.id,
        context.group.id,
        context.owner.id,
        context.reader.id,
    )
    assert restored.id == context.group_member.id

    await DepartmentService(db).set_member_department(
        context.workspace.id,
        context.reader.id,
        context.owner.id,
        None,
        None,
    )
    after_transfer = await sessions.list_accessible(context.reader.id, context.workspace.id)
    accessible_ids = {item.id for item in after_transfer}
    assert department.id not in accessible_ids
    assert group.id in accessible_ids

    transferred = await groups.transfer_owner(
        context.workspace.id,
        context.group.id,
        context.reader.id,
        context.owner.id,
    )
    assert transferred.owner_id == context.reader.id
    with pytest.raises(AgentException, match="先转让负责人"):
        await WorkspaceService(db).remove_member(
            context.workspace.id,
            context.reader.id,
            context.owner.id,
        )
