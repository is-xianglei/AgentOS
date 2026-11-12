"""MCP 管理接口与组装根的契约测试：不连数据库和网络。

- 路由经 get_mcp_admin_service 取 McpAdminService，依赖内部调用组装根的 mcp_admin_service；
- 响应走统一外壳，请求头的值不回显、错误里也不带；没有写权限的成员看不到地址的查询参数；
- mcp_enabled=False 时管理接口返回中文错误；组装根缺少连接器时抛中文错误；
- 本阶段 scoped_tools 仍为空，MCP 运行时惰性创建、lifespan 只在创建过时关闭；
- permission/api 对 mcp__ 名字不走「工具未注册」校验，改查 McpServerService。
"""

from collections.abc import AsyncIterator
from dataclasses import replace
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any

import httpx
import pytest

import database.registry  # noqa: F401  构造 ORM 实例前需全部映射在册
from api import deps
from core.config import settings
from core.errors import AgentException
from harness import bootstrap
from harness.app.mcp_admin import McpAdminService, McpRefreshResult
from harness.domain.mcp import (
    ApprovalMode,
    McpAuthType,
    McpServerConfig,
    McpToolAnnotations,
    McpToolSummary,
)
from harness.domain.mcp_config import McpServerView, McpToolDiff, list_server_tools
from mcp_server.service import McpServerService

pytestmark = pytest.mark.anyio

USER_ID = 5
WORKSPACE_ID = 2
_NOW = datetime(2026, 11, 12, tzinfo=UTC)
_SECRET = "Bearer super-secret-token"


def _tool(name: str) -> McpToolSummary:
    return McpToolSummary(
        name=name,
        description=f"{name} 的描述",
        input_schema_sha256="d" * 64,
        annotations=McpToolAnnotations(read_only=True),
    )


def _view(*, writable: bool = True, header_names: tuple[str, ...] | None = None) -> McpServerView:
    return McpServerView(
        config=McpServerConfig(
            server_id=11,
            workspace_id=WORKSPACE_ID,
            owner_user_id=None,
            server_key="github",
            url="https://mcp.example.com/mcp?key=abc",
            auth_type=McpAuthType.STATIC_HEADERS,
            approval_mode=ApprovalMode.TRUST_ANNOTATIONS,
            config_rev=2,
        ),
        name="GitHub",
        description=None,
        created_by=USER_ID,
        writable=writable,
        last_probe_at=_NOW,
        last_probe_error=None,
        tools=(_tool("create_issue"),),
        created_at=_NOW,
        updated_at=_NOW,
        header_names=header_names,
    )


class FakeAdmin:
    """McpAdminService 的替身：记下收到的参数，返回固定视图。"""

    def __init__(self) -> None:
        self.calls: list[tuple[str, Any]] = []

    async def list_servers(self) -> list[McpServerView]:
        self.calls.append(("list", None))
        return [_view(writable=False)]

    async def get_server(self, server_id: int) -> McpServerView:
        self.calls.append(("get", server_id))
        if server_id != 11:
            raise AgentException.message("MCP server 不存在", status_code=404)
        return _view(writable=False)

    async def create_server(self, draft: Any) -> McpServerView:
        self.calls.append(("create", draft))
        return _view(header_names=tuple(draft.secret.as_dict()) if draft.secret else ())

    async def update_server(self, server_id: int, changes: Any) -> McpServerView:
        self.calls.append(("update", (server_id, changes)))
        return _view(header_names=("Authorization",))

    async def delete_server(self, server_id: int) -> None:
        self.calls.append(("delete", server_id))

    async def refresh_tools(self, server_id: int) -> McpRefreshResult:
        self.calls.append(("probe", server_id))
        return McpRefreshResult(server=_view(), diff=McpToolDiff(added=("new_tool",)))

    async def list_tools(self, server_id: int) -> list[Any]:
        self.calls.append(("tools", server_id))
        return list_server_tools(_view().config, [_tool("create_issue")])


@pytest.fixture
async def client() -> AsyncIterator[tuple[httpx.AsyncClient, FakeAdmin]]:
    from main import create_app

    fake = FakeAdmin()
    app = create_app()
    app.dependency_overrides[deps.get_mcp_admin_service] = lambda: fake
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as http:
        yield http, fake


async def test_create_returns_tools_and_masks_headers(client) -> None:
    http, fake = client

    response = await http.post(
        "/api/mcp/servers",
        json={
            "visibility": "workspace",
            "server_key": "github",
            "name": "GitHub",
            "url": "HTTPS://MCP.Example.com/mcp/",
            "auth_type": "static_headers",
            "headers": {"Authorization": _SECRET},
            "tool_denylist": ["delete_repo"],
        },
    )

    assert response.status_code == 200, response.text
    assert _SECRET not in response.text
    data = response.json()["data"]
    assert data["server"]["headers"] == {"Authorization": "********"}
    assert data["server"]["visibility"] == "workspace"
    assert data["server"]["url"] == "https://mcp.example.com/mcp?key=abc"
    assert data["tools"][0]["name"] == "create_issue"
    (op, draft), = fake.calls
    assert op == "create"
    # 请求体在路由里转成领域输入：地址已规范化，凭证在 draft 上、不进 repr。
    assert draft.url == "https://mcp.example.com/mcp"
    assert draft.tool_denylist == ("delete_repo",)
    assert _SECRET not in repr(draft)


async def test_create_validation_errors_do_not_echo_secrets(client) -> None:
    http, fake = client

    bad_header = await http.post(
        "/api/mcp/servers",
        json={
            "visibility": "private",
            "server_key": "x",
            "name": "x",
            "url": "https://x.example.com",
            "auth_type": "static_headers",
            "headers": {"Cookie": _SECRET},
        },
    )
    bad_key = await http.post(
        "/api/mcp/servers",
        json={"visibility": "private", "server_key": "Bad", "name": "x", "url": "https://x.com"},
    )
    oauth = await http.post(
        "/api/mcp/servers",
        json={
            "visibility": "private",
            "server_key": "x",
            "name": "x",
            "url": "https://x.com",
            "auth_type": "oauth",
        },
    )
    extra = await http.post(
        "/api/mcp/servers",
        json={"visibility": "private", "server_key": "x", "name": "x", "url": "https://x.com",
              "owner_user_id": 1},
    )

    assert bad_header.status_code == 400
    assert _SECRET not in bad_header.text
    assert bad_header.json()["error"]["message"] == "MCP 请求头名称不合法或不允许配置"
    assert bad_key.status_code == 400
    assert "server_key 不合法" in bad_key.json()["error"]["message"]
    # 本阶段只开放 none 与 static_headers，oauth 在 Schema 层就被拒绝。
    assert oauth.status_code == 422
    assert extra.status_code == 422
    assert fake.calls == []


async def test_update_passes_only_submitted_fields(client) -> None:
    http, fake = client

    response = await http.put(
        "/api/mcp/servers/11",
        json={"headers": {"Authorization": "********"}, "description": None, "is_enabled": False},
    )
    null_name = await http.put("/api/mcp/servers/11", json={"name": None})

    assert response.status_code == 200, response.text
    assert response.json()["data"]["headers"] == {"Authorization": "********"}
    (op, (server_id, changes)), = fake.calls
    assert (op, server_id) == ("update", 11)
    assert changes == {
        "headers": {"Authorization": "********"},
        "description": None,
        "is_enabled": False,
    }
    assert null_name.status_code == 422


async def test_update_maps_visibility_and_enums(client) -> None:
    http, fake = client

    response = await http.put(
        "/api/mcp/servers/11",
        json={"visibility": "private", "approval_mode": "never", "auth_type": "none"},
    )

    assert response.status_code == 200, response.text
    (_, (_, changes)), = fake.calls
    assert changes == {
        "private": True,
        "approval_mode": ApprovalMode.NEVER,
        "auth_type": McpAuthType.NONE,
    }


async def test_member_view_redacts_url_query_and_has_no_headers(client) -> None:
    http, _ = client

    listed = await http.get("/api/mcp/servers")
    detail = await http.get("/api/mcp/servers/11")
    missing = await http.get("/api/mcp/servers/99")

    assert listed.status_code == 200
    (server,) = listed.json()["data"]
    assert server["url"] == "https://mcp.example.com/mcp?********"
    assert server["headers"] is None
    assert server["writable"] is False
    assert server["tool_count"] == 1
    assert detail.json()["data"]["id"] == 11
    assert missing.status_code == 404
    assert missing.json()["error"]["message"] == "MCP server 不存在"


async def test_delete_probe_and_tools(client) -> None:
    http, fake = client

    deleted = await http.delete("/api/mcp/servers/11")
    probed = await http.post("/api/mcp/servers/11/probe")
    tools = await http.get("/api/mcp/servers/11/tools")

    assert deleted.json()["data"] == {"deleted": True}
    assert probed.json()["data"]["ok"] is True
    assert probed.json()["data"]["diff"] == {
        "added": ["new_tool"],
        "removed": [],
        "schema_changed": [],
    }
    (tool,) = tools.json()["data"]
    assert tool["model_name"] == "mcp__github__create_issue"
    assert tool["requires_approval"] is False
    assert tool["filtered_out"] is False
    assert [op for op, _ in fake.calls] == ["delete", "probe", "tools"]


# ---------- 依赖与开关 ----------


async def test_dependency_builds_admin_service_from_bootstrap(monkeypatch) -> None:
    monkeypatch.setattr(settings, "mcp_enabled", True)
    db: Any = object()

    service = await deps.get_mcp_admin_service(db, SimpleNamespace(id=USER_ID), WORKSPACE_ID)

    assert isinstance(service, McpAdminService)
    assert isinstance(service._registry, McpServerService)
    assert service._registry.db is db
    assert (service._workspace_id, service._user_id) == (WORKSPACE_ID, USER_ID)


async def test_disabled_mcp_rejects_admin_endpoints(monkeypatch) -> None:
    from main import create_app

    monkeypatch.setattr(settings, "mcp_enabled", False)

    async def fake_db() -> AsyncIterator[None]:
        yield None

    app = create_app()
    app.dependency_overrides[deps.get_db] = fake_db
    app.dependency_overrides[deps.get_current_active_user] = lambda: SimpleNamespace(id=USER_ID)
    app.dependency_overrides[deps.get_current_workspace_id] = lambda: WORKSPACE_ID
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as http:
        responses = [
            await http.get("/api/mcp/servers"),
            await http.post("/api/mcp/servers/1/probe"),
            await http.delete("/api/mcp/servers/1"),
        ]

    for response in responses:
        assert response.status_code == 403
        assert response.json()["error"]["message"] == "MCP 功能未启用（AGENTOS_MCP_ENABLED=false）"


def test_harness_without_connector_raises_chinese_error() -> None:
    harness = replace(bootstrap.builtin_harness(), mcp_connector=None)

    with pytest.raises(AgentException, match="未提供 MCP 连接器"):
        harness.mcp_admin_service(object(), user_id=1, workspace_id=2)  # type: ignore[arg-type]


# ---------- 组装根：本阶段不接回合，运行时惰性创建 ----------


def test_default_harness_keeps_scoped_tools_empty_but_wires_connector() -> None:
    harness = bootstrap.default_harness()

    assert harness.scoped_tools == ()
    assert harness.mcp_connector is bootstrap.mcp_connector


def test_mcp_factories_share_lazy_runtime_and_read_settings(monkeypatch) -> None:
    from harness.adapters.mcp.connector import McpConnector
    from harness.adapters.mcp.provider import McpToolProvider
    from mcp_server.source import McpServerCatalog

    bootstrap.mcp_runtime.cache_clear()
    monkeypatch.setattr(settings, "mcp_allowed_private_hosts", "Intra.Local, ,mcp.corp")
    monkeypatch.setattr(settings, "mcp_list_timeout_seconds", 12.0)
    try:
        runtime = bootstrap.mcp_runtime()
        connector = bootstrap.mcp_connector()
        provider = bootstrap.mcp_tool_provider(bootstrap.AsyncSessionLocal)

        assert bootstrap.mcp_runtime() is runtime
        assert runtime.http.allowed_private_hosts == frozenset({"intra.local", "mcp.corp"})
        assert runtime.http.resolver is bootstrap.system_resolver
        assert isinstance(connector, McpConnector) and connector._runtime is runtime
        assert isinstance(provider, McpToolProvider) and provider.name == "mcp"
        assert provider._runtime is runtime
        assert provider._limits.list_timeout == 12.0
        assert isinstance(provider._servers, McpServerCatalog)
    finally:
        bootstrap.mcp_runtime.cache_clear()


async def test_close_mcp_runtime_only_closes_created_runtime(monkeypatch) -> None:
    closed: list[object] = []

    async def fake_close(self) -> None:
        closed.append(self)

    monkeypatch.setattr(bootstrap.McpRuntime, "aclose", fake_close)
    bootstrap.mcp_runtime.cache_clear()
    try:
        await bootstrap.close_mcp_runtime()
        assert closed == []
        assert bootstrap.mcp_runtime.cache_info().currsize == 0

        runtime = bootstrap.mcp_runtime()
        await bootstrap.close_mcp_runtime()
        assert closed == [runtime]
    finally:
        bootstrap.mcp_runtime.cache_clear()


# ---------- permission/api 对 mcp__ 名字的校验 ----------


async def test_permission_rule_tool_name_routes_mcp_names_to_mcp_service(monkeypatch) -> None:
    from permission import api as permission_api

    asked: list[tuple[int, str]] = []

    async def known(self: McpServerService, workspace_id: int, tool_name: str) -> bool:
        asked.append((workspace_id, tool_name))
        return tool_name == "mcp__github__create_issue"

    monkeypatch.setattr(McpServerService, "is_known_shared_tool", known)
    db: Any = object()

    await permission_api._validate_tool_name(db, "mcp__github__create_issue", WORKSPACE_ID)
    await permission_api._validate_tool_name(db, "Bash", WORKSPACE_ID)
    with pytest.raises(AgentException) as unknown_mcp:
        await permission_api._validate_tool_name(db, "mcp__secret__x", WORKSPACE_ID)
    with pytest.raises(AgentException) as unknown_builtin:
        await permission_api._validate_tool_name(db, "NoSuchTool", WORKSPACE_ID)

    # 内置工具不查 MCP；未知的 MCP 名字与未注册的内置工具报同一句话，不泄露私有 server 的存在。
    assert asked == [(WORKSPACE_ID, "mcp__github__create_issue"), (WORKSPACE_ID, "mcp__secret__x")]
    for error in (unknown_mcp.value, unknown_builtin.value):
        assert error.status_code == 400
        assert error.message == "工具未注册，不能配置权限规则"
