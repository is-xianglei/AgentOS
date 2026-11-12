"""mcp_server 域的真实 PostgreSQL 集成测试。

大部分用例在一个外层事务里跑、结束时整体回滚（同 test_model_service_pg.py）；
并发创建同一 server_key 需要两个连接各自提交，单独用已提交的数据，用例结束后物理删除。
覆盖：部分唯一索引与软删后重建同一个 key、两个连接并发创建一个成功一个 409、私有 server 对别人 404
且不在别人的列表里、与别人的私有 server 撞 key 时 409 不带 owner、每人私有 server 上限、
McpServerSource 实现只返回已启用的共享 server 加本人的私有 server、permission/api 对 mcp__ 名字的校验。
库不可达时跳过。
"""

import asyncio
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any
from uuid import uuid4

import httpx
import pytest
from sqlalchemy import delete, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, create_async_engine
from sqlalchemy.pool import NullPool

import database.engine  # noqa: F401  副作用注册全部映射与全局软删过滤
from core.config import settings
from core.errors import AgentException
from harness.domain.mcp import (
    McpAuthType,
    McpProbeResult,
    McpToolAnnotations,
    McpToolSummary,
)
from harness.domain.mcp_config import McpServerDraft, plan_update
from harness.domain.tool import ToolScope
from mcp_server.models import McpServerRecord, McpServerSecretRecord
from mcp_server.service import McpServerService
from mcp_server.source import McpServerCatalog
from user.models import UserRecord
from workspace.models import WorkspaceMemberRecord, WorkspaceRecord

pytestmark = pytest.mark.anyio

# 后到者最多等这么久进入锁等待；超时说明它没有被唯一索引挡住。
_LOCK_WAIT_TIMEOUT_SECONDS = 5.0
_IS_WAITING_FOR_LOCK_SQL = text(
    "SELECT wait_event_type = 'Lock' FROM pg_stat_activity WHERE pid = :pid"
)


def _tool(name: str) -> McpToolSummary:
    return McpToolSummary(
        name=name,
        description=f"{name} 的描述",
        input_schema_sha256="d" * 64,
        annotations=McpToolAnnotations(read_only=True),
    )


def _ok(*names: str) -> McpProbeResult:
    return McpProbeResult.success(
        server_name="fake",
        server_version="1",
        protocol_version="2026-07-28",
        tools=[_tool(name) for name in names],
    )


def _draft(key: str, *, private: bool = False, **overrides: Any) -> McpServerDraft:
    values: dict[str, Any] = {
        "server_key": key,
        "name": key,
        "url": f"https://{key.replace('_', '-')}.example.com/mcp",
        "private": private,
        "auth_type": McpAuthType.STATIC_HEADERS,
        "headers": {"Authorization": f"Bearer {key}-secret"},
    }
    values.update(overrides)
    return McpServerDraft.create(**values)


async def _create(
    db: AsyncSession,
    workspace_id: int,
    user_id: int,
    draft: McpServerDraft,
    *tools: str,
) -> int:
    service = McpServerService(db)
    await service.prepare_create(workspace_id, user_id, draft)
    view = await service.create_server(workspace_id, user_id, draft, _ok(*tools))
    return view.server_id


def _members(workspace_id: int, roles: list[tuple[int, str]]) -> list[WorkspaceMemberRecord]:
    now = datetime.now(UTC)
    return [
        WorkspaceMemberRecord(workspace_id=workspace_id, user_id=user_id, role=role, joined_at=now)
        for user_id, role in roles
    ]


@pytest.fixture
async def pg() -> AsyncIterator[SimpleNamespace]:
    """外层事务里的工作区：admin、member、other 三个成员；用例结束整体回滚。"""
    engine = create_async_engine(settings.database_url, poolclass=NullPool)
    try:
        conn = await engine.connect()
    except Exception as exc:  # noqa: BLE001  任何连接错误都只用于决定跳过
        await engine.dispose()
        pytest.skip(f"PostgreSQL 不可达(AGENTOS_DATABASE_URL): {type(exc).__name__}")
    transaction = await conn.begin()
    session = AsyncSession(bind=conn, expire_on_commit=False)
    try:
        if not await session.scalar(text("SELECT to_regclass('mcp_servers')")):
            pytest.skip("需要已升级到 0031_mcp_servers 的数据库")
        suffix = uuid4().hex[:12]
        users = {
            name: UserRecord(
                email=f"mcp-{name}-{suffix}@test.local",
                username=f"mcp-{name}-{suffix}",
                password="x",
            )
            for name in ("admin", "member", "other")
        }
        workspace = WorkspaceRecord(
            name=f"mcp-{suffix}", slug=f"mcp-{suffix}", display_name="MCP 测试"
        )
        session.add_all([*users.values(), workspace])
        await session.flush()
        session.add_all(
            _members(
                workspace.id,
                [
                    (users["admin"].id, "admin"),
                    (users["member"].id, "member"),
                    (users["other"].id, "member"),
                ],
            )
        )
        await session.flush()
        yield SimpleNamespace(
            db=session,
            workspace_id=workspace.id,
            admin=users["admin"].id,
            member=users["member"].id,
            other=users["other"].id,
        )
    finally:
        await session.close()
        await transaction.rollback()
        await conn.close()
        await engine.dispose()


# ---------- 部分唯一索引 ----------


def _raw_server(workspace_id: int, server_key: str, **overrides: Any) -> McpServerRecord:
    values: dict[str, Any] = {
        "workspace_id": workspace_id,
        "server_key": server_key,
        "name": server_key,
        "url": "https://raw.example.com",
        "auth_type": "none",
        "approval_mode": "always",
        "tool_allowlist": [],
        "tool_denylist": [],
        "config_rev": 1,
        "is_enabled": True,
        "tools_snapshot": [],
    }
    values.update(overrides)
    return McpServerRecord(**values)


async def _insert_violates_unique(db: AsyncSession, record: McpServerRecord) -> bool:
    """在保存点里直接插入，绕过 Service 的预检查；撞部分唯一索引时回滚保存点并返回 True。"""
    savepoint = await db.begin_nested()
    db.add(record)
    try:
        await db.flush()
    except IntegrityError:
        await savepoint.rollback()
        return True
    await savepoint.commit()
    return False


async def test_partial_unique_index_allows_recreating_key_after_soft_delete(pg) -> None:
    db = pg.db
    first_id = await _create(db, pg.workspace_id, pg.admin, _draft("github"), "create_issue")

    # 共享和私有 server 共用一个命名空间：私有 server 也撞同一个 key。
    assert await _insert_violates_unique(db, _raw_server(pg.workspace_id, "github"))
    assert await _insert_violates_unique(
        db,
        _raw_server(pg.workspace_id, "github", owner_user_id=pg.member),
    )

    service = McpServerService(db)
    await service.delete_server(pg.workspace_id, pg.admin, first_id)
    second_id = await _create(db, pg.workspace_id, pg.member, _draft("github", private=True))

    assert second_id != first_id
    deleted = await db.scalar(
        select(McpServerRecord)
        .where(McpServerRecord.id == first_id)
        .execution_options(include_deleted=True)
    )
    assert deleted is not None and deleted.is_deleted
    # 凭证表同样按部分唯一索引约束：软删后的旧凭证不挡新行，有效凭证一个 server 只有一行。
    assert await _insert_violates_unique_secret(db, second_id)


async def _insert_violates_unique_secret(db: AsyncSession, server_id: int) -> bool:
    savepoint = await db.begin_nested()
    db.add(McpServerSecretRecord(server_id=server_id, headers={"X-Dup": "1"}))
    try:
        await db.flush()
    except IntegrityError:
        await savepoint.rollback()
        return True
    await savepoint.commit()
    return False


async def test_secret_rows_follow_soft_delete_and_jsonb_round_trip(pg) -> None:
    db = pg.db
    server_id = await _create(db, pg.workspace_id, pg.admin, _draft("github"), "a")
    service = McpServerService(db)
    current = await service.load_for_write(pg.workspace_id, pg.admin, server_id)
    update = plan_update(current, {"headers": {"Authorization": "********", "X-Team": "t1"}})
    view = await service.apply_update(pg.workspace_id, pg.admin, server_id, update, _ok("a"))

    rows = list(
        await db.scalars(
            select(McpServerSecretRecord)
            .where(McpServerSecretRecord.server_id == server_id)
            .execution_options(include_deleted=True)
            .order_by(McpServerSecretRecord.id)
        )
    )
    assert [row.is_deleted for row in rows] == [True, False]
    await db.refresh(rows[1])
    assert rows[1].headers == {"Authorization": "Bearer github-secret", "X-Team": "t1"}
    assert view.header_names == ("Authorization", "X-Team")
    assert view.config.config_rev == 2


# ---------- 私有 server 的可见性与撞 key ----------


async def test_private_server_is_404_and_hidden_for_others(pg) -> None:
    db = pg.db
    private_id = await _create(db, pg.workspace_id, pg.member, _draft("mine", private=True), "x")
    shared_id = await _create(db, pg.workspace_id, pg.admin, _draft("shared"), "y")
    service = McpServerService(db)

    for user_id in (pg.admin, pg.other):
        listed = await service.list_servers(pg.workspace_id, user_id)
        assert [view.server_id for view in listed] == [shared_id]
        for call in (
            service.get_server(pg.workspace_id, user_id, private_id),
            service.load_for_write(pg.workspace_id, user_id, private_id),
            service.delete_server(pg.workspace_id, user_id, private_id),
        ):
            with pytest.raises(AgentException) as exc_info:
                await call
            assert exc_info.value.status_code == 404
            assert exc_info.value.message == "MCP server 不存在"

    own = await service.list_servers(pg.workspace_id, pg.member)
    assert [view.server_id for view in own] == [shared_id, private_id]
    assert [view.writable for view in own] == [False, True]


async def test_key_clash_with_someone_elses_private_server_is_409_without_owner(pg) -> None:
    db = pg.db
    await _create(db, pg.workspace_id, pg.other, _draft("secret_key", private=True))
    service = McpServerService(db)

    for user_id, private in ((pg.admin, False), (pg.member, True)):
        with pytest.raises(AgentException) as exc_info:
            await service.prepare_create(
                pg.workspace_id,
                user_id,
                _draft("secret_key", private=private),
            )
        assert exc_info.value.status_code == 409
        assert exc_info.value.message == "server_key 已被占用"
        assert exc_info.value.details == {"server_key": "secret_key"}
        assert str(pg.other) not in str(exc_info.value.details)


async def test_private_server_quota_per_user(pg, monkeypatch) -> None:
    db = pg.db
    monkeypatch.setattr(settings, "mcp_max_private_servers_per_user", 2)
    await _create(db, pg.workspace_id, pg.member, _draft("p_one", private=True))
    await _create(db, pg.workspace_id, pg.member, _draft("p_two", private=True))
    # 共享 server 与别人的私有 server 不占名额；软删的私有 server 也不占。
    await _create(db, pg.workspace_id, pg.admin, _draft("shared"))
    await _create(db, pg.workspace_id, pg.other, _draft("o_one", private=True))
    service = McpServerService(db)

    with pytest.raises(AgentException) as exc_info:
        await service.prepare_create(pg.workspace_id, pg.member, _draft("p_three", private=True))
    assert exc_info.value.status_code == 409
    assert exc_info.value.details == {"limit": 2}

    (first,) = [
        view
        for view in await service.list_servers(pg.workspace_id, pg.member)
        if view.config.server_key == "p_one"
    ]
    await service.delete_server(pg.workspace_id, pg.member, first.server_id)
    await _create(db, pg.workspace_id, pg.member, _draft("p_three", private=True))


async def test_member_cannot_write_shared_server(pg) -> None:
    db = pg.db
    shared_id = await _create(db, pg.workspace_id, pg.admin, _draft("shared"))
    service = McpServerService(db)

    with pytest.raises(AgentException) as create_error:
        await service.prepare_create(pg.workspace_id, pg.member, _draft("another"))
    with pytest.raises(AgentException) as write_error:
        await service.load_for_write(pg.workspace_id, pg.member, shared_id)

    assert create_error.value.status_code == 403
    assert write_error.value.status_code == 403


# ---------- McpServerSource 的生产实现 ----------


async def test_catalog_lists_enabled_shared_and_own_private_with_secrets(pg) -> None:
    db = pg.db
    await _create(db, pg.workspace_id, pg.admin, _draft("shared_on"))
    await _create(db, pg.workspace_id, pg.admin, _draft("shared_off", is_enabled=False))
    await _create(db, pg.workspace_id, pg.admin, _draft("no_auth", auth_type=McpAuthType.NONE,
                                                         headers=None))
    await _create(db, pg.workspace_id, pg.member, _draft("mine", private=True))
    await _create(db, pg.workspace_id, pg.member, _draft("mine_off", private=True,
                                                          is_enabled=False))
    await _create(db, pg.workspace_id, pg.other, _draft("theirs", private=True))
    opened: list[AsyncSession] = []

    def session_factory() -> AsyncSession:
        # 与测试共用外层事务的连接：短会话读得到未提交的测试数据，关闭时不影响外层事务。
        session = AsyncSession(bind=db.bind, expire_on_commit=False)
        opened.append(session)
        return session

    catalog = McpServerCatalog(session_factory)
    entries = await catalog.list_servers(
        ToolScope(workspace_id=pg.workspace_id, user_id=pg.member),
    )

    by_key = {entry.config.server_key: entry for entry in entries}
    assert set(by_key) == {"shared_on", "no_auth", "mine"}
    assert by_key["mine"].config.owner_user_id == pg.member
    assert all(entry.config.server_id is not None for entry in entries)
    assert by_key["no_auth"].secret is None
    secret = by_key["mine"].secret
    assert secret is not None and secret.as_dict() == {"Authorization": "Bearer mine-secret"}
    assert "mine-secret" not in repr(by_key["mine"])
    # 每次调用开一个短会话，读完即关。
    assert len(opened) == 1
    other = await catalog.list_servers(ToolScope(workspace_id=pg.workspace_id, user_id=pg.other))
    assert {entry.config.server_key for entry in other} == {"shared_on", "no_auth", "theirs"}
    assert len(opened) == 2


# ---------- permission/api 对 mcp__ 名字的校验 ----------


async def test_permission_api_validates_mcp_tool_names(pg) -> None:
    from api.deps import get_current_active_user, get_current_workspace_id, get_db
    from main import create_app

    db = pg.db
    await _create(db, pg.workspace_id, pg.admin, _draft("github"), "create_issue", "a.b")
    await _create(db, pg.workspace_id, pg.admin, _draft("off", is_enabled=False), "t")
    await _create(db, pg.workspace_id, pg.member, _draft("mine", private=True), "secret_tool")
    # 快照为空的 OAuth server 按 server 前缀匹配；OAuth 的授权与探测在 M3，这里直接造行。
    db.add(_raw_server(pg.workspace_id, "jira", auth_type="oauth"))
    await db.flush()
    admin = await db.get(UserRecord, pg.admin)

    async def database() -> AsyncIterator[AsyncSession]:
        yield db

    app = create_app()
    app.dependency_overrides.update({
        get_db: database,
        get_current_active_user: lambda: admin,
        get_current_workspace_id: lambda: pg.workspace_id,
    })
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:

        async def upsert(tool_name: str) -> httpx.Response:
            return await client.post(
                "/api/permissions",
                json={"scope": "global", "tool_name": tool_name, "behavior": "allow"},
            )

        accepted = [
            await upsert("mcp__github__create_issue"),
            await upsert(f"mcp__github__a_b_{_sha1_8('a.b')}"),
            await upsert("mcp__off__t"),
            await upsert("mcp__jira__anything"),
            await upsert("Bash"),
        ]
        rejected = [
            await upsert("mcp__github__missing"),
            await upsert("mcp__mine__secret_tool"),
            await upsert("mcp__nope__x"),
            await upsert("mcp__"),
            await upsert("NoSuchTool"),
        ]

    for response in accepted:
        assert response.status_code == 200, response.text
    for response in rejected:
        assert response.status_code == 400, response.text
        # 私有 server 的工具与不存在的工具报同一句话，不泄露它的存在。
        assert response.json()["error"]["message"] == "工具未注册，不能配置权限规则"


def _sha1_8(name: str) -> str:
    import hashlib

    return hashlib.sha1(name.encode("utf-8")).hexdigest()[:8]


# ---------- 两个连接并发创建同一个 server_key ----------


async def _wait_until_blocked(engine: AsyncEngine, pid: int) -> None:
    """轮询 pg_stat_activity，直到指定连接进入锁等待（被未提交的同 key 索引项挡住）。"""
    deadline = asyncio.get_running_loop().time() + _LOCK_WAIT_TIMEOUT_SECONDS
    async with engine.connect() as probe:
        while asyncio.get_running_loop().time() < deadline:
            if await probe.scalar(_IS_WAITING_FOR_LOCK_SQL, {"pid": pid}):
                return
            await asyncio.sleep(0.05)
    raise AssertionError("后到的请求没有进入锁等待")


@pytest.fixture
async def committed() -> AsyncIterator[SimpleNamespace]:
    """已提交的工作区与 owner；用例结束后物理删除（server 随工作区级联删除）。"""
    engine = create_async_engine(settings.database_url, poolclass=NullPool)
    try:
        async with engine.connect() as probe:
            if not await probe.scalar(text("SELECT to_regclass('mcp_servers')")):
                await engine.dispose()
                pytest.skip("需要已升级到 0031_mcp_servers 的数据库")
    except Exception as exc:  # noqa: BLE001  任何连接错误都只用于决定跳过
        await engine.dispose()
        pytest.skip(f"PostgreSQL 不可达(AGENTOS_DATABASE_URL): {type(exc).__name__}")
    suffix = uuid4().hex[:12]
    user_ids: list[int] = []
    workspace_ids: list[int] = []
    try:
        async with AsyncSession(engine, expire_on_commit=False) as db:
            owner = UserRecord(
                email=f"mcpc-{suffix}@test.local", username=f"mcpc-{suffix}", password="x"
            )
            workspace = WorkspaceRecord(
                name=f"mcpc-{suffix}", slug=f"mcpc-{suffix}", display_name="MCP 并发测试"
            )
            db.add_all([owner, workspace])
            await db.flush()
            user_ids.append(owner.id)
            workspace_ids.append(workspace.id)
            db.add_all(_members(workspace.id, [(owner.id, "owner")]))
            await db.commit()
        yield SimpleNamespace(engine=engine, workspace_id=workspace.id, owner=owner.id)
    finally:
        async with engine.begin() as conn:
            await conn.execute(delete(WorkspaceRecord).where(WorkspaceRecord.id.in_(workspace_ids)))
            await conn.execute(delete(UserRecord).where(UserRecord.id.in_(user_ids)))
        await engine.dispose()


async def test_concurrent_create_same_key_one_succeeds_one_409(committed) -> None:
    env = committed
    draft = _draft("race")
    first = AsyncSession(env.engine, expire_on_commit=False)
    second = AsyncSession(env.engine, expire_on_commit=False)
    try:
        # 两个请求都通过了重名预检查（都还没写库），然后各自探测成功、写库。
        await McpServerService(first).prepare_create(env.workspace_id, env.owner, draft)
        await McpServerService(second).prepare_create(env.workspace_id, env.owner, draft)

        # 先到者插入但不提交；后到者的插入被同 key 的未提交索引项挡住，进入锁等待。
        await McpServerService(first).create_server(env.workspace_id, env.owner, draft, _ok("a"))
        second_pid = await second.scalar(text("SELECT pg_backend_pid()"))
        second_task = asyncio.create_task(
            McpServerService(second).create_server(env.workspace_id, env.owner, draft, _ok("a")),
        )
        await _wait_until_blocked(env.engine, second_pid)
        await first.commit()

        # 唯一索引冲突转成 409 业务错误，不会变成 500，也不透露是谁占用。
        with pytest.raises(AgentException) as exc_info:
            await second_task
        assert exc_info.value.status_code == 409
        assert exc_info.value.message == "server_key 已被占用"
        assert isinstance(exc_info.value.__cause__, IntegrityError)
        await second.rollback()
    finally:
        await first.close()
        await second.close()

    async with AsyncSession(env.engine) as check:
        rows = list(
            await check.scalars(
                select(McpServerRecord).where(McpServerRecord.workspace_id == env.workspace_id),
            )
        )
        secrets = list(
            await check.scalars(
                select(McpServerSecretRecord).where(
                    McpServerSecretRecord.server_id.in_([row.id for row in rows]),
                ),
            )
        )
    assert [row.server_key for row in rows] == ["race"]
    assert len(secrets) == 1
