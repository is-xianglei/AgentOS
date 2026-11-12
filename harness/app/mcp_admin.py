"""McpAdminService：MCP server 管理的应用层入口，编排「持久化 + 网络」两件事。

顺序固定为：先经 McpServerRegistry 在库里校验角色、可见性和重名 → 经 McpServerConnector 探测 →
探测成功才写库 → 让本进程的连接与缓存失效。角色、上限与重名这些业务规则归数据所属域
（mcp_server 的 McpServerService），这里只编排，不重复校验。

管理接口照常走 get_db 的请求事务：探测期间事务空闲挂着，最长挂 mcp_probe_timeout_seconds；
管理操作频率低，不为此破例分步提交。OAuth 的两个半程在 M3 加入。
"""

import logging
from dataclasses import dataclass

from core.errors import AgentException
from harness.app.mcp_port import McpServerConnector, McpServerRegistry
from harness.domain.mcp import McpProbeResult
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
        只换快照（管理界面与权限规则校验用）不改配置，config_rev 不变，也不让连接失效：
        回合里的工具清单走 SDK 响应缓存，按 mcp_tools_cache_seconds 过期后感知变化。
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
        return McpRefreshResult(server=server, diff=diff_tools(current.view.tools, probe.tools))

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
