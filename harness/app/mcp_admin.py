"""McpAdminService：MCP server 管理的应用层入口，编排「持久化 + 网络」两件事。

顺序固定为：先经 McpServerRegistry 在库里校验角色、可见性和重名 → 经 McpServerConnector 探测 →
探测成功才写库 → 让本进程的连接与缓存失效（手动刷新只清工具清单缓存）。角色、上限与重名这些业务规则归数据所属域
（mcp_server 的 McpServerService），这里只编排，不重复校验。

管理接口照常走 get_db 的请求事务：探测期间事务空闲挂着，最长挂 mcp_probe_timeout_seconds；
管理操作频率低，不为此破例分步提交。

OAuth 的两个半程：发起时先经 registry 核对当前用户能授权这个 server（看不到即 404，私有 server 因此只有 owner
本人能授权），再由连接器发现、注册并返回授权地址；完成时连接器核对 state 的发起人就是当前用户、换 token，
token 经 registry 写入请求事务。客户端注册信息由适配器经 McpOAuthStore 自开短事务写入，不在请求事务里。
"""

import logging
from dataclasses import dataclass

from core.errors import AgentException
from harness.app.mcp_port import McpServerConnector, McpServerRegistry
from harness.domain.mcp import McpProbeResult, McpServerConfig
from harness.domain.mcp_config import (
    McpServerChanges,
    McpServerDraft,
    McpServerView,
    McpToolDiff,
    McpToolListing,
    diff_tools,
    list_server_tools,
    plan_update,
)
from harness.domain.mcp_oauth import OAuthCallback

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class McpRefreshResult:
    """重新探测的结果：探测后的 server 视图，与写入前快照的差异；探测失败时差异为空、error 非空。"""

    server: McpServerView
    diff: McpToolDiff
    error: str | None = None


class McpAdminService:
    """一次请求的管理入口：绑定可信的工作区与当前用户，由组装根 mcp_admin_service(...) 构造。"""

    def __init__(
        self,
        *,
        registry: McpServerRegistry,
        connector: McpServerConnector,
        workspace_id: int,
        user_id: int,
    ) -> None:
        self._registry = registry
        self._connector = connector
        self._workspace_id = workspace_id
        self._user_id = user_id

    async def list_servers(self) -> list[McpServerView]:
        return await self._registry.list_servers(self._workspace_id, self._user_id)

    async def get_server(self, server_id: int) -> McpServerView:
        return await self._registry.get_server(self._workspace_id, self._user_id, server_id)

    async def list_tools(self, server_id: int) -> list[McpToolListing]:
        """读工具快照，按回合同一套规则附上模型名、过滤结果和推导出的审批要求；不连 server。"""
        server = await self.get_server(server_id)
        return list_server_tools(server.config, server.tools)

    async def create_server(self, draft: McpServerDraft) -> McpServerView:
        """校验 → 探测 → 探测成功才写库；失败的探测不留半条配置。"""
        await self._registry.prepare_create(self._workspace_id, self._user_id, draft)
        owner = self._user_id if draft.private else None
        config = draft.probe_config(workspace_id=self._workspace_id, owner_user_id=owner)
        probe = await self._connector.probe(config, draft.secret)
        self._require_success(probe, server_key=draft.server_key)
        return await self._registry.create_server(
            self._workspace_id,
            self._user_id,
            draft,
            probe,
        )

    async def update_server(self, server_id: int, changes: McpServerChanges) -> McpServerView:
        """地址或认证变化时重新探测，探测成功才写库；配置版本变化后让本进程的旧连接失效。"""
        current = await self._registry.load_for_write(self._workspace_id, self._user_id, server_id)
        update = plan_update(current, changes)
        probe: McpProbeResult | None = None
        if update.needs_probe:
            probe = await self._connector.probe(update.config, update.secret)
            self._require_success(probe, server_key=update.config.server_key)
        server = await self._registry.apply_update(
            self._workspace_id,
            self._user_id,
            server_id,
            update,
            probe,
        )
        if update.revision_changed:
            await self._connector.invalidate(server_id)
        return server

    async def delete_server(self, server_id: int) -> None:
        """软删 server 与凭证，再让本进程的连接失效；其他实例从下一回合起读不到它。"""
        await self._registry.delete_server(self._workspace_id, self._user_id, server_id)
        await self._connector.invalidate(server_id)

    async def refresh_tools(self, server_id: int) -> McpRefreshResult:
        """强制重新探测并拉取完整工具清单，返回与上次快照的差异（新增、删除、schema 变化）。

        探测失败不抛错：记下失败时间与错误、保留旧快照，结果里带中文错误。
        只换快照（管理界面与权限规则校验用）不改配置，config_rev 不变，也不让连接失效；
        探测成功后清掉本进程里这个 server 的工具清单缓存，下一回合就拉到新清单，
        其他实例仍按 mcp_tools_cache_seconds 过期后感知变化。
        """
        current = await self._registry.load_for_write(self._workspace_id, self._user_id, server_id)
        config = current.view.config
        probe = await self._connector.probe(config, current.secret)
        server = await self._registry.record_probe(
            self._workspace_id,
            self._user_id,
            server_id,
            probe,
        )
        if not probe.ok:
            return McpRefreshResult(server=server, diff=McpToolDiff(), error=probe.error)
        await self._connector.drop_cached_tools(server_id)
        return McpRefreshResult(server=server, diff=diff_tools(current.view.tools, probe.tools))

    # ---------- OAuth ----------

    async def begin_oauth(self, server_id: int) -> str:
        """授权前半程：核对当前用户能授权这个 server，返回授权地址（带 resource、PKCE 与 state）。"""
        config = await self._registry.require_oauth_server(
            self._workspace_id,
            self._user_id,
            server_id,
        )
        return await self._connector.begin_oauth(
            config,
            workspace_id=self._workspace_id,
            user_id=self._user_id,
        )

    async def complete_oauth(self, callback: OAuthCallback) -> McpServerView:
        """授权后半程：换 token → 写库（请求事务）→ 本进程旧缓存作废 → 用新 token 回写快照（独立短事务）。"""
        grant = await self._connector.complete_oauth(
            callback,
            workspace_id=self._workspace_id,
            user_id=self._user_id,
            config_for=self._oauth_config,
        )
        server = await self._registry.save_oauth_token(self._workspace_id, self._user_id, grant)
        await self._connector.forget_user(grant.server_id, self._user_id)
        await self._connector.refresh_oauth_snapshot(server.config, self._user_id, grant.token)
        return server

    async def revoke_oauth(self, server_id: int) -> bool:
        """撤销自己的授权：软删自己的 token，本进程里这个用户的连接与 token 缓存随之作废。"""
        revoked = await self._registry.revoke_oauth_token(
            self._workspace_id,
            self._user_id,
            server_id,
        )
        await self._connector.forget_user(server_id, self._user_id)
        return revoked

    async def _oauth_config(self, server_id: int) -> McpServerConfig:
        return await self._registry.require_oauth_server(
            self._workspace_id,
            self._user_id,
            server_id,
        )

    @staticmethod
    def _require_success(probe: McpProbeResult, *, server_key: str) -> None:
        """创建或改地址时探测必须成功：失败只带可展示的中文错误，不落库。"""
        if probe.ok:
            return
        logger.info("MCP server 探测失败，未写库，server_key=%s", server_key)
        raise AgentException.message(
            "MCP server 探测失败",
            {"server_key": server_key, "error": probe.error},
            status_code=400,
        )
