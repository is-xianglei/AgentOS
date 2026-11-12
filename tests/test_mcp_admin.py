"""McpAdminService 的编排顺序：用内存 registry 与假 connector，不连数据库和网络。

覆盖：校验在探测之前、探测失败不写库、探测成功才写库；只有地址或认证变化才重新探测，
config_rev 变化后才让连接失效；删除后失效；重新探测返回与快照的差异，失败时保留快照。
"""

from dataclasses import replace
from datetime import UTC, datetime
from typing import Any

import pytest

from core.errors import AgentException
from harness.app.mcp_admin import McpAdminService
from harness.app.mcp_port import McpServerConnector, McpServerRegistry
from harness.domain.mcp import (
    McpAuthType,
    McpProbeResult,
    McpSecret,
    McpServerConfig,
    McpToolAnnotations,
    McpToolSummary,
)
from harness.domain.mcp_config import (
    McpServerDraft,
    McpServerUpdate,
    McpServerView,
    McpWritableServer,
)

pytestmark = pytest.mark.anyio

WORKSPACE_ID = 1
USER_ID = 10
_NOW = datetime(2026, 11, 12, tzinfo=UTC)


def _tool(name: str, digest: str = "d") -> McpToolSummary:
    return McpToolSummary(
        name=name,
        description="",
        input_schema_sha256=digest,
        annotations=McpToolAnnotations(),
    )


def _ok(*tools: McpToolSummary) -> McpProbeResult:
    return McpProbeResult.success(
        server_name="fake",
        server_version="1",
        protocol_version="2026-07-28",
        tools=tools,
    )


class FakeConnector(McpServerConnector):
    """假连接器：按预设返回探测结果，记录每次探测的配置与失效调用。"""

    def __init__(self, result: McpProbeResult | None = None) -> None:
        self.result = result or _ok(_tool("a"))
        self.probed: list[tuple[McpServerConfig, McpSecret | None]] = []
        self.invalidated: list[int] = []
        self.journal: list[str] | None = None

    async def probe(self, config: McpServerConfig, secret: McpSecret | None) -> McpProbeResult:
        self.probed.append((config, secret))
        if self.journal is not None:
            self.journal.append("probe")
        return self.result

    async def invalidate(self, server_id: int) -> None:
        self.invalidated.append(server_id)
        if self.journal is not None:
            self.journal.append("invalidate")


class InMemoryRegistry:
    """McpServerRegistry 的内存实现：只保存一份配置，记下每一步调用，拒绝时抛预设的异常。"""

    def __init__(self) -> None:
        self.journal: list[str] = []
        self.views: dict[int, McpServerView] = {}
        self.secrets: dict[int, McpSecret | None] = {}
        self.reject_prepare: AgentException | None = None
        self.next_id = 1

    def _store(self, view: McpServerView, secret: McpSecret | None) -> McpServerView:
        self.views[view.server_id] = view
        self.secrets[view.server_id] = secret
        return view

    async def list_servers(self, workspace_id: int, actor_user_id: int) -> list[McpServerView]:
        self.journal.append("list")
        return list(self.views.values())

    async def get_server(
        self,
        workspace_id: int,
        actor_user_id: int,
        server_id: int,
    ) -> McpServerView:
        self.journal.append("get")
        if server_id not in self.views:
            raise AgentException.message("MCP server 不存在", status_code=404)
        return self.views[server_id]

    async def prepare_create(
        self,
        workspace_id: int,
        actor_user_id: int,
        draft: McpServerDraft,
    ) -> None:
        self.journal.append("prepare")
        if self.reject_prepare is not None:
            raise self.reject_prepare

    async def create_server(
        self,
        workspace_id: int,
        actor_user_id: int,
        draft: McpServerDraft,
        probe: McpProbeResult,
    ) -> McpServerView:
        self.journal.append("create")
        owner = actor_user_id if draft.private else None
        config = replace(
            draft.probe_config(workspace_id=workspace_id, owner_user_id=owner),
            server_id=self.next_id,
        )
        self.next_id += 1
        view = McpServerView(
            config=config,
            name=draft.name,
            description=draft.description,
            created_by=actor_user_id,
            writable=True,
            last_probe_at=_NOW,
            last_probe_error=None,
            tools=probe.tools,
            created_at=_NOW,
            updated_at=_NOW,
        )
        return self._store(view, draft.secret)

    async def load_for_write(
        self,
        workspace_id: int,
        actor_user_id: int,
        server_id: int,
    ) -> McpWritableServer:
        self.journal.append("load")
        if server_id not in self.views:
            raise AgentException.message("MCP server 不存在", status_code=404)
        return McpWritableServer(view=self.views[server_id], secret=self.secrets[server_id])

    async def apply_update(
        self,
        workspace_id: int,
        actor_user_id: int,
        server_id: int,
        update: McpServerUpdate,
        probe: McpProbeResult | None,
    ) -> McpServerView:
        self.journal.append("apply")
        view = replace(self.views[server_id], config=update.config, name=update.name)
        if probe is not None:
            view = replace(view, tools=probe.tools)
        return self._store(view, update.secret)

    async def delete_server(self, workspace_id: int, actor_user_id: int, server_id: int) -> None:
        self.journal.append("delete")
        self.views.pop(server_id)

    async def record_probe(
        self,
        workspace_id: int,
        actor_user_id: int,
        server_id: int,
        probe: McpProbeResult,
    ) -> McpServerView:
        self.journal.append("record_probe")
        view = self.views[server_id]
        if probe.ok:
            view = replace(view, tools=probe.tools, last_probe_error=None)
        else:
            view = replace(view, last_probe_error=probe.error)
        return self._store(view, self.secrets[server_id])


def _service(
    registry: InMemoryRegistry,
    connector: FakeConnector,
) -> McpAdminService:
    # 端口是结构化协议：内存实现直接满足，不继承。
    port: McpServerRegistry = registry
    connector.journal = registry.journal
    return McpAdminService(
        registry=port,
        connector=connector,
        workspace_id=WORKSPACE_ID,
        user_id=USER_ID,
    )


def _draft(**overrides: Any) -> McpServerDraft:
    values: dict[str, Any] = {
        "server_key": "github",
        "name": "GitHub",
        "url": "https://mcp.example.com/mcp",
        "private": False,
        "auth_type": McpAuthType.STATIC_HEADERS,
        "headers": {"Authorization": "Bearer t"},
    }
    values.update(overrides)
    return McpServerDraft.create(**values)


async def _created(registry: InMemoryRegistry, connector: FakeConnector) -> int:
    view = await _service(registry, connector).create_server(_draft())
    registry.journal.clear()
    connector.probed.clear()
    connector.invalidated.clear()
    return view.server_id


# ---------- 新建 ----------


async def test_create_validates_then_probes_then_writes() -> None:
    registry, connector = InMemoryRegistry(), FakeConnector(_ok(_tool("a"), _tool("b")))

    view = await _service(registry, connector).create_server(_draft(private=True))

    assert registry.journal == ["prepare", "probe", "create"]
    (config, secret), = connector.probed
    # 探测用的是尚未落库的配置，私有 server 的 owner 是当前用户，凭证随探测一起交给连接器。
    assert (config.server_id, config.owner_user_id, config.workspace_id) == (None, USER_ID, 1)
    assert secret is not None and secret.as_dict() == {"Authorization": "Bearer t"}
    assert [tool.name for tool in view.tools] == ["a", "b"]
    assert connector.invalidated == []


async def test_failed_probe_does_not_write() -> None:
    registry = InMemoryRegistry()
    connector = FakeConnector(McpProbeResult.failure("连接 MCP server 失败"))

    with pytest.raises(AgentException) as exc_info:
        await _service(registry, connector).create_server(_draft())

    assert exc_info.value.status_code == 400
    assert exc_info.value.details["error"] == "连接 MCP server 失败"
    assert registry.journal == ["prepare", "probe"]
    assert registry.views == {}


async def test_rejected_validation_skips_probe() -> None:
    registry, connector = InMemoryRegistry(), FakeConnector()
    registry.reject_prepare = AgentException.message("server_key 已被占用", status_code=409)

    with pytest.raises(AgentException, match="已被占用"):
        await _service(registry, connector).create_server(_draft())

    assert registry.journal == ["prepare"]
    assert connector.probed == []


async def test_invalid_address_from_connector_propagates_without_write() -> None:
    """地址不合法时连接器抛中文异常（属调用方输入错误），不落库。"""

    class RejectingConnector(FakeConnector):
        async def probe(self, config: McpServerConfig, secret: McpSecret | None) -> McpProbeResult:
            raise AgentException.message("MCP server 地址指向内网或保留地址", status_code=400)

    registry = InMemoryRegistry()

    with pytest.raises(AgentException, match="内网"):
        await _service(registry, RejectingConnector()).create_server(_draft())

    assert registry.journal == ["prepare"]
    assert registry.views == {}


# ---------- 修改 ----------


async def test_url_change_reprobes_bumps_revision_and_invalidates() -> None:
    registry, connector = InMemoryRegistry(), FakeConnector()
    server_id = await _created(registry, connector)
    connector.result = _ok(_tool("a"), _tool("c"))

    view = await _service(registry, connector).update_server(
        server_id,
        {"url": "https://other.example.com/mcp"},
    )

    assert registry.journal == ["load", "probe", "apply", "invalidate"]
    (config, secret), = connector.probed
    assert (config.url, config.config_rev, config.server_id) == (
        "https://other.example.com/mcp",
        2,
        server_id,
    )
    assert secret is not None and secret.as_dict() == {"Authorization": "Bearer t"}
    assert view.config.config_rev == 2
    assert [tool.name for tool in view.tools] == ["a", "c"]
    assert connector.invalidated == [server_id]


async def test_failed_reprobe_keeps_old_config() -> None:
    registry, connector = InMemoryRegistry(), FakeConnector()
    server_id = await _created(registry, connector)
    connector.result = McpProbeResult.failure("探测 MCP server 超时")

    with pytest.raises(AgentException, match="探测失败"):
        await _service(registry, connector).update_server(
            server_id,
            {"headers": {"Authorization": "Bearer new"}},
        )

    assert registry.journal == ["load", "probe"]
    assert registry.views[server_id].config.config_rev == 1
    secret = registry.secrets[server_id]
    assert secret is not None and secret.as_dict() == {"Authorization": "Bearer t"}
    assert connector.invalidated == []


async def test_filter_change_bumps_revision_without_probe() -> None:
    registry, connector = InMemoryRegistry(), FakeConnector()
    server_id = await _created(registry, connector)

    view = await _service(registry, connector).update_server(
        server_id,
        {"tool_denylist": ["a"]},
    )

    assert registry.journal == ["load", "apply", "invalidate"]
    assert connector.probed == []
    assert view.config.config_rev == 2


async def test_display_change_neither_probes_nor_invalidates() -> None:
    registry, connector = InMemoryRegistry(), FakeConnector()
    server_id = await _created(registry, connector)

    view = await _service(registry, connector).update_server(server_id, {"name": "改名"})

    assert registry.journal == ["load", "apply"]
    assert connector.probed == [] and connector.invalidated == []
    assert (view.name, view.config.config_rev) == ("改名", 1)


# ---------- 删除与重新探测 ----------


async def test_delete_soft_deletes_then_invalidates() -> None:
    registry, connector = InMemoryRegistry(), FakeConnector()
    server_id = await _created(registry, connector)

    await _service(registry, connector).delete_server(server_id)

    assert registry.journal == ["delete", "invalidate"]
    assert connector.invalidated == [server_id]


async def test_refresh_returns_diff_against_snapshot() -> None:
    registry, connector = InMemoryRegistry(), FakeConnector(_ok(_tool("a", "v1"), _tool("b")))
    server_id = await _created(registry, connector)
    connector.result = _ok(_tool("a", "v2"), _tool("c"))

    result = await _service(registry, connector).refresh_tools(server_id)

    assert registry.journal == ["load", "probe", "record_probe"]
    assert result.error is None
    assert (result.diff.added, result.diff.removed) == (("c",), ("b",))
    assert [change.name for change in result.diff.schema_changed] == ["a"]
    assert [tool.name for tool in result.server.tools] == ["a", "c"]
    # 只换快照不改配置：config_rev 不变，也不让连接失效。
    assert result.server.config.config_rev == 1
    assert connector.invalidated == []


async def test_failed_refresh_keeps_snapshot_and_reports_error() -> None:
    registry, connector = InMemoryRegistry(), FakeConnector(_ok(_tool("a")))
    server_id = await _created(registry, connector)
    connector.result = McpProbeResult.failure("连接 MCP server 失败")

    result = await _service(registry, connector).refresh_tools(server_id)

    assert result.error == "连接 MCP server 失败"
    assert result.diff.is_empty
    assert [tool.name for tool in result.server.tools] == ["a"]
    assert result.server.last_probe_error == "连接 MCP server 失败"


async def test_list_tools_reads_snapshot_without_probe() -> None:
    registry, connector = InMemoryRegistry(), FakeConnector(_ok(_tool("a.b"), _tool("x")))
    server_id = await _created(registry, connector)

    listings = await _service(registry, connector).list_tools(server_id)

    assert registry.journal == ["get"]
    assert connector.probed == []
    assert [listing.summary.name for listing in listings] == ["a.b", "x"]
    assert listings[1].model_name == "mcp__github__x"
    assert all(listing.requires_approval for listing in listings)
