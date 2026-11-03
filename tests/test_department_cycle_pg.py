"""部门树防环的真实 PostgreSQL 集成测试：并发反向移动、已有环时的读取终止和无环行为。

并发用例需要两个独立连接真实提交，才能复现 READ COMMITTED 下互相看不到未提交改动的竞态，
测试数据在 fixture 收尾时按工作区物理删除；其余用例包在外层事务里，结束时回滚，不留数据。
"""

import asyncio
import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime

import pytest
from sqlalchemy import delete, select, text, update
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

_DESCENDANT_ERROR = "不能将部门移动到其子孙部门下"
# 判定“仍在等锁”的观察窗口：修复前 B 不等锁，会在窗口内直接结束。
_LOCK_WAIT_WINDOW_SECONDS = 2.0

# 独立于被测 CTE 的成环检测：从每个部门沿 parent_id 向上走，回到起点即在环上；
# 包含软删除行，并用 path 数组截断，自身不会因环而无限递归。
_CYCLE_SQL = text(
    """
    WITH RECURSIVE walk(start_id, parent_id, path) AS (
        SELECT id, parent_id, ARRAY[id]
        FROM departments
        WHERE workspace_id = :workspace_id
        UNION ALL
        SELECT walk.start_id, d.parent_id, walk.path || d.id
        FROM walk
        JOIN departments AS d ON d.id = walk.parent_id
        WHERE NOT d.id = ANY(walk.path)
    )
    SELECT DISTINCT start_id FROM walk WHERE parent_id = start_id ORDER BY start_id
    """
)

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


async def _seed_tenant(db: AsyncSession, label: str) -> tuple[int, int]:
    """建一个工作区和已加入的 owner，满足 _require_manager_or_roles 的权限要求。"""
    suffix = uuid.uuid4().hex[:12]
    user = UserRecord(
        email=f"dept-{label}-{suffix}@test.local",
        username=f"dept-{label}-{suffix}",
        password="x",
    )
    workspace = WorkspaceRecord(
        name=f"dept-{label}-{suffix}",
        slug=f"dept-{label}-{suffix}",
        display_name="部门防环测试",
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
async def committed() -> AsyncIterator[tuple[AsyncEngine, list[tuple[int, int]]]]:
    """两组已提交的工作区与 owner；用例结束后按工作区物理删除全部测试数据。"""
    engine = create_async_engine(settings.database_url, poolclass=NullPool)
    try:
        async with engine.connect() as probe:
            await probe.execute(text("SELECT 1"))
    except Exception as exc:  # noqa: BLE001  任何连接错误都只用于决定跳过
        await engine.dispose()
        pytest.skip(f"PostgreSQL 不可达(AGENTOS_DATABASE_URL): {type(exc).__name__}")
    tenants: list[tuple[int, int]] = []
    try:
        async with AsyncSession(engine, expire_on_commit=False) as db:
            for label in ("w1", "w2"):
                tenants.append(await _seed_tenant(db, label))
            await db.commit()
        yield engine, tenants
    finally:
        # 并发用例真实提交，无法靠回滚清理；测试数据直接物理删除，不走软删除。
        workspace_ids = [workspace_id for workspace_id, _ in tenants]
        user_ids = [user_id for _, user_id in tenants]
        async with engine.begin() as conn:
            await conn.execute(
                delete(DepartmentRecord).where(
                    DepartmentRecord.workspace_id.in_(workspace_ids),
                )
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
async def rolled() -> AsyncIterator[tuple[AsyncSession, int, int]]:
    """包在外层事务里的会话，结束时整体回滚，不落库。"""
    engine = create_async_engine(settings.database_url, poolclass=NullPool)
    try:
        conn = await engine.connect()
    except Exception as exc:  # noqa: BLE001  任何连接错误都只用于决定跳过
        await engine.dispose()
        pytest.skip(f"PostgreSQL 不可达(AGENTOS_DATABASE_URL): {type(exc).__name__}")
    transaction = await conn.begin()
    db = AsyncSession(bind=conn, expire_on_commit=False)
    try:
        workspace_id, user_id = await _seed_tenant(db, "rb")
        yield db, workspace_id, user_id
    finally:
        await db.close()
        await transaction.rollback()
        await conn.close()
        await engine.dispose()


async def _seed_pair(engine: AsyncEngine, workspace_id: int, user_id: int) -> tuple[int, int]:
    """提交两个顶级部门 X、Y，作为反向移动的起点。"""
    async with AsyncSession(engine, expire_on_commit=False) as db:
        service = DepartmentService(db)
        x = await service.create_department(workspace_id, user_id, "X")
        y = await service.create_department(workspace_id, user_id, "Y")
        await db.commit()
        return x.id, y.id


async def _move(
    db: AsyncSession,
    workspace_id: int,
    user_id: int,
    department_id: int,
    parent_id: int | None,
) -> DepartmentRecord:
    return await DepartmentService(db).update_department(
        workspace_id,
        department_id,
        user_id,
        {"parent_id": parent_id},
    )


async def _backend_pid(db: AsyncSession) -> int:
    conn = await db.connection()
    return (await conn.execute(text("SELECT pg_backend_pid()"))).scalar_one()


async def _parents(engine: AsyncEngine, workspace_id: int) -> dict[int, int | None]:
    async with engine.connect() as conn:
        rows = await conn.execute(
            select(DepartmentRecord.id, DepartmentRecord.parent_id).where(
                DepartmentRecord.workspace_id == workspace_id,
            )
        )
        return {row.id: row.parent_id for row in rows}


async def _cycle_ids(engine: AsyncEngine, workspace_id: int) -> list[int]:
    async with engine.connect() as conn:
        rows = await conn.execute(_CYCLE_SQL, {"workspace_id": workspace_id})
        return list(rows.scalars())


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


def _ids(records: list[DepartmentRecord]) -> list[int]:
    return [record.id for record in records]


async def test_concurrent_reverse_moves_are_serialized(committed) -> None:
    engine, tenants = committed
    workspace_id, user_id = tenants[0]
    x_id, y_id = await _seed_pair(engine, workspace_id, user_id)

    async with (
        AsyncSession(engine, expire_on_commit=False) as db_a,
        AsyncSession(engine, expire_on_commit=False) as db_b,
    ):
        # A：X 移到 Y 下，flush 但不提交，事务保持打开。
        await _move(db_a, workspace_id, user_id, x_id, y_id)
        pid_a = await _backend_pid(db_a)
        pid_b = await _backend_pid(db_b)
        # B：Y 移到 X 下，应阻塞在 A 持有的同一工作区咨询锁上。
        task_b = asyncio.create_task(_move(db_b, workspace_id, user_id, y_id, x_id))
        b_waited = await _waits_on_lock(
            engine,
            task_b,
            waiter_pid=pid_b,
            holder_pid=pid_a,
            workspace_id=workspace_id,
        )
        await db_a.commit()
        b_error: str | None = None
        try:
            await task_b
            await db_b.commit()
        except AgentException as exc:
            b_error = exc.message
            await db_b.rollback()

    outcome = {
        "b_waited_on_lock": b_waited,
        "b_error": b_error,
        "parents": await _parents(engine, workspace_id),
        "cycle_ids": await _cycle_ids(engine, workspace_id),
    }
    assert outcome == {
        "b_waited_on_lock": True,
        "b_error": _DESCENDANT_ERROR,
        "parents": {x_id: y_id, y_id: None},
        "cycle_ids": [],
    }


async def test_tree_lock_does_not_block_other_workspace(committed) -> None:
    engine, tenants = committed
    (ws1, user1), (ws2, user2) = tenants
    x1, y1 = await _seed_pair(engine, ws1, user1)
    x2, y2 = await _seed_pair(engine, ws2, user2)

    async with (
        AsyncSession(engine, expire_on_commit=False) as db_a,
        AsyncSession(engine, expire_on_commit=False) as db_c,
    ):
        await _move(db_a, ws1, user1, x1, y1)
        pid_a = await _backend_pid(db_a)
        # A 未提交、仍持有工作区 1 的锁时，工作区 2 的移动不应等待。
        await asyncio.wait_for(
            _move(db_c, ws2, user2, y2, x2),
            timeout=_LOCK_WAIT_WINDOW_SECONDS,
        )
        await db_c.commit()
        async with engine.connect() as observer:
            params = {"pid": pid_a, "workspace_id": ws1}
            a_holds_lock = (await observer.execute(_HOLDS_LOCK_SQL, params)).scalar_one()
        await db_a.commit()

    assert a_holds_lock
    assert await _parents(engine, ws1) == {x1: y1, y1: None}
    assert await _parents(engine, ws2) == {x2: None, y2: x2}


async def test_reads_terminate_on_existing_cycle(rolled) -> None:
    db, ws, uid = rolled
    service = DepartmentService(db)
    a = await service.create_department(ws, uid, "A")
    b = await service.create_department(ws, uid, "B", parent_id=a.id)
    c = await service.create_department(ws, uid, "C", parent_id=b.id)
    d = await service.create_department(ws, uid, "D", parent_id=c.id)
    # 模拟修复前竞态留下的历史环 A -> C -> B -> A（D 挂在环外）。Service 会拒绝成环，
    # 这是本文件唯一绕过 Service 直接写库的地方。
    await db.execute(
        update(DepartmentRecord).where(DepartmentRecord.id == a.id).values(parent_id=c.id)
    )
    # 修复前递归 CTE 在环上无限展开，超时让用例快速失败而不是一直挂着。
    conn = await db.connection()
    await conn.execute(text("SET LOCAL statement_timeout = '3s'"))

    ancestors = {}
    descendants = {}
    for department in (a, b, c, d):
        ancestors[department.name] = _ids(await service.repo.list_ancestors(ws, department.id))
        descendants[department.name] = _ids(
            await service.repo.list_descendants(ws, department.id)
        )

    # 遇到已访问的部门即截断，每个部门在结果中只出现一次，且不包含起点自身。
    assert ancestors == {
        "A": [c.id, b.id],
        "B": [a.id, c.id],
        "C": [b.id, a.id],
        "D": [c.id, b.id, a.id],
    }
    assert descendants == {
        "A": [b.id, c.id, d.id],
        "B": [c.id, a.id, d.id],
        "C": [a.id, d.id, b.id],
        "D": [],
    }
    # 会话可见性判定依赖的范围解析同样能返回。
    assert await service.get_department_scope_ids(ws, d.id) == [d.id, c.id, b.id, a.id]


async def test_acyclic_moves_keep_previous_behavior(rolled) -> None:
    db, ws, uid = rolled
    service = DepartmentService(db)
    root = await service.create_department(ws, uid, "root")
    a = await service.create_department(ws, uid, "a", parent_id=root.id, sort_order=1)
    b = await service.create_department(ws, uid, "b", parent_id=root.id, sort_order=2)
    a1 = await service.create_department(ws, uid, "a1", parent_id=a.id)

    moved = await _move(db, ws, uid, b.id, a.id)
    assert moved.parent_id == a.id
    assert _ids(await service.repo.list_descendants(ws, root.id)) == [a.id, a1.id, b.id]
    assert _ids(await service.repo.list_ancestors(ws, b.id)) == [a.id, root.id]

    moved = await _move(db, ws, uid, a1.id, None)
    assert moved.parent_id is None
    assert _ids(await service.repo.list_ancestors(ws, a1.id)) == []
    assert _ids(await service.repo.list_descendants(ws, root.id)) == [a.id, b.id]

    with pytest.raises(AgentException) as rejected:
        await _move(db, ws, uid, root.id, b.id)
    assert rejected.value.message == _DESCENDANT_ERROR
    assert rejected.value.status_code == AgentException.status_code
    assert (await service.get_required(ws, root.id)).parent_id is None
    assert _ids(await service.repo.list_ancestors(ws, b.id)) == [a.id, root.id]
