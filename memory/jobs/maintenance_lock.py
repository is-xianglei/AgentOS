"""保留清理的跨进程互斥：同一时刻只让一个进程执行，并共享扫描游标。

保留清理按 Space 分页扫描、分批物理删除，重复执行只浪费资源，不会破坏数据，属于效率型互斥，
单个 Redis 实例的 SET NX EX 已经足够，不需要 fencing token。
锁的过期时间等于清理间隔：抢到的进程执行完不主动释放，整个周期内其他进程都抢不到，
这样每个周期全集群只清理一次。持锁进程崩溃时，锁到期自动释放，下个周期由其他进程接手。
disable_redis_events=true 时改用进程内实现，只适合单进程本地调试，与 run 事件流的取舍一致。
"""

import time
from typing import Protocol

from core.config import settings
from core.errors import AgentException
from core.redis_client import RedisClients, build_redis_clients

_KEY_PREFIX = "agentos:memory:retention"
# 维护锁只发普通命令，用不到阻塞读；阻塞池按 0 秒 BLOCK 配置，不会被占用。
_BLOCK_MS = 0


class RetentionCoordinator(Protocol):
    """保留清理的周期锁与扫描游标。"""

    async def try_acquire(self, ttl_seconds: int) -> bool:
        """抢本周期的执行权；锁在 ttl_seconds 后过期，期间其他进程都抢不到。"""
        ...

    async def load_cursor(self) -> int:
        """读上一批清理停下的 Space ID；没有记录时从 0 开始。"""
        ...

    async def save_cursor(self, after_space_id: int) -> None:
        """记下本批清理停下的 Space ID，下一批从这里继续。"""
        ...

    async def aclose(self) -> None: ...


class RedisRetentionCoordinator:
    """Redis 后端：锁用 SET NX EX，游标是一个普通字符串键。"""

    def __init__(self, clients: RedisClients, *, key_prefix: str = _KEY_PREFIX) -> None:
        self._clients = clients
        self._redis = clients.command
        self._lock_key = f"{key_prefix}:lock"
        self._cursor_key = f"{key_prefix}:cursor"

    async def try_acquire(self, ttl_seconds: int) -> bool:
        return bool(await self._redis.set(self._lock_key, "1", nx=True, ex=ttl_seconds))

    async def load_cursor(self) -> int:
        raw = await self._redis.get(self._cursor_key)
        if raw is None:
            return 0
        try:
            return max(int(raw), 0)
        except ValueError:
            # 游标只影响扫描起点，读到脏值时从头扫描即可。
            return 0

    async def save_cursor(self, after_space_id: int) -> None:
        await self._redis.set(self._cursor_key, str(after_space_id))

    async def aclose(self) -> None:
        await self._clients.aclose()


class LocalRetentionCoordinator:
    """进程内后端（disable_redis_events=true）：只在本进程内互斥，适合单进程本地调试。"""

    def __init__(self) -> None:
        self._locked_until = 0.0
        self._cursor = 0

    async def try_acquire(self, ttl_seconds: int) -> bool:
        now = time.monotonic()
        if now < self._locked_until:
            return False
        self._locked_until = now + ttl_seconds
        return True

    async def load_cursor(self) -> int:
        return self._cursor

    async def save_cursor(self, after_space_id: int) -> None:
        self._cursor = after_space_id

    async def aclose(self) -> None:
        return None


def build_retention_coordinator() -> RetentionCoordinator:
    """按配置选择后端；未配置 AGENTOS_REDIS_URL 时抛中文异常，不静默退回进程内实现。"""
    if settings.disable_redis_events:
        return LocalRetentionCoordinator()
    if not settings.redis_url:
        raise AgentException.message(
            "未配置 AGENTOS_REDIS_URL，Memory保留清理无法跨进程互斥；"
            "如需单进程本地调试，请设置 AGENTOS_DISABLE_REDIS_EVENTS=true",
        )
    return RedisRetentionCoordinator(build_redis_clients(block_ms=_BLOCK_MS))
