"""部门删除与建子部门、移入部门、分配成员并发时的真实 PostgreSQL 集成测试。

READ COMMITTED 下双方都看不到对方未提交的改动；软删除只改非键列，与外键检查的
FOR KEY SHARE 不冲突，不加锁时两边都能提交，留下挂在已删除部门下的子部门或成员。
并发用例用两个独立会话真实提交，测试数据在 fixture 收尾时按工作区物理删除。
"""

import asyncio
import uuid
from collections.abc import AsyncIterator, Callable, Coroutine
from datetime import UTC, datetime

import pytest
from sqlalchemy import delete, select, text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, create_async_engine
from sqlalchemy.pool import NullPool

import database.engine  # noqa: F401  副作用注册全部映射与全局软删过滤
from core.config import settings
from core.errors import AgentException
from department.models import DepartmentRecord
from department.service import DepartmentService
from user.models import UserRecord
from workspace.models import WorkspaceMemberRecord, WorkspaceRecord

pytestmark = pytest.mark.anyio

type DepartmentOp = Callable[[AsyncSession], Coroutine[object, object, object]]

_NOT_FOUND = ("部门不存在", 404)
_NOT_LEAF = ("仅允许删除叶子部门", 409)
_HAS_MEMBERS = ("部门仍有成员，不能删除", 409)
# 判定“仍在等锁”的观察窗口：修复前等待方不拿锁，会在窗口内直接结束。
_LOCK_WAIT_WINDOW_SECONDS = 2.0

# waiter 正在排队等待 holder 已持有的同一把咨询锁，且锁的第二个键是该工作区 ID。
_WAITING_ON_HOLDER_SQL = text(
    """
    SELECT EXISTS (
        SELECT 1
        FROM pg_locks AS waiting
        JOIN pg_locks AS held
          ON held.locktype = 'advisory'
         AND held.granted
         AND held.pid = :holder_pid
         AND held.database = waiting.database
         AND held.classid = waiting.classid
         AND held.objid = waiting.objid
         AND held.objsubid = waiting.objsubid
        WHERE waiting.locktype = 'advisory'
          AND NOT waiting.granted
          AND waiting.pid = :waiter_pid
          AND waiting.objsubid = 2
          AND waiting.objid::bigint = :workspace_id
    )
    """
)

# pid 已持有以该工作区 ID 为第二个键的双键咨询锁（双键形式在 pg_locks 中 objsubid = 2）。
_HOLDS_LOCK_SQL = text(
    """
    SELECT EXISTS (
        SELECT 1
        FROM pg_locks
        WHERE locktype = 'advisory'
          AND granted
          AND pid = :pid
          AND objsubid = 2
          AND objid::bigint = :workspace_id
    )
    """
)


async def _seed_tenant(db: AsyncSession) -> tuple[int, int]:
    """建一个工作区和已加入的 owner，满足删除部门只允许 owner 的权限要求。"""
    suffix = uuid.uuid4().hex[:12]
    user = UserRecord(
        email=f"dept-del-{suffix}@test.local",
        username=f"dept-del-{suffix}",
        password="x",
    )
    workspace = WorkspaceRecord(
        name=f"dept-del-{suffix}",
        slug=f"dept-del-{suffix}",
        display_name="部门删除并发测试",
    )
    db.add_all([user, workspace])
    await db.flush()
    db.add(
        WorkspaceMemberRecord(
            workspace_id=workspace.id,
            user_id=user.id,
            role="owner",
            joined_at=datetime.now(UTC),
        )
    )
    await db.flush()
    return workspace.id, user.id


@pytest.fixture
async def tenant() -> AsyncIterator[tuple[AsyncEngine, int, int]]:
    """已提交的工作区与 owner；用例结束后按工作区物理删除全部测试数据。"""
    engine = create_async_engine(settings.database_url, poolclass=NullPool)
    try:
        async with engine.connect() as probe:
            await probe.execute(text("SELECT 1"))
    except Exception as exc:  # noqa: BLE001  任何连接错误都只用于决定跳过
        await engine.dispose()
        pytest.skip(f"PostgreSQL 不可达(AGENTOS_DATABASE_URL): {type(exc).__name__}")
    seeded: list[tuple[int, int]] = []
    try:
        async with AsyncSession(engine, expire_on_commit=False) as db:
            seeded.append(await _seed_tenant(db))
            await db.commit()
        workspace_id, user_id = seeded[0]
        yield engine, workspace_id, user_id
    finally:
        # 并发用例真实提交，无法靠回滚清理；测试数据直接物理删除，不走软删除。
        workspace_ids = [workspace_id for workspace_id, _ in seeded]
        user_ids = [user_id for _, user_id in seeded]
        async with engine.begin() as conn:
            await conn.execute(
                delete(DepartmentRecord).where(DepartmentRecord.workspace_id.in_(workspace_ids))
            )
            await conn.execute(
                delete(WorkspaceMemberRecord).where(
                    WorkspaceMemberRecord.workspace_id.in_(workspace_ids),
                )
            )
            await conn.execute(delete(WorkspaceRecord).where(WorkspaceRecord.id.in_(workspace_ids)))
            await conn.execute(delete(UserRecord).where(UserRecord.id.in_(user_ids)))
        await engine.dispose()


@pytest.fixture
async def member_uid(tenant) -> AsyncIterator[int]:
    """已提交且已加入（joined_at 不为空）的普通成员，在 tenant 收尾之前物理删除。"""
    engine, ws, _ = tenant
    suffix = uuid.uuid4().hex[:12]
    async with AsyncSession(engine, expire_on_commit=False) as db:
        user = UserRecord(
            email=f"dept-member-{suffix}@test.local",
            username=f"dept-member-{suffix}",
            password="x",
        )
        db.add(user)
        await db.flush()
        db.add(
            WorkspaceMemberRecord(
                workspace_id=ws,
                user_id=user.id,
                role="member",
                joined_at=datetime.now(UTC),
            )
        )
        await db.commit()
    try:
        yield user.id
    finally:
        async with engine.begin() as conn:
            await conn.execute(
                delete(WorkspaceMemberRecord).where(WorkspaceMemberRecord.user_id == user.id)
            )
            await conn.execute(delete(UserRecord).where(UserRecord.id == user.id))


async def _seed_departments(engine: AsyncEngine, ws: int, uid: int, *names: str) -> dict[str, int]:
    """提交若干顶级部门作为并发起点，返回名称到 ID 的映射。"""
    async with AsyncSession(engine, expire_on_commit=False) as db:
        service = DepartmentService(db)
        ids = {name: (await service.create_department(ws, uid, name)).id for name in names}
        await db.commit()
        return ids


def _create(ws: int, uid: int, name: str, parent_id: int | None) -> DepartmentOp:
    return lambda db: DepartmentService(db).create_department(ws, uid, name, parent_id=parent_id)


def _delete(ws: int, uid: int, department_id: int) -> DepartmentOp:
    return lambda db: DepartmentService(db).delete_department(ws, department_id, uid)


def _move(ws: int, uid: int, department_id: int, parent_id: int) -> DepartmentOp:
    updates = {"parent_id": parent_id}
    return lambda db: DepartmentService(db).update_department(ws, department_id, uid, updates)


def _assign(
    ws: int,
    uid: int,
    member_uid: int,
    department_id: int | None,
    job_title: str | None = None,
) -> DepartmentOp:
    return lambda db: DepartmentService(db).set_member_department(
        ws,
        member_uid,
        uid,
        department_id,
        job_title,
    )


async def _commit(engine: AsyncEngine, op: DepartmentOp) -> object:
    """在独立会话中执行并提交，返回操作结果。"""
    async with AsyncSession(engine, expire_on_commit=False) as db:
        result = await op(db)
        await db.commit()
        return result


async def _member_department(engine: AsyncEngine, ws: int, member_uid: int) -> int | None:
    async with engine.connect() as conn:
        return await conn.scalar(
            select(WorkspaceMemberRecord.department_id).where(
                WorkspaceMemberRecord.workspace_id == ws,
                WorkspaceMemberRecord.user_id == member_uid,
            )
        )


async def _backend_pid(db: AsyncSession) -> int:
    conn = await db.connection()
    return (await conn.execute(text("SELECT pg_backend_pid()"))).scalar_one()


async def _waits_on_lock(
    engine: AsyncEngine,
    task: asyncio.Task,
    *,
    waiter_pid: int,
    holder_pid: int,
    workspace_id: int,
) -> bool:
    """在观察窗口内轮询 pg_locks；任务提前结束说明它没有等锁。"""
    params = {"waiter_pid": waiter_pid, "holder_pid": holder_pid, "workspace_id": workspace_id}
    deadline = asyncio.get_running_loop().time() + _LOCK_WAIT_WINDOW_SECONDS
    async with engine.connect() as observer:
        while asyncio.get_running_loop().time() < deadline:
            if task.done():
                return False
            if (await observer.execute(_WAITING_ON_HOLDER_SQL, params)).scalar_one():
                return not task.done()
            await asyncio.sleep(0.05)
    return False


async def _race(
    engine: AsyncEngine,
    workspace_id: int,
    *,
    holder: DepartmentOp,
    waiter: DepartmentOp,
) -> tuple[bool, tuple[str, int] | None]:
    """holder 执行完不提交，期间并发执行 waiter；holder 提交后再结束 waiter。

    返回 waiter 是否等过 holder 的部门树锁，以及 waiter 失败时的报错文案和状态码。
    """
    async with (
        AsyncSession(engine, expire_on_commit=False) as db_holder,
        AsyncSession(engine, expire_on_commit=False) as db_waiter,
    ):
        await holder(db_holder)
        holder_pid = await _backend_pid(db_holder)
        waiter_pid = await _backend_pid(db_waiter)
        task = asyncio.create_task(waiter(db_waiter))
        waited = await _waits_on_lock(
            engine,
            task,
            waiter_pid=waiter_pid,
            holder_pid=holder_pid,
            workspace_id=workspace_id,
        )
        await db_holder.commit()
        error: tuple[str, int] | None = None
        try:
            await task
            await db_waiter.commit()
        except AgentException as exc:
            error = (exc.message, exc.status_code)
            await db_waiter.rollback()
    return waited, error


async def _snapshot(engine: AsyncEngine, workspace_id: int) -> dict[str, tuple[int | None, bool]]:
    """按名称返回全部部门（含软删除）的 (parent_id, is_deleted)。"""
    async with engine.connect() as conn:
        rows = await conn.execute(
            select(
                DepartmentRecord.name,
                DepartmentRecord.parent_id,
                DepartmentRecord.is_deleted,
            ).where(DepartmentRecord.workspace_id == workspace_id)
        )
        return {row.name: (row.parent_id, row.is_deleted) for row in rows}


async def test_create_child_waits_for_pending_delete(tenant) -> None:
    engine, ws, uid = tenant
    ids = await _seed_departments(engine, ws, uid, "L")
    # A 删除 L 未提交时，B 在 L 下建子部门：B 等锁，A 提交后 B 看到 L 已删除。
    waited, error = await _race(
        engine,
        ws,
        holder=_delete(ws, uid, ids["L"]),
        waiter=_create(ws, uid, "C", ids["L"]),
    )
    assert {
        "b_waited_on_lock": waited,
        "b_error": error,
        "departments": await _snapshot(engine, ws),
    } == {
        "b_waited_on_lock": True,
        "b_error": _NOT_FOUND,
        "departments": {"L": (None, True)},
    }


async def test_delete_waits_for_pending_child_create(tenant) -> None:
    engine, ws, uid = tenant
    ids = await _seed_departments(engine, ws, uid, "L")
    # B 在 L 下建子部门未提交时，A 删除 L：A 等锁，B 提交后 A 看到 L 已有子部门。
    waited, error = await _race(
        engine,
        ws,
        holder=_create(ws, uid, "C", ids["L"]),
        waiter=_delete(ws, uid, ids["L"]),
    )
    assert {
        "a_waited_on_lock": waited,
        "a_error": error,
        "departments": await _snapshot(engine, ws),
    } == {
        "a_waited_on_lock": True,
        "a_error": _NOT_LEAF,
        "departments": {"L": (None, False), "C": (ids["L"], False)},
    }


async def test_move_into_pending_deleted_department_waits(tenant) -> None:
    engine, ws, uid = tenant
    ids = await _seed_departments(engine, ws, uid, "L", "M")
    # A 删除 L 未提交时，B 把 M 移到 L 下：B 等锁，A 提交后 B 看到 L 已删除，M 留在顶级。
    waited, error = await _race(
        engine,
        ws,
        holder=_delete(ws, uid, ids["L"]),
        waiter=_move(ws, uid, ids["M"], ids["L"]),
    )
    assert {
        "b_waited_on_lock": waited,
        "b_error": error,
        "departments": await _snapshot(engine, ws),
    } == {
        "b_waited_on_lock": True,
        "b_error": _NOT_FOUND,
        "departments": {"L": (None, True), "M": (None, False)},
    }


async def test_delete_waits_for_pending_move_into_department(tenant) -> None:
    engine, ws, uid = tenant
    ids = await _seed_departments(engine, ws, uid, "L", "M")
    # 先后反过来：B 把 M 移到 L 下未提交时，A 删除 L，A 等锁后看到 L 已有子部门。
    waited, error = await _race(
        engine,
        ws,
        holder=_move(ws, uid, ids["M"], ids["L"]),
        waiter=_delete(ws, uid, ids["L"]),
    )
    assert {
        "a_waited_on_lock": waited,
        "a_error": error,
        "departments": await _snapshot(engine, ws),
    } == {
        "a_waited_on_lock": True,
        "a_error": _NOT_LEAF,
        "departments": {"L": (None, False), "M": (ids["L"], False)},
    }


async def test_create_top_level_does_not_wait_for_tree_lock(tenant) -> None:
    engine, ws, uid = tenant
    ids = await _seed_departments(engine, ws, uid, "L")
    async with (
        AsyncSession(engine, expire_on_commit=False) as db_a,
        AsyncSession(engine, expire_on_commit=False) as db_b,
    ):
        await _delete(ws, uid, ids["L"])(db_a)
        pid_a = await _backend_pid(db_a)
        # A 未提交、仍持有部门树锁时，B 新建顶级部门不应等待。
        await asyncio.wait_for(
            _create(ws, uid, "T", None)(db_b),
            timeout=_LOCK_WAIT_WINDOW_SECONDS,
        )
        await db_b.commit()
        async with engine.connect() as observer:
            params = {"pid": pid_a, "workspace_id": ws}
            a_holds_lock = (await observer.execute(_HOLDS_LOCK_SQL, params)).scalar_one()
        await db_a.commit()

    assert a_holds_lock
    assert await _snapshot(engine, ws) == {"L": (None, True), "T": (None, False)}


async def test_assign_member_waits_for_pending_delete(tenant, member_uid) -> None:
    engine, ws, uid = tenant
    ids = await _seed_departments(engine, ws, uid, "K", "L")
    await _commit(engine, _assign(ws, uid, member_uid, ids["K"]))
    # A 删除 L 未提交时，B 把 M 分配到 L：B 等锁，A 提交后 B 看到 L 已删除，M 仍在 K。
    waited, error = await _race(
        engine,
        ws,
        holder=_delete(ws, uid, ids["L"]),
        waiter=_assign(ws, uid, member_uid, ids["L"]),
    )
    assert {
        "b_waited_on_lock": waited,
        "b_error": error,
        "member_department": await _member_department(engine, ws, member_uid),
        "departments": await _snapshot(engine, ws),
    } == {
        "b_waited_on_lock": True,
        "b_error": _NOT_FOUND,
        "member_department": ids["K"],
        "departments": {"K": (None, False), "L": (None, True)},
    }


async def test_delete_waits_for_pending_member_assign(tenant, member_uid) -> None:
    engine, ws, uid = tenant
    ids = await _seed_departments(engine, ws, uid, "L")
    # B 把 M 分配到 L 未提交时，A 删除 L：A 等锁，B 提交后 A 看到 L 已有成员。
    waited, error = await _race(
        engine,
        ws,
        holder=_assign(ws, uid, member_uid, ids["L"]),
        waiter=_delete(ws, uid, ids["L"]),
    )
    assert {
        "a_waited_on_lock": waited,
        "a_error": error,
        "member_department": await _member_department(engine, ws, member_uid),
        "departments": await _snapshot(engine, ws),
    } == {
        "a_waited_on_lock": True,
        "a_error": _HAS_MEMBERS,
        "member_department": ids["L"],
        "departments": {"L": (None, False)},
    }


async def test_unassign_member_does_not_wait_for_tree_lock(tenant, member_uid) -> None:
    engine, ws, uid = tenant
    ids = await _seed_departments(engine, ws, uid, "K", "L")
    await _commit(engine, _assign(ws, uid, member_uid, ids["K"]))
    async with (
        AsyncSession(engine, expire_on_commit=False) as db_a,
        AsyncSession(engine, expire_on_commit=False) as db_b,
    ):
        await _delete(ws, uid, ids["L"])(db_a)
        pid_a = await _backend_pid(db_a)
        # A 未提交、仍持有部门树锁时，B 把 M 移出部门不应等待。
        await asyncio.wait_for(
            _assign(ws, uid, member_uid, None)(db_b),
            timeout=_LOCK_WAIT_WINDOW_SECONDS,
        )
        await db_b.commit()
        async with engine.connect() as observer:
            params = {"pid": pid_a, "workspace_id": ws}
            a_holds_lock = (await observer.execute(_HOLDS_LOCK_SQL, params)).scalar_one()
        await db_a.commit()

    assert a_holds_lock
    assert await _member_department(engine, ws, member_uid) is None
    assert await _snapshot(engine, ws) == {"K": (None, False), "L": (None, True)}


async def test_assign_member_without_concurrency(tenant, member_uid) -> None:
    engine, ws, uid = tenant
    ids = await _seed_departments(engine, ws, uid, "K", "L")
    first = await _commit(engine, _assign(ws, uid, member_uid, ids["K"], "工程师"))
    second = await _commit(engine, _assign(ws, uid, member_uid, ids["L"], "组长"))
    assert (first.department_id, first.job_title) == (ids["K"], "工程师")
    assert (second.department_id, second.job_title) == (ids["L"], "组长")
    assert await _member_department(engine, ws, member_uid) == ids["L"]
