"""McpAdminService 的 OAuth 编排：内存 registry 加假连接器，不连数据库和网络。

- 发起：先经 registry 核对当前用户能授权这个 server（看不到即 404），再由连接器生成授权地址；
- 完成：连接器换 token（config_for 经 registry 取配置）→ registry 写 token → 本进程缓存作废 → 回写快照；
- 撤销：registry 软删自己的 token → 本进程缓存作废；
- 新建 OAuth server：探测只验证元数据（metadata_only），快照为空。
"""

from dataclasses import replace
from datetime import UTC, datetime
from typing import Any

import pytest

from core.errors import AgentException
from harness.app.mcp_admin import McpAdminService
from harness.domain.mcp import McpAuthType, McpProbeResult, McpServerConfig
from harness.domain.mcp_config import McpServerDraft, McpServerView
from harness.domain.mcp_oauth import (
    McpOAuthAuthorization,
    McpOAuthGrant,
    McpOAuthStatus,
    McpOAuthToken,
    OAuthCallback,
)
from tests.test_mcp_admin import FakeConnector, InMemoryRegistry

pytestmark = pytest.mark.anyio

WORKSPACE_ID = 1
USER_ID = 10
_NOW = datetime(2026, 11, 13, tzinfo=UTC)


class OAuthRegistry(InMemoryRegistry):
    """在内存 registry 上补 OAuth 的四个方法：看不到的 server 404，写 token 时核对配置版本。"""

    def __init__(self) -> None:
        super().__init__()
        self.tokens: dict[tuple[int, int], McpOAuthToken] = {}

    async def require_oauth_server(
        self,
        workspace_id: int,
        actor_user_id: int,
        server_id: int,
    ) -> McpServerConfig:
        self.journal.append("require_oauth")
        view = self.views.get(server_id)
        if view is None:
            raise AgentException.message("MCP server 不存在", status_code=404)
        return view.config

    async def save_oauth_token(
        self,
        workspace_id: int,
        actor_user_id: int,
        grant: McpOAuthGrant,
    ) -> McpServerView:
        self.journal.append("save_token")
        view = self.views[grant.server_id]
        if view.config.config_rev != grant.config_rev:
            raise AgentException.message("配置已被修改", status_code=409)
        self.tokens[(grant.server_id, actor_user_id)] = grant.token
        return replace(
            view,
            oauth=McpOAuthAuthorization(status=McpOAuthStatus.AUTHORIZED, scope=grant.token.scope),
        )

    async def revoke_oauth_token(
        self,
        workspace_id: int,
        actor_user_id: int,
        server_id: int,
    ) -> bool:
        self.journal.append("revoke")
        if server_id not in self.views:
            raise AgentException.message("MCP server 不存在", status_code=404)
        return self.tokens.pop((server_id, actor_user_id), None) is not None


class OAuthConnector(FakeConnector):
    """假连接器的 OAuth 部分：记录调用顺序；完成时通过 config_for 取配置，模拟授权期间的配置变化。"""

    def __init__(self, result: McpProbeResult | None = None) -> None:
        super().__init__(result)
        self.calls: list[tuple[str, Any]] = []

    async def begin_oauth(self, config: McpServerConfig, *, workspace_id: int, user_id: int) -> str:
        self._note("begin", (config.server_id, workspace_id, user_id))
        return "https://auth.example.com/authorize?x=1"

    async def complete_oauth(
        self,
        callback: OAuthCallback,
        *,
        workspace_id: int,
        user_id: int,
        config_for: Any,
    ) -> McpOAuthGrant:
        config = await config_for(int(callback.state))
        self._note("complete", (callback.state, workspace_id, user_id))
        return McpOAuthGrant(
            server_id=config.require_id(),
            config_rev=config.config_rev,
            token=McpOAuthToken(access_token="at", refresh_token="rt", scope="tools.read"),
        )

    async def refresh_oauth_snapshot(
        self,
        config: McpServerConfig,
        user_id: int,
        token: McpOAuthToken,
    ) -> bool:
        self._note("snapshot", (config.server_id, user_id))
        return True

    async def forget_user(self, server_id: int, user_id: int) -> None:
        self._note("forget", (server_id, user_id))

    def _note(self, name: str, args: Any) -> None:
        self.calls.append((name, args))
        if self.journal is not None:
            self.journal.append(name)


def _service(registry: OAuthRegistry, connector: OAuthConnector) -> McpAdminService:
    connector.journal = registry.journal
    return McpAdminService(
        registry=registry,
        connector=connector,
        workspace_id=WORKSPACE_ID,
        user_id=USER_ID,
    )


async def _oauth_server(registry: OAuthRegistry, connector: OAuthConnector) -> int:
    draft = McpServerDraft.create(
        server_key="jira",
        name="Jira",
        url="https://mcp.example.com/mcp",
        private=False,
        auth_type=McpAuthType.OAUTH,
    )
    view = await _service(registry, connector).create_server(draft)
    registry.journal.clear()
    connector.calls.clear()
    return view.server_id


async def test_create_oauth_server_probes_metadata_only_and_keeps_empty_snapshot() -> None:
    registry = OAuthRegistry()
    connector = OAuthConnector(McpProbeResult.metadata_only())
    server_id = await _oauth_server(registry, connector)

    view = registry.views[server_id]
    assert view.config.auth_type is McpAuthType.OAUTH
    assert view.tools == ()
    ((config, secret),) = connector.probed
    assert config.auth_type is McpAuthType.OAUTH and secret is None


async def test_begin_checks_registry_before_connector() -> None:
    registry = OAuthRegistry()
    connector = OAuthConnector(McpProbeResult.metadata_only())
    server_id = await _oauth_server(registry, connector)

    url = await _service(registry, connector).begin_oauth(server_id)

    assert url.startswith("https://auth.example.com/authorize")
    assert registry.journal == ["require_oauth", "begin"]
    assert connector.calls == [("begin", (server_id, WORKSPACE_ID, USER_ID))]


async def test_begin_for_invisible_server_is_404_without_network() -> None:
    registry = OAuthRegistry()
    connector = OAuthConnector()
    with pytest.raises(AgentException) as exc_info:
        await _service(registry, connector).begin_oauth(99)
    assert exc_info.value.status_code == 404
    assert connector.calls == []


async def test_complete_saves_token_then_forgets_cache_then_records_snapshot() -> None:
    registry = OAuthRegistry()
    connector = OAuthConnector(McpProbeResult.metadata_only())
    server_id = await _oauth_server(registry, connector)

    view = await _service(registry, connector).complete_oauth(
        OAuthCallback(state=str(server_id), code="c", iss="https://auth.example.com"),
    )

    assert registry.journal == ["require_oauth", "complete", "save_token", "forget", "snapshot"]
    assert view.oauth is not None and view.oauth.status is McpOAuthStatus.AUTHORIZED
    assert registry.tokens[(server_id, USER_ID)].access_token == "at"


async def test_complete_rejected_when_config_changed_during_authorization() -> None:
    registry = OAuthRegistry()
    connector = OAuthConnector(McpProbeResult.metadata_only())
    server_id = await _oauth_server(registry, connector)
    original = connector.complete_oauth

    async def stale(callback: OAuthCallback, **kwargs: Any) -> McpOAuthGrant:
        grant = await original(callback, **kwargs)
        return replace(grant, config_rev=grant.config_rev - 1)

    connector.complete_oauth = stale  # type: ignore[method-assign]
    with pytest.raises(AgentException) as exc_info:
        await _service(registry, connector).complete_oauth(
            OAuthCallback(state=str(server_id), code="c"),
        )
    assert exc_info.value.status_code == 409
    assert registry.tokens == {}
    assert "forget" not in registry.journal and "snapshot" not in registry.journal


async def test_revoke_soft_deletes_then_forgets_cache() -> None:
    registry = OAuthRegistry()
    connector = OAuthConnector(McpProbeResult.metadata_only())
    server_id = await _oauth_server(registry, connector)
    service = _service(registry, connector)
    await service.complete_oauth(OAuthCallback(state=str(server_id), code="c"))
    registry.journal.clear()

    first = await service.revoke_oauth(server_id)
    second = await service.revoke_oauth(server_id)

    assert (first, second) == (True, False)
    assert registry.journal == ["revoke", "forget", "revoke", "forget"]
