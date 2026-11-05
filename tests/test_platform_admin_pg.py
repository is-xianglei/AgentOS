"""平台首个注册用户成为平台管理员的真实 PostgreSQL 集成测试。

开发库里已经有用户，没法直接验证「users 表为空」的分支。所以在外层事务里新建一个临时
schema，把 users、workspaces、workspace_members 建在里面，并用 SET LOCAL search_path
把它放在 public 前面：ORM 的未限定表名会落到临时表，其余表仍解析到 public。
建表、注册都在外层事务里，结束时整体回滚，不留数据。库不可达时跳过并给出原因。
"""

import uuid
from types import SimpleNamespace

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.pool import NullPool

import database.engine  # noqa: F401  副作用注册全部映射与全局软删过滤
from auth.service import AuthService
from core.config import settings
from database.base import Base
from user.models import UserRecord
from user.repository import FIRST_USER_LOCK_ID, FIRST_USER_LOCK_NAMESPACE
from user.service import UserService
from workspace.models import WorkspaceMemberRecord, WorkspaceRecord

pytestmark = pytest.mark.anyio


@pytest.fixture
async def empty_users_db():
    engine = create_async_engine(settings.database_url, poolclass=NullPool)
    try:
        conn = await engine.connect()
    except Exception as exc:  # noqa: BLE001  任何连接错误都只用于决定跳过
        await engine.dispose()
        pytest.skip(f"PostgreSQL 不可达(AGENTOS_DATABASE_URL): {type(exc).__name__}")
    transaction = await conn.begin()
    schema = f"test_platform_admin_{uuid.uuid4().hex[:12]}"
    await conn.execute(text(f'CREATE SCHEMA "{schema}"'))
    await conn.execute(text(f'SET LOCAL search_path TO "{schema}", public'))
    # checkfirst 会把 public 里同名的表当成已存在而跳过，这里必须关掉。
    await conn.run_sync(
        Base.metadata.create_all,
        tables=[UserRecord.__table__, WorkspaceRecord.__table__, WorkspaceMemberRecord.__table__],
        checkfirst=False,
    )
    session = AsyncSession(bind=conn, expire_on_commit=False)
    try:
        yield SimpleNamespace(db=session)
    finally:
        await session.close()
        await transaction.rollback()
        await conn.close()
        await engine.dispose()


async def _register(db: AsyncSession, name: str) -> UserRecord:
    result = await AuthService(db).register(
        email=f"{name}@test.local",
        username=name,
        password="secret123",
    )
    return result["user"]


async def test_first_registered_user_becomes_platform_admin(empty_users_db) -> None:
    first = await _register(empty_users_db.db, "first-user")
    second = await _register(empty_users_db.db, "second-user")

    assert first.is_platform_admin is True
    assert second.is_platform_admin is False


async def test_soft_deleted_user_still_counts_as_existing(empty_users_db) -> None:
    """表里只剩软删除的用户时，新注册的人也不是首个用户，平台管理员不会因删号转给后来者。"""
    deleted = UserRecord(
        email="deleted-user@test.local",
        username="deleted-user",
        password="x",
        is_deleted=True,
    )
    empty_users_db.db.add(deleted)
    await empty_users_db.db.flush()

    newcomer = await _register(empty_users_db.db, "newcomer")

    assert newcomer.is_platform_admin is False


async def test_first_user_check_holds_advisory_lock(empty_users_db) -> None:
    """判定会先取固定键的事务级咨询锁，并发注册因此只能逐个判定。"""
    is_first = await UserService(empty_users_db.db).lock_and_check_first_user()

    held = await empty_users_db.db.scalar(
        text(
            "SELECT count(*) FROM pg_locks"
            " WHERE locktype = 'advisory' AND pid = pg_backend_pid()"
            " AND classid::bigint = :namespace AND objid::bigint = :lock_id AND objsubid = 2"
        ),
        {"namespace": FIRST_USER_LOCK_NAMESPACE, "lock_id": FIRST_USER_LOCK_ID},
    )
    assert is_first is True
    assert held == 1
