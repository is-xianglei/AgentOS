"""McpServerSource 的生产实现：每回合开一个短数据库会话读 server 配置与凭证，读完即关。

McpToolProvider 不持有回合的数据库会话，所以由组装根注入 session_factory（AsyncSessionLocal）。
短会话只读不写、不占用回合的事务；读写规则仍归 McpServerService，这里只负责会话生命周期。
放在 mcp_server 包而不是组装根：它只依赖本域 Service 与应用层端口，组装根只负责把它接上。
"""

from collections.abc import Callable, Sequence
from contextlib import AbstractAsyncContextManager

from sqlalchemy.ext.asyncio import AsyncSession

from harness.domain.mcp import McpServerEntry
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
