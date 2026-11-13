"""mcp_server 域对 harness 端口的生产实现：每次调用经组装根注入的会话工厂开一个短会话，用完即关。

- McpServerCatalog（McpServerSource）：每回合读 server 配置、凭证与当前用户的 OAuth token，只读不写；
- McpOAuthCatalog（McpOAuthStore）：OAuth 适配器读写客户端注册与 token，每次调用自开短事务并提交。

McpToolProvider 与 OAuth 适配器都不持有回合或请求的数据库会话，所以由组装根注入 session_factory
（AsyncSessionLocal）。读写规则仍归 McpServerService，这里只负责会话与事务的生命周期。
放在 mcp_server 包而不是组装根：它只依赖本域 Service 与应用层端口，组装根只负责把它接上。
"""

from collections.abc import Callable, Sequence
from contextlib import AbstractAsyncContextManager

from sqlalchemy.ext.asyncio import AsyncSession

from harness.domain.mcp import McpServerEntry, McpToolSummary
from harness.domain.mcp_oauth import McpOAuthClient, McpOAuthToken, McpTokenRefresh
from harness.domain.tool import ToolScope
from mcp_server.service import McpServerService

# 打开一个独立数据库会话的工厂（如 async_sessionmaker），由组装根注入。
type SessionFactory = Callable[[], AbstractAsyncContextManager[AsyncSession]]


class McpServerCatalog:
    """按回合身份列出可用 server 的配置与凭证，结构化满足 harness/app/mcp_port.py 的 McpServerSource。"""

    def __init__(self, session_factory: SessionFactory) -> None:
        self._session_factory = session_factory

    async def list_servers(self, scope: ToolScope) -> Sequence[McpServerEntry]:
        """已启用的共享 server 加上 scope.user_id 本人的私有 server；短会话只读，退出时回滚并关闭。"""
        async with self._session_factory() as db:
            return await McpServerService(db).list_usable(scope.workspace_id, scope.user_id)


class McpOAuthCatalog:
    """OAuth 客户端注册与 token 的读写，结构化满足 harness/app/mcp_port.py 的 McpOAuthStore。

    事务例外（CLAUDE.md 事务规则第 5 条）：这里的写入不借用回合或请求的会话，每次调用自开短事务并当场提交。
    原因是生命周期不同：刷新换来的新 token 必须落库，授权服务器那边旧的 refresh_token 可能已经失效，
    不能随回合回滚而丢；行锁也要在 HTTP 往返结束后随提交立即释放，不能挂到回合结束。
    """

    def __init__(self, session_factory: SessionFactory) -> None:
        self._session_factory = session_factory

    async def get_client(self, server_id: int) -> McpOAuthClient | None:
        async with self._session_factory() as db:
            return await McpServerService(db).get_oauth_client(server_id)

    async def save_client(self, server_id: int, client: McpOAuthClient) -> None:
        async with self._session_factory() as db:
            await McpServerService(db).save_oauth_client(server_id, client)
            await db.commit()

    async def get_token(self, server_id: int, user_id: int) -> McpOAuthToken | None:
        async with self._session_factory() as db:
            return await McpServerService(db).get_oauth_token(server_id, user_id)

    async def refresh_token_locked(
        self,
        server_id: int,
        user_id: int,
        refresh: McpTokenRefresh,
    ) -> McpOAuthToken | None:
        """专用短事务覆盖「加锁读 → 刷新的 HTTP 往返 → 写回」，提交时释放行锁；回调抛异常则回滚、不写库。"""
        async with self._session_factory() as db:
            try:
                token = await McpServerService(db).refresh_token_locked(server_id, user_id, refresh)
                await db.commit()
            except BaseException:
                await db.rollback()
                raise
            return token

    async def invalidate_token(self, server_id: int, user_id: int, access_token: str) -> bool:
        async with self._session_factory() as db:
            changed = await McpServerService(db).invalidate_oauth_token(
                server_id,
                user_id,
                access_token,
            )
            await db.commit()
            return changed

    async def record_tools_snapshot(
        self,
        server_id: int,
        config_rev: int,
        tools: Sequence[McpToolSummary],
    ) -> bool:
        async with self._session_factory() as db:
            written = await McpServerService(db).record_tools_snapshot(server_id, config_rev, tools)
            await db.commit()
            return written
