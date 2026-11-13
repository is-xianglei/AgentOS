"""McpRuntime：进程级 MCP 运行时，持有连接池与共享响应缓存，提供 aclose()。

缓存直接用 SDK 的响应缓存：所有 Client 共用一个 InMemoryResponseCacheStore，经
CacheConfig(partition=授权分区, target_id=f"{server_id}#rev={config_rev}") 隔离；server 给的
ttlMs 超过 tools_cache_seconds 时由 _CappedTtlStore 截断。target_id 不放 URL，凭证不会混进缓存键。
管理员手动刷新后经 drop_cached_tools() 按 server 清掉缓存条目，下一回合就重新拉取。
OAuth server 的连接挂 McpTokenAuth，token 缓存与刷新归 McpOAuth（oauth.py），由组装根注入。
"""

import hashlib
import logging
import time
from collections.abc import AsyncIterator, Callable
from contextlib import AsyncExitStack, asynccontextmanager
from dataclasses import dataclass, replace

import httpx2
from mcp import Client
from mcp.client import CacheConfig, CacheEntry, CacheKey, InMemoryResponseCacheStore
from mcp.client.streamable_http import streamable_http_client
from mcp_types import Implementation
from mcp_types import Tool as SdkTool

from core.errors import AgentException
from harness.adapters.mcp.http import McpAddressError, McpHttpFactory
from harness.adapters.mcp.oauth import McpOAuth
from harness.adapters.mcp.pool import ConnectionPool, Lease, McpConnectFailed, PoolKey
from harness.domain.mcp import McpSecret, McpServerConfig, user_partition

logger = logging.getLogger(__name__)

_CLIENT_INFO = Implementation(name="AgentOS", version="1")
# 共享缓存的条目上限；每个 (server, 授权分区) 的工具清单占一条。
_CACHE_MAX_ENTRIES = 4096
# 自动应答 input_required 的轮数：本期不支持交互输入（9.2 节），一轮都不自动应答，直接报错。
_INPUT_REQUIRED_MAX_ROUNDS = 0


def _arm_id(target_id: str) -> str:
    """SDK 由 target_id 派生的缓存臂标识（client/client.py：sha256(target_id) 的十六进制）。

    SDK 把它与授权分区等字段一起编进 CacheKey.partition（client/caching.py 的 _arm），
    所以按 server 清条目时只需看 partition 里是否含有它，不解析 SDK 的编码格式。
    """
    return hashlib.sha256(target_id.encode()).hexdigest()


class _CappedTtlStore(InMemoryResponseCacheStore):
    """截断 server 给的过长 ttlMs：条目过期时间不超过写入时刻加上限；另记下写过的键，供按 server 清除。"""

    def __init__(self, *, max_ttl_seconds: float, clock: Callable[[], float]) -> None:
        super().__init__(max_entries=_CACHE_MAX_ENTRIES)
        self._max_ttl_seconds = max_ttl_seconds
        self._clock = clock
        # 写过的键，按写入先后排列；与父类同一上限，超出时丢最早的一条（父类多半也已淘汰它，
        # 没淘汰的只是清不到、仍按 TTL 过期）。
        self._written: dict[CacheKey, None] = {}

    async def set(self, key: CacheKey, entry: CacheEntry) -> None:
        latest = self._clock() + self._max_ttl_seconds
        if entry.expires_at is not None and entry.expires_at > latest:
            entry = replace(entry, expires_at=latest)
        await super().set(key, entry)
        self._written.pop(key, None)
        self._written[key] = None
        if len(self._written) > _CACHE_MAX_ENTRIES:
            del self._written[next(iter(self._written))]

    async def delete(self, key: CacheKey) -> None:
        await super().delete(key)
        self._written.pop(key, None)

    async def clear(self) -> None:
        await super().clear()
        self._written.clear()

    async def evict_arms(self, arm_ids: set[str]) -> int:
        """删掉属于这些缓存臂（即这些 target_id）的全部条目，不分授权分区与协议版本；返回条数。"""
        doomed = [key for key in self._written if any(arm in key.partition for arm in arm_ids)]
        for key in doomed:
            await self.delete(key)
        return len(doomed)


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
    oauth 为 None 表示本装配不支持 OAuth server，遇到时抛中文异常，不回落成无认证连接。
    """

    def __init__(
        self,
        *,
        http: McpHttpFactory,
        limits: McpRuntimeLimits,
        oauth: McpOAuth | None = None,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._http = http
        self._limits = limits
        self._oauth = oauth
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
        # 每个 server 用过的缓存 target_id（按 config_rev 不同各一个），按 server 清缓存时据此找条目。
        self._cache_targets: dict[int, set[str]] = {}

    @property
    def pool(self) -> ConnectionPool[Client]:
        return self._pool

    @property
    def http(self) -> McpHttpFactory:
        return self._http

    @property
    def connect_timeout(self) -> float:
        return self._limits.connect_timeout

    @property
    def oauth(self) -> McpOAuth:
        """OAuth 服务；本装配没有提供时抛中文异常（调用方把它记成该 server 的故障）。"""
        if self._oauth is None:
            raise AgentException.message("当前装配未提供 MCP OAuth 支持，无法连接 OAuth server")
        return self._oauth

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
        auth: httpx2.Auth | None = None,
    ) -> Client:
        """建连：SSRF 校验 → 建 httpx2 客户端（OAuth server 挂 auth）→ Streamable HTTP → SDK Client，全部压进 stack。

        地址校验不通过属于不可重试的建连失败；其余错误由连接池按建连失败归类。
        """
        try:
            pinned = await self._http.check(config)
        except McpAddressError as exc:
            raise McpConnectFailed(exc.message, retryable=False) from exc
        http_client = await stack.enter_async_context(
            self._http.create(config, secret, pinned, auth=auth),
        )
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
        auth: httpx2.Auth | None = None,
    ) -> AsyncIterator[Lease[Client]]:
        """借用 (server_id, config_rev, partition) 的连接，用完归还；建连失败抛 McpConnectFailed。

        auth 只在新建连接时挂上；OAuth 的分区按用户划分，复用到的连接挂的就是同一个用户的认证插件。
        """
        server_id = config.require_id()
        key = PoolKey(server_id=server_id, config_rev=config.config_rev, partition=partition)
        target_id = f"{server_id}#rev={config.config_rev}"
        self._cache_targets.setdefault(server_id, set()).add(target_id)
        cache = self.cache_config(target_id, partition)

        async def opener(stack: AsyncExitStack) -> Client:
            if auth is None:
                return await self.open_client(stack, config, secret, cache=cache)
            return await self.open_client(stack, config, secret, cache=cache, auth=auth)

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

    async def drop_cached_tools(self, server_id: int) -> int:
        """清掉本进程里这个 server 的工具清单缓存：SDK 响应缓存条目（全部授权分区）与最近成功清单。

        管理员手动刷新探测成功后调用，下一回合解析时重新拉取，不用等 TTL 过期；连接与 config_rev 不动。
        只作用于本进程，其他实例仍按 mcp_tools_cache_seconds 过期。返回清掉的缓存条目数。
        """
        for key in [k for k in self._last_good if k.server_id == server_id]:
            del self._last_good[key]
        arms = {_arm_id(target) for target in self._cache_targets.get(server_id, ())}
        removed = await self._cache_store.evict_arms(arms) if arms else 0
        logger.info(
            "MCP 工具清单缓存已清除，server_id=%s，条数=%s",
            server_id,
            removed,
            extra={"mcp_server_id": server_id},
        )
        return removed

    def forget_oauth_user(self, server_id: int, user_id: int) -> None:
        """撤销授权后：这个用户在这个 server 上的 token 缓存、连接与最近成功清单作废，别的用户不受影响。"""
        if self._oauth is not None:
            self._oauth.forget_user(server_id, user_id)
        partition = user_partition(user_id)
        stale = [k for k in self._last_good if (k.server_id, k.partition) == (server_id, partition)]
        for key in stale:
            del self._last_good[key]
        self._pool.invalidate(server_id, partition=partition)

    def invalidate(self, server_id: int) -> None:
        """本进程内这个 server 的连接与旧清单全部失效；SDK 缓存条目随 config_rev 变化不再被读到。"""
        for key in [k for k in self._last_good if k.server_id == server_id]:
            del self._last_good[key]
        if self._oauth is not None:
            self._oauth.forget_server(server_id)
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
