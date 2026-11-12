"""自建 Agent 绑定 MCP server（M2）的真实 PostgreSQL 集成测试。

在一个外层事务里跑、结束时整体回滚（同 test_mcp_server_pg.py）。覆盖：
- agent_mcp_servers 的部分唯一索引（软删后可以重新绑定）与 tool_names 的 CHECK；
- 绑定写入与运行配置（原名换模型名、停用 server 暂时不可见）；
- 软删 server 时经 agent 域级联解除绑定，Agent 下一回合看不到它；
- 绑定私有 server、停用 server、别人的私有 server 报错；
- 接口 PUT /api/agents/{id}/mcp-servers 的权限、404 与 Agent 详情里的绑定。
库不可达或未升级到 0033 时跳过。
"""

from collections.abc import AsyncIterator
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any
from uuid import uuid4

import httpx
import pytest
from sqlalchemy import select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.pool import NullPool

import database.engine  # noqa: F401  副作用注册全部映射与全局软删过滤
from agent.models import AgentMcpServerRecord, AgentRecord
from agent.service import AgentDefinitionService, AgentMcpBinding, AgentMcpBindingInput
from core.config import settings
from core.errors import AgentException
from harness.domain.mcp import model_tool_names
from mcp_server.models import McpServerRecord
from mcp_server.service import McpServerService
from user.models import UserRecord
from workspace.models import WorkspaceMemberRecord, WorkspaceRecord

pytestmark = pytest.mark.anyio


def _server(workspace_id: int, server_key: str, **overrides: Any) -> McpServerRecord:
    values: dict[str, Any] = {
        "workspace_id": workspace_id,
        "server_key": server_key,
        "name": server_key,
        "url": f"https://{server_key}.example.com/mcp",
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


@pytest.fixture
async def pg() -> AsyncIterator[SimpleNamespace]:
    """外层事务：两个工作区；admin、member 与另一工作区的 owner；若干 server 与一个 Agent。"""
    engine = create_async_engine(settings.database_url, poolclass=NullPool)
    try:
        conn = await engine.connect()
    except Exception as exc:  # noqa: BLE001  任何连接错误都只用于决定跳过
        await engine.dispose()
        pytest.skip(f"PostgreSQL 不可达(AGENTOS_DATABASE_URL): {type(exc).__name__}")
    transaction = await conn.begin()
    session = AsyncSession(bind=conn, expire_on_commit=False)
    try:
        if not await session.scalar(text("SELECT to_regclass('agent_mcp_servers')")):
            pytest.skip("需要已升级到 0033_agent_mcp_servers 的数据库")
        suffix = uuid4().hex[:12]
        users = {
            name: UserRecord(
                email=f"amcp-{name}-{suffix}@test.local",
                username=f"amcp-{name}-{suffix}",
                password="x",
            )
            for name in ("admin", "member", "stranger")
        }
        home, away = (
            WorkspaceRecord(name=f"amcp-{n}-{suffix}", slug=f"amcp-{n}-{suffix}", display_name=n)
            for n in ("home", "away")
        )
        session.add_all([*users.values(), home, away])
        await session.flush()
        now = datetime.now(UTC)
        session.add_all(
            [
                WorkspaceMemberRecord(
                    workspace_id=home.id, user_id=users["admin"].id, role="admin", joined_at=now
                ),
                WorkspaceMemberRecord(
                    workspace_id=home.id, user_id=users["member"].id, role="member", joined_at=now
                ),
                WorkspaceMemberRecord(
                    workspace_id=away.id, user_id=users["stranger"].id, role="owner", joined_at=now
                ),
            ]
        )
        servers = {
            "github": _server(home.id, "github"),
            "jira": _server(home.id, "jira"),
            "off": _server(home.id, "off", is_enabled=False),
            "admin_private": _server(home.id, "mine", owner_user_id=users["admin"].id),
            "member_private": _server(home.id, "theirs", owner_user_id=users["member"].id),
            "away": _server(away.id, "github"),
        }
        session.add_all(servers.values())
        await session.flush()
        agent = AgentRecord(
            workspace_id=home.id,
            name=f"审稿人-{suffix}",
            system_prompt="你是审稿人",
            is_enabled=True,
            created_by_user_id=users["admin"].id,
        )
        session.add(agent)
        await session.flush()
        yield SimpleNamespace(
            db=session,
            workspace_id=home.id,
            away_workspace_id=away.id,
            admin=users["admin"].id,
            member=users["member"].id,
            stranger=users["stranger"].id,
            servers={key: server.id for key, server in servers.items()},
            agent_id=agent.id,
        )
    finally:
        await session.close()
        await transaction.rollback()
        await conn.close()
        await engine.dispose()


async def _violates(db: AsyncSession, record: AgentMcpServerRecord) -> bool:
    """在保存点里直接插入，绕过 Service；撞索引或 CHECK 时回滚保存点并返回 True。"""
    savepoint = await db.begin_nested()
    db.add(record)
    try:
        await db.flush()
    except IntegrityError:
        await savepoint.rollback()
        return True
    await savepoint.commit()
    return False


def _bind(server_id: int, *names: str) -> AgentMcpBindingInput:
    return AgentMcpBindingInput(server_id=server_id, tool_names=tuple(names) if names else None)


# ---------- 部分唯一索引与 CHECK ----------


async def test_partial_unique_index_and_tool_names_check(pg) -> None:
    db, github = pg.db, pg.servers["github"]
    await AgentDefinitionService(db).replace_mcp_bindings(
        pg.workspace_id, pg.agent_id, pg.admin, [_bind(github, "create_issue")]
    )

    # 同一 Agent 对同一 server 只能有一条有效绑定。
    duplicate = AgentMcpServerRecord(agent_id=pg.agent_id, server_id=github, tool_names=None)
    assert await _violates(db, duplicate)
    # 库里同样拒绝空白名单；NULL 才表示全部工具。
    empty = AgentMcpServerRecord(agent_id=pg.agent_id, server_id=pg.servers["jira"], tool_names=[])
    assert await _violates(db, empty)

    # 解除后可以重新绑定：旧行软删，新行插入。
    service = AgentDefinitionService(db)
    await service.replace_mcp_bindings(pg.workspace_id, pg.agent_id, pg.admin, [])
    await AgentDefinitionService(db).replace_mcp_bindings(
        pg.workspace_id, pg.agent_id, pg.admin, [_bind(github)]
    )
    rows = (
        await db.scalars(
            select(AgentMcpServerRecord)
            .where(AgentMcpServerRecord.agent_id == pg.agent_id)
            .order_by(AgentMcpServerRecord.id)
            .execution_options(include_deleted=True)
        )
    ).all()
    assert [(row.server_id, row.tool_names, row.is_deleted) for row in rows] == [
        (github, ["create_issue"], True),
        (github, None, False),
    ]
    # NULL 存成 SQL NULL，而不是 JSON 的 null。
    stored = await db.scalar(
        text("SELECT tool_names IS NULL FROM agent_mcp_servers WHERE id = :id"), {"id": rows[1].id}
    )
    assert stored is True


# ---------- 运行配置与删除级联 ----------


async def test_runtime_config_converts_names_and_hides_disabled_server(pg) -> None:
    db = pg.db
    service = AgentDefinitionService(db)
    await service.replace_mcp_bindings(
        pg.workspace_id,
        pg.agent_id,
        pg.admin,
        [_bind(pg.servers["github"], "create_issue", "a.b"), _bind(pg.servers["jira"])],
    )
    jira = await db.get(McpServerRecord, pg.servers["jira"])
    jira.is_enabled = False
    await db.flush()

    config = await service.get_runtime_config(pg.workspace_id, pg.agent_id)

    expected = model_tool_names("github", ["create_issue", "a.b"]).names.values()
    assert config.mcp_bindings == (
        AgentMcpBinding(source="mcp:github", tool_names=frozenset(expected)),
    )
    # 停用的 server 只是暂时不可见，绑定保留，重新启用后恢复。
    jira.is_enabled = True
    await db.flush()
    config = await service.get_runtime_config(pg.workspace_id, pg.agent_id)
    assert [binding.source for binding in config.mcp_bindings] == ["mcp:github", "mcp:jira"]


async def test_deleting_server_soft_deletes_bindings_in_same_transaction(pg) -> None:
    db = pg.db
    await AgentDefinitionService(db).replace_mcp_bindings(
        pg.workspace_id,
        pg.agent_id,
        pg.admin,
        [_bind(pg.servers["github"]), _bind(pg.servers["jira"])],
    )

    await McpServerService(db).delete_server(pg.workspace_id, pg.admin, pg.servers["github"])

    rows = (
        await db.scalars(
            select(AgentMcpServerRecord)
            .where(AgentMcpServerRecord.agent_id == pg.agent_id)
            .execution_options(include_deleted=True)
        )
    ).all()
    states = {row.server_id: row.is_deleted for row in rows}
    assert states == {pg.servers["github"]: True, pg.servers["jira"]: False}
    # 预加载的绑定关系同样经全局软删过滤，详情里只剩有效绑定。
    agent = await AgentDefinitionService(db).get_agent(pg.workspace_id, pg.agent_id, pg.admin)
    assert [binding.server_id for binding in agent.mcp_bindings] == [pg.servers["jira"]]
    # Agent 下一回合就看不到被删的 server。
    config = await AgentDefinitionService(db).get_runtime_config(pg.workspace_id, pg.agent_id)
    assert [binding.source for binding in config.mcp_bindings] == ["mcp:jira"]


@pytest.mark.parametrize(
    ("server", "status_code", "message"),
    [
        ("admin_private", 400, "私有 MCP server 不能绑定到 Agent，只能绑定工作区共享的 server"),
        ("member_private", 404, "MCP server 不存在"),
        ("off", 409, "MCP server 已停用，不能绑定"),
        ("away", 404, "MCP server 不存在"),
    ],
    ids=["own_private", "others_private", "disabled", "cross_workspace"],
)
async def test_binding_unusable_server_is_rejected(
    pg, server: str, status_code: int, message: str
) -> None:
    with pytest.raises(AgentException) as exc_info:
        await AgentDefinitionService(pg.db).replace_mcp_bindings(
            pg.workspace_id, pg.agent_id, pg.admin, [_bind(pg.servers[server])]
        )

    assert exc_info.value.status_code == status_code
    assert exc_info.value.message == message
    count = await pg.db.scalar(
        text("SELECT count(*) FROM agent_mcp_servers WHERE agent_id = :id"), {"id": pg.agent_id}
    )
    assert count == 0


# ---------- 接口 PUT /api/agents/{id}/mcp-servers ----------


async def _client(pg, user_id: int, workspace_id: int) -> httpx.AsyncClient:
    """经真实路由、真实 Service 与本用例的外层事务发请求；只替换会话与当前用户、工作区。"""
    from api.deps import get_current_active_user, get_current_workspace_id, get_db
    from main import create_app

    user = await pg.db.get(UserRecord, user_id)

    async def database() -> AsyncIterator[AsyncSession]:
        yield pg.db

    app = create_app()
    app.dependency_overrides.update({
        get_db: database,
        get_current_active_user: lambda: user,
        get_current_workspace_id: lambda: workspace_id,
    })
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")


async def test_put_mcp_servers_api_permission_and_404(pg) -> None:
    url = f"/api/agents/{pg.agent_id}/mcp-servers"
    github, jira = pg.servers["github"], pg.servers["jira"]
    body = {"servers": [{"server_id": jira}, {"server_id": github, "tool_names": ["create_issue"]}]}

    async with await _client(pg, pg.admin, pg.workspace_id) as admin:
        ok_response = await admin.put(url, json=body)
        detail = await admin.get(f"/api/agents/{pg.agent_id}")
        empty = await admin.put(url, json={"servers": [{"server_id": github, "tool_names": []}]})
        missing_agent = await admin.put("/api/agents/999999999/mcp-servers", json=body)
        missing_server = await admin.put(url, json={"servers": [{"server_id": 999999999}]})
    async with await _client(pg, pg.member, pg.workspace_id) as member:
        forbidden = await member.put(url, json=body)
    async with await _client(pg, pg.stranger, pg.away_workspace_id) as stranger:
        other_workspace = await stranger.put(url, json=body)

    assert ok_response.status_code == 200, ok_response.text
    expected = [
        {"server_id": github, "tool_names": ["create_issue"]},
        {"server_id": jira, "tool_names": None},
    ]
    assert ok_response.json()["data"]["mcp_servers"] == expected
    assert detail.json()["data"]["mcp_servers"] == expected
    assert empty.status_code == 400
    # 权限同 PUT /api/agents/{id}/tools：只有 owner / admin 能改。
    assert forbidden.status_code == 403
    assert (missing_agent.status_code, other_workspace.status_code) == (404, 404)
    assert missing_server.status_code == 404
    assert missing_server.json()["error"]["message"] == "MCP server 不存在"
