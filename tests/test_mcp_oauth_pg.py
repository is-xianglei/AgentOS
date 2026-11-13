"""MCP OAuth 的真实 PostgreSQL 集成测试（库可达且已升到 0034 时执行，否则跳过）。

覆盖：
- 两个连接同时刷新同一个 token，只发出一次刷新请求（后到者等行锁，锁内读到新 token 直接用）；
- lock_timeout 生效：行锁被别的事务占着时，5 秒内放弃并判为需要重新授权，不无限等待；
- 部分唯一索引：(server, 用户) 只有一份有效 token，撤销（软删）后可以重新授权；客户端注册同理；
- 私有 server 的 OAuth 接口对别人一律 404；授权完成与撤销经真实 HTTP 接口与 McpServerService 写库；
- 列表附带当前用户的授权状态、不含 token；快照回写只在清单变化且配置版本未变时写入。
已提交的数据（并发与锁超时需要两个连接各自提交）在用例结束后物理删除。
"""

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any
from uuid import uuid4

import httpx
import pytest
from sqlalchemy import delete, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

import database.engine  # noqa: F401  副作用注册全部映射与全局软删过滤
from core.config import settings
from core.errors import AgentException
from harness.domain.mcp import McpToolAnnotations, McpToolSummary
from harness.domain.mcp_oauth import McpOAuthGrant, McpOAuthToken
from mcp_server.models import McpOAuthTokenRecord, McpServerRecord
from mcp_server.service import McpServerService, McpTokenLockTimeout
from mcp_server.source import McpOAuthCatalog
from user.models import UserRecord
from workspace.models import WorkspaceMemberRecord, WorkspaceRecord

pytestmark = pytest.mark.anyio

_LOCK_WAIT_TIMEOUT_SECONDS = 5.0
_IS_WAITING_FOR_LOCK_SQL = text(
    "SELECT wait_event_type = 'Lock' FROM pg_stat_activity WHERE pid = :pid"
)


def _token(access: str, *, refresh: str | None = "rt-1", minutes: int = 60) -> McpOAuthToken:
    return McpOAuthToken(
        access_token=access,
        refresh_token=refresh,
        scope="tools.read",
        expires_at=datetime.now(UTC) + timedelta(minutes=minutes),
        issuer="https://auth.example.com",
    )


def _server(workspace_id: int, server_key: str, **overrides: Any) -> McpServerRecord:
    values: dict[str, Any] = {
        "workspace_id": workspace_id,
        "server_key": server_key,
        "name": server_key,
        "url": f"https://{server_key}.example.com/mcp",
        "auth_type": "oauth",
        "approval_mode": "always",
        "tool_allowlist": [],
        "tool_denylist": [],
        "config_rev": 1,
        "is_enabled": True,
        "tools_snapshot": [],
    }
    values.update(overrides)
    return McpServerRecord(**values)


@pytest.fixture
async def env() -> AsyncIterator[SimpleNamespace]:
    """已提交的工作区（admin、member、other）与两个 OAuth server（共享、member 的私有）；结束后物理删除。"""
    engine = create_async_engine(settings.database_url, poolclass=NullPool)
    try:
        async with engine.connect() as probe:
            if not await probe.scalar(text("SELECT to_regclass('mcp_oauth_tokens')")):
                await engine.dispose()
                pytest.skip("需要已升级到 0034_mcp_oauth 的数据库")
    except Exception as exc:  # noqa: BLE001  任何连接错误都只用于决定跳过
        await engine.dispose()
        pytest.skip(f"PostgreSQL 不可达(AGENTOS_DATABASE_URL): {type(exc).__name__}")
    factory = async_sessionmaker(engine, expire_on_commit=False)
    suffix = uuid4().hex[:12]
    user_ids: list[int] = []
    workspace_ids: list[int] = []
    try:
        async with factory() as db:
            users = {
                name: UserRecord(
                    email=f"mcpo-{name}-{suffix}@test.local",
                    username=f"mcpo-{name}-{suffix}",
                    password="x",
                )
                for name in ("admin", "member", "other")
            }
            workspace = WorkspaceRecord(
                name=f"mcpo-{suffix}", slug=f"mcpo-{suffix}", display_name="MCP OAuth 测试"
            )
            db.add_all([*users.values(), workspace])
            await db.flush()
            user_ids.extend(user.id for user in users.values())
            workspace_ids.append(workspace.id)
            now = datetime.now(UTC)
            db.add_all(
                [
                    WorkspaceMemberRecord(
                        workspace_id=workspace.id, user_id=users[name].id, role=role, joined_at=now
                    )
                    for name, role in (
                        ("admin", "admin"),
                        ("member", "member"),
                        ("other", "member"),
                    )
                ]
            )
            shared = _server(workspace.id, "jira")
            private = _server(workspace.id, "mine", owner_user_id=users["member"].id)
            db.add_all([shared, private])
            await db.commit()
        yield SimpleNamespace(
            engine=engine,
            factory=factory,
            workspace_id=workspace.id,
            admin=users["admin"].id,
            member=users["member"].id,
            other=users["other"].id,
            shared_id=shared.id,
            private_id=private.id,
        )
    finally:
        async with engine.begin() as conn:
            await conn.execute(delete(WorkspaceRecord).where(WorkspaceRecord.id.in_(workspace_ids)))
            await conn.execute(delete(UserRecord).where(UserRecord.id.in_(user_ids)))
        await engine.dispose()


async def _save(env: SimpleNamespace, server_id: int, user_id: int, token: McpOAuthToken) -> None:
    async with env.factory() as db:
        grant = McpOAuthGrant(server_id=server_id, config_rev=1, token=token)
        await McpServerService(db).save_oauth_token(env.workspace_id, user_id, grant)
        await db.commit()


async def _active_tokens(env: SimpleNamespace, server_id: int, user_id: int) -> list[Any]:
    async with env.factory() as db:
        rows = await db.scalars(
            select(McpOAuthTokenRecord)
            .where(
                McpOAuthTokenRecord.server_id == server_id,
                McpOAuthTokenRecord.user_id == user_id,
            )
            .execution_options(include_deleted=True)
            .order_by(McpOAuthTokenRecord.id)
        )
        return list(rows)


# ---------- 并发刷新与锁超时 ----------


async def test_concurrent_refresh_sends_one_request(env) -> None:
    """两个实例（两个连接）同时发现同一个 token 过期：先到者持行锁刷新，后到者等锁后读到新 token 直接用。"""
    await _save(env, env.shared_id, env.admin, _token("at-old", refresh="rt-old"))
    sent: list[str] = []
    first_inside = asyncio.Event()
    release = asyncio.Event()
    pids: dict[str, int] = {}

    def tracking_factory(name: str):
        """记下每个实例短事务所用连接的 backend pid，用来精确判断后到者是否在等锁。"""

        @asynccontextmanager
        async def factory() -> AsyncIterator[AsyncSession]:
            async with env.factory() as db:
                pids[name] = await db.scalar(text("SELECT pg_backend_pid()"))
                yield db

        return factory

    def exchange_for(name: str):
        async def exchange(current: McpOAuthToken) -> McpOAuthToken | None:
            if current.access_token != "at-old":
                return current  # 已被别的实例刷新过
            sent.append(name)
            first_inside.set()
            await release.wait()  # 刷新的 HTTP 往返期间一直持锁
            return _token("at-new", refresh="rt-new")

        return exchange

    first = asyncio.create_task(
        McpOAuthCatalog(tracking_factory("a")).refresh_token_locked(
            env.shared_id,
            env.admin,
            exchange_for("a"),
        ),
    )
    second: asyncio.Task[Any] | None = None
    try:
        await asyncio.wait_for(first_inside.wait(), _LOCK_WAIT_TIMEOUT_SECONDS)
        second = asyncio.create_task(
            McpOAuthCatalog(tracking_factory("b")).refresh_token_locked(
                env.shared_id,
                env.admin,
                exchange_for("b"),
            ),
        )
        await _wait_until_blocked(env, pids, "b")
    finally:
        # 无论断言是否通过都放行先到者，否则它一直持锁，夹具清理会被挡住。
        release.set()
    a, b = await asyncio.gather(first, second)

    assert sent == ["a"]
    assert a is not None and b is not None
    assert a.access_token == b.access_token == "at-new"
    rows = await _active_tokens(env, env.shared_id, env.admin)
    assert [(row.is_deleted, row.token["access_token"]) for row in rows] == [(False, "at-new")]
    assert rows[0].token["refresh_token"] == "rt-new"


async def _wait_until_blocked(env: SimpleNamespace, pids: dict[str, int], name: str) -> None:
    """轮询 pg_stat_activity，直到指定实例的连接进入锁等待。"""
    deadline = asyncio.get_running_loop().time() + _LOCK_WAIT_TIMEOUT_SECONDS
    async with env.engine.connect() as probe:
        while asyncio.get_running_loop().time() < deadline:
            pid = pids.get(name)
            if pid is not None and await probe.scalar(_IS_WAITING_FOR_LOCK_SQL, {"pid": pid}):
                return
            await asyncio.sleep(0.02)
    raise AssertionError("后到的刷新没有进入锁等待")


async def test_lock_timeout_gives_up_instead_of_waiting(env, monkeypatch) -> None:
    """行锁被占着时 SET LOCAL lock_timeout 生效：到期抛 McpTokenLockTimeout，不无限等待。"""
    import mcp_server.service as service_module

    monkeypatch.setattr(service_module, "REFRESH_LOCK_TIMEOUT_SECONDS", 1)
    await _save(env, env.shared_id, env.admin, _token("at-held"))
    async with env.factory() as holder:
        await holder.execute(
            select(McpOAuthTokenRecord)
            .where(
                McpOAuthTokenRecord.server_id == env.shared_id,
                McpOAuthTokenRecord.user_id == env.admin,
            )
            .with_for_update()
        )
        called: list[str] = []

        async def exchange(current: McpOAuthToken) -> McpOAuthToken | None:
            called.append(current.access_token)
            return current

        started = asyncio.get_running_loop().time()
        with pytest.raises(McpTokenLockTimeout):
            await McpOAuthCatalog(env.factory).refresh_token_locked(
                env.shared_id,
                env.admin,
                exchange,
            )
        elapsed = asyncio.get_running_loop().time() - started
        await holder.rollback()
    assert called == []
    assert 0.8 <= elapsed < 4


async def test_lock_timeout_setting_is_transaction_local(env) -> None:
    async with env.factory() as db:
        await McpServerService(db).refresh_token_locked(env.shared_id, env.admin, _never)
        inside = await db.scalar(text("SHOW lock_timeout"))
        await db.commit()
        after = await db.scalar(text("SHOW lock_timeout"))
    assert inside == "5s"
    assert after != "5s"


async def _never(current: McpOAuthToken) -> McpOAuthToken | None:
    raise AssertionError("没有 token 时不应调用刷新回调")


async def test_rejected_refresh_invalidates_inside_the_lock(env) -> None:
    await _save(env, env.shared_id, env.admin, _token("at-x"))

    async def rejected(current: McpOAuthToken) -> McpOAuthToken | None:
        return None

    result = await McpOAuthCatalog(env.factory).refresh_token_locked(
        env.shared_id,
        env.admin,
        rejected,
    )
    rows = await _active_tokens(env, env.shared_id, env.admin)
    assert result is None
    assert [row.is_deleted for row in rows] == [True]


async def test_invalidate_only_touches_the_rejected_token(env) -> None:
    await _save(env, env.shared_id, env.admin, _token("at-current"))
    catalog = McpOAuthCatalog(env.factory)
    assert await catalog.invalidate_token(env.shared_id, env.admin, "at-stale") is False
    assert (await catalog.get_token(env.shared_id, env.admin)) is not None
    assert await catalog.invalidate_token(env.shared_id, env.admin, "at-current") is True
    assert (await catalog.get_token(env.shared_id, env.admin)) is None


# ---------- 部分唯一索引、撤销后重新授权、快照 ----------


async def test_partial_unique_index_and_reauthorize_after_revoke(env) -> None:
    await _save(env, env.shared_id, env.admin, _token("at-1"))
    # 直接插第二份有效 token 撞部分唯一索引。
    async with env.factory() as db:
        db.add(
            McpOAuthTokenRecord(
                server_id=env.shared_id,
                user_id=env.admin,
                token={"access_token": "dup"},
            )
        )
        with pytest.raises(IntegrityError):
            await db.flush()
        await db.rollback()
    # 同一用户再授权：软删旧行再插新行，有效行始终一行；别的用户各自一行。
    await _save(env, env.shared_id, env.admin, _token("at-2"))
    await _save(env, env.shared_id, env.other, _token("at-other"))
    async with env.factory() as db:
        revoked = await McpServerService(db).revoke_oauth_token(
            env.workspace_id,
            env.admin,
            env.shared_id,
        )
        again = await McpServerService(db).revoke_oauth_token(
            env.workspace_id,
            env.admin,
            env.shared_id,
        )
        await db.commit()
    await _save(env, env.shared_id, env.admin, _token("at-3"))

    rows = await _active_tokens(env, env.shared_id, env.admin)
    assert revoked is True and again is False
    assert [(row.is_deleted, row.token["access_token"]) for row in rows] == [
        (True, "at-1"),
        (True, "at-2"),
        (False, "at-3"),
    ]
    other = await _active_tokens(env, env.shared_id, env.other)
    assert [(row.is_deleted, row.token["access_token"]) for row in other] == [(False, "at-other")]


async def test_client_registration_replaced_with_single_active_row(env) -> None:
    from harness.domain.mcp_oauth import McpOAuthClient
    from mcp_server.models import McpOAuthClientRecord

    catalog = McpOAuthCatalog(env.factory)
    for client_id in ("c-1", "c-2"):
        await catalog.save_client(
            env.shared_id,
            McpOAuthClient(
                client_id=client_id,
                issuer="https://auth.example.com",
                info={"client_id": client_id, "client_secret": "s3cret"},
            ),
        )
    current = await catalog.get_client(env.shared_id)
    async with env.factory() as db:
        rows = list(
            await db.scalars(
                select(McpOAuthClientRecord)
                .where(McpOAuthClientRecord.server_id == env.shared_id)
                .execution_options(include_deleted=True)
                .order_by(McpOAuthClientRecord.id)
            )
        )
    assert current is not None and current.client_id == "c-2"
    assert [(row.is_deleted, row.client_info["client_id"]) for row in rows] == [
        (True, "c-1"),
        (False, "c-2"),
    ]


async def test_save_token_rejects_changed_config_and_url_change_drops_tokens(env) -> None:
    await _save(env, env.shared_id, env.other, _token("at-other"))
    async with env.factory() as db:
        with pytest.raises(AgentException) as exc_info:
            await McpServerService(db).save_oauth_token(
                env.workspace_id,
                env.admin,
                McpOAuthGrant(server_id=env.shared_id, config_rev=99, token=_token("x")),
            )
    assert exc_info.value.status_code == 409

    from harness.domain.mcp_config import plan_update

    async with env.factory() as db:
        service = McpServerService(db)
        current = await service.load_for_write(env.workspace_id, env.admin, env.shared_id)
        update = plan_update(current, {"url": "https://jira2.example.com/mcp"})
        from harness.domain.mcp import McpProbeResult

        await service.apply_update(
            env.workspace_id,
            env.admin,
            env.shared_id,
            update,
            McpProbeResult.metadata_only(),
        )
        await db.commit()
    # 地址变了，旧 token 的受众已不是它：全部作废。
    rows = await _active_tokens(env, env.shared_id, env.other)
    assert [row.is_deleted for row in rows] == [True]


async def test_snapshot_written_only_when_changed_and_revision_matches(env) -> None:
    tools = [
        McpToolSummary(
            name="search",
            description="搜索",
            input_schema_sha256="a" * 64,
            annotations=McpToolAnnotations(read_only=True),
        )
    ]
    catalog = McpOAuthCatalog(env.factory)
    first = await catalog.record_tools_snapshot(env.shared_id, 1, tools)
    same = await catalog.record_tools_snapshot(env.shared_id, 1, tools)
    stale = await catalog.record_tools_snapshot(env.shared_id, 7, [])
    async with env.factory() as db:
        record = await db.get(McpServerRecord, env.shared_id)
    assert (first, same, stale) == (True, False, False)
    assert record is not None and [item["name"] for item in record.tools_snapshot] == ["search"]


# ---------- HTTP 接口：私有 server 对别人 404、授权状态、完成与撤销 ----------


class _FakeConnector:
    """只替换网络动作：发起返回固定地址，完成按 state 编码的身份造一份 grant；其余记录调用。"""

    def __init__(self) -> None:
        self.begun: list[tuple[int, int]] = []
        self.forgotten: list[tuple[int, int]] = []
        self.snapshots: list[int] = []

    async def begin_oauth(self, config: Any, *, workspace_id: int, user_id: int) -> str:
        self.begun.append((config.require_id(), user_id))
        return "https://auth.example.com/authorize?state=s"

    async def complete_oauth(
        self,
        callback: Any,
        *,
        workspace_id: int,
        user_id: int,
        config_for: Any,
    ) -> McpOAuthGrant:
        server_id = int(callback.state)
        config = await config_for(server_id)
        return McpOAuthGrant(
            server_id=server_id,
            config_rev=config.config_rev,
            token=_token(f"at-{callback.code}"),
        )

    async def refresh_oauth_snapshot(self, config: Any, user_id: int, token: Any) -> bool:
        self.snapshots.append(config.require_id())
        return False

    async def forget_user(self, server_id: int, user_id: int) -> None:
        self.forgotten.append((server_id, user_id))


@asynccontextmanager
async def _client(
    env: SimpleNamespace,
    user_id: int,
) -> AsyncIterator[tuple[httpx.AsyncClient, Any]]:
    from fastapi import Depends

    from api import deps
    from harness.app.mcp_admin import McpAdminService
    from main import create_app

    connector = _FakeConnector()

    async def database() -> AsyncIterator[AsyncSession]:
        async with env.factory() as db:
            try:
                yield db
                await db.commit()
            except Exception:
                await db.rollback()
                raise

    async def admin_service(db: AsyncSession = Depends(deps.get_db)) -> McpAdminService:
        return McpAdminService(
            registry=McpServerService(db),
            connector=connector,  # type: ignore[arg-type]
            workspace_id=env.workspace_id,
            user_id=user_id,
        )

    app = create_app()
    app.dependency_overrides.update({
        deps.get_db: database,
        deps.get_mcp_admin_service: admin_service,
    })
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        yield client, connector


async def test_private_server_oauth_endpoints_are_404_for_others(env) -> None:
    for user_id in (env.other, env.admin):
        async with _client(env, user_id) as (http, connector):
            authorize = await http.post(f"/api/mcp/servers/{env.private_id}/oauth/authorize")
            revoke = await http.delete(f"/api/mcp/servers/{env.private_id}/oauth")
            complete = await http.post(
                "/api/mcp/oauth/complete",
                json={"state": str(env.private_id), "code": "c"},
            )
        for response in (authorize, revoke, complete):
            assert response.status_code == 404, response.text
            assert response.json()["error"]["message"] == "MCP server 不存在"
        assert connector.begun == []
    async with _client(env, env.member) as (http, connector):
        owner = await http.post(f"/api/mcp/servers/{env.private_id}/oauth/authorize")
    assert owner.status_code == 200
    assert owner.json()["data"]["authorization_url"].startswith("https://auth.example.com/")
    assert connector.begun == [(env.private_id, env.member)]
    assert await _active_tokens(env, env.private_id, env.other) == []


async def test_complete_and_revoke_through_http_show_status_without_token(env) -> None:
    async with _client(env, env.admin) as (http, connector):
        before = await http.get("/api/mcp/servers")
        done = await http.post(
            "/api/mcp/oauth/complete",
            json={"state": str(env.shared_id), "code": "c1", "iss": "https://auth.example.com"},
        )
        after = await http.get("/api/mcp/servers")
        revoked = await http.delete(f"/api/mcp/servers/{env.shared_id}/oauth")
        final = await http.get(f"/api/mcp/servers/{env.shared_id}")

    status_before = {item["server_key"]: item["oauth"] for item in before.json()["data"]}
    assert status_before["jira"]["status"] == "unauthorized"
    assert done.status_code == 200, done.text
    assert done.json()["data"]["oauth"]["status"] == "authorized"
    status_after = {item["server_key"]: item["oauth"] for item in after.json()["data"]}
    assert status_after["jira"] == {
        "status": "authorized",
        "scope": "tools.read",
        "expires_at": status_after["jira"]["expires_at"],
    }
    # 响应里没有 token。
    for response in (done, after, final):
        assert "at-c1" not in response.text and "rt-1" not in response.text
    assert revoked.json()["data"] == {"revoked": True}
    assert final.json()["data"]["oauth"]["status"] == "unauthorized"
    assert connector.forgotten == [(env.shared_id, env.admin), (env.shared_id, env.admin)]
    assert connector.snapshots == [env.shared_id]
    rows = await _active_tokens(env, env.shared_id, env.admin)
    assert [(row.is_deleted, row.token["access_token"]) for row in rows] == [(True, "at-c1")]


async def test_client_metadata_document_is_public(monkeypatch) -> None:
    from main import create_app

    monkeypatch.setattr(settings, "public_base_url", "https://agentos.example.com")
    transport = httpx.ASGITransport(app=create_app())
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get("/api/mcp/oauth/client-metadata.json")
        monkeypatch.setattr(settings, "public_base_url", None)
        missing = await client.get("/api/mcp/oauth/client-metadata.json")
    assert response.status_code == 200
    document = response.json()
    assert document["client_id"] == "https://agentos.example.com/api/mcp/oauth/client-metadata.json"
    assert document["redirect_uris"] == ["https://agentos.example.com/mcp/oauth/callback"]
    assert document["token_endpoint_auth_method"] == "none"
    assert missing.status_code == 404
