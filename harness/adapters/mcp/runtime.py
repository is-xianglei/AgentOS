"""McpRuntime：进程级 MCP 运行时，持有连接池与共享响应缓存，提供 aclose()。

缓存直接用 SDK 的响应缓存：所有 Client 共用一个 InMemoryResponseCacheStore，经
CacheConfig(partition=授权分区, target_id=f"{server_id}#rev={config_rev}") 隔离；server 给的
ttlMs 超过 tools_cache_seconds 时由 _CappedTtlStore 截断。target_id 不放 URL，凭证不会混进缓存键。
"""

import logging
import time
from collections.abc import AsyncIterator, Callable
from contextlib import AsyncExitStack, asynccontextmanager
from dataclasses import dataclass, replace

from mcp import Client
from mcp.client import CacheConfig, CacheEntry, CacheKey, InMemoryResponseCacheStore
from mcp.client.streamable_http import streamable_http_client
from mcp_types import Implementation
from mcp_types import Tool as SdkTool

from harness.adapters.mcp.http import McpAddressError, McpHttpFactory
from harness.adapters.mcp.pool import ConnectionPool, Lease, McpConnectFailed, PoolKey
from harness.domain.mcp import McpSecret, McpServerConfig

logger = logging.getLogger(__name__)

_CLIENT_INFO = Implementation(name="AgentOS", version="1")
# 共享缓存的条目上限；每个 (server, 授权分区) 的工具清单占一条。
_CACHE_MAX_ENTRIES = 4096
# 自动应答 input_required 的轮数：本期不支持交互输入（9.2 节），一轮都不自动应答，直接报错。
_INPUT_REQUIRED_MAX_ROUNDS = 0


class _CappedTtlStore(InMemoryResponseCacheStore):
    """截断 server 给的过长 ttlMs：条目过期时间不超过写入时刻加上限。"""

    def __init__(self, *, max_ttl_seconds: float, clock: Callable[[], float]) -> None:
        super().__init__(max_entries=_CACHE_MAX_ENTRIES)
        self._max_ttl_seconds = max_ttl_seconds
        self._clock = clock

    async def set(self, key: CacheKey, entry: CacheEntry) -> None:
        latest = self._clock() + self._max_ttl_seconds
        if entry.expires_at is not None and entry.expires_at > latest:
            entry = replace(entry, expires_at=latest)
        await super().set(key, entry)


@dataclass(frozen=True)
class McpRuntimeLimits:
    """运行时用到的超时与上限，由组装根从 Settings 读好后传入，适配器内部不读配置。"""

    connect_timeout: float
    idle_seconds: float
    pool_max_connections: int
    tools_cache_seconds: int


class McpRuntime:
    """进程级 MCP 运行时：连接池、共享响应缓存与出站 HTTP 工厂。

    connection() 按 (server_id, config_rev, 授权分区) 借一条连接；invalidate() 让某个 server
    在本进程的连接失效；aclose() 在 lifespan 退出时关闭全部连接。首次使用前不建任何连接。
    """

    def __init__(
        self,
        *,
        http: McpHttpFactory,
        limits: McpRuntimeLimits,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._http = http
        self._limits = limits
        self._clock = clock
        self._cache_store = _CappedTtlStore(
            max_ttl_seconds=limits.tools_cache_seconds,
            clock=clock,
        )
        self._pool: ConnectionPool[Client] = ConnectionPool(
            max_connections=limits.pool_max_connections,
            idle_seconds=limits.idle_seconds,
            connect_timeout=limits.connect_timeout,
        )
        # 每个 (server_id, config_rev, 授权分区) 最近一次成功的工具清单，刷新失败时沿用。
        self._last_good: dict[PoolKey, tuple[SdkTool, ...]] = {}

    @property
    def pool(self) -> ConnectionPool[Client]:
        return self._pool

    @property
    def http(self) -> McpHttpFactory:
        return self._http

    @property
    def connect_timeout(self) -> float:
        return self._limits.connect_timeout

    def cache_config(self, target_id: str, partition: str) -> CacheConfig:
        """共享 store 下的缓存配置；server 没给 ttlMs 时用工具清单缓存时长。"""
        return CacheConfig(
            store=self._cache_store,
            partition=partition,
            target_id=target_id,
            default_ttl_ms=self._limits.tools_cache_seconds * 1000,
            clock=self._clock,
        )

    async def open_client(
        self,
        stack: AsyncExitStack,
        config: McpServerConfig,
        secret: McpSecret | None,
        *,
        cache: CacheConfig | None,
    ) -> Client:
        """建连：SSRF 校验 → 建 httpx2 客户端 → Streamable HTTP → SDK Client，全部压进 stack。

        地址校验不通过属于不可重试的建连失败；其余错误由连接池按建连失败归类。
        """
        try:
            pinned = await self._http.check(config)
        except McpAddressError as exc:
            raise McpConnectFailed(exc.message, retryable=False) from exc
        http_client = await stack.enter_async_context(self._http.create(config, secret, pinned))
        transport = streamable_http_client(config.url, http_client=http_client)
        client = Client(
            transport,
            client_info=_CLIENT_INFO,
            cache=cache,
            input_required_max_rounds=_INPUT_REQUIRED_MAX_ROUNDS,
        )
        return await stack.enter_async_context(client)

    @asynccontextmanager
    async def connection(
        self,
        config: McpServerConfig,
        secret: McpSecret | None,
        *,
        partition: str,
    ) -> AsyncIterator[Lease[Client]]:
        """借用 (server_id, config_rev, partition) 的连接，用完归还；建连失败抛 McpConnectFailed。"""
        server_id = config.require_id()
        key = PoolKey(server_id=server_id, config_rev=config.config_rev, partition=partition)
        cache = self.cache_config(f"{server_id}#rev={config.config_rev}", partition)

        async def opener(stack: AsyncExitStack) -> Client:
            return await self.open_client(stack, config, secret, cache=cache)

        lease = await self._pool.acquire(key, opener)
        try:
            yield lease
        finally:
            self._pool.release(lease)

    def remember_tools(self, key: PoolKey, tools: tuple[SdkTool, ...]) -> None:
        """记下最近一次成功的清单；同一 server 旧 config_rev 的记录不会再被读到，一并清掉。"""
        for old in [k for k in self._last_good if k.server_id == key.server_id]:
            if old.config_rev != key.config_rev:
                del self._last_good[old]
        self._last_good[key] = tools

    def last_good_tools(self, key: PoolKey) -> tuple[SdkTool, ...] | None:
        return self._last_good.get(key)

    def invalidate(self, server_id: int) -> None:
        """本进程内这个 server 的连接与旧清单全部失效；SDK 缓存条目随 config_rev 变化不再被读到。"""
        for key in [k for k in self._last_good if k.server_id == server_id]:
            del self._last_good[key]
        removed = self._pool.invalidate(server_id)
        logger.info(
            "MCP server 连接已失效，server_id=%s，条数=%s",
            server_id,
            removed,
            extra={"mcp_server_id": server_id},
        )

    async def aclose(self) -> None:
        """关闭全部连接，屏蔽外层取消，最多等 3 秒。"""
        await self._pool.aclose()
