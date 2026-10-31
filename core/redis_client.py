"""Redis 连接构造:普通操作与阻塞读分用两个连接池。

阻塞读(XREAD BLOCK)会长时间占住连接,与 XADD/HSET 共池会让写入排队等待空闲连接;
阻塞池的 socket_timeout 必须大于 BLOCK 时长,否则驱动会在服务端返回前判定超时。
"""

from dataclasses import dataclass

from redis.asyncio import ConnectionPool, Redis

from core.config import settings
from core.errors import AgentException

# 普通命令的读写超时:Redis 故障时让写入方尽快失败,而不是无限挂起执行。
_COMMAND_SOCKET_TIMEOUT_SECONDS = 5.0
# 阻塞读超时在 BLOCK 时长之上留出的余量。
_BLOCKING_TIMEOUT_MARGIN_SECONDS = 5.0


@dataclass(frozen=True)
class RedisClients:
    """一对共享配置、分开连接池的客户端。"""

    command: Redis
    blocking: Redis

    async def aclose(self) -> None:
        """关闭两个客户端及其连接池。"""
        await self.command.aclose(close_connection_pool=True)
        await self.blocking.aclose(close_connection_pool=True)


def build_redis_clients(*, block_ms: int) -> RedisClients:
    """按 Settings 构造客户端;redis_password 非空时覆盖 URL 中的密码。"""
    if not settings.redis_url:
        raise AgentException.message(
            "未配置 AGENTOS_REDIS_URL,且未启用 AGENTOS_DISABLE_REDIS_EVENTS",
        )
    options: dict[str, object] = {
        "decode_responses": True,
        "socket_connect_timeout": _COMMAND_SOCKET_TIMEOUT_SECONDS,
        "health_check_interval": 30,
    }
    if settings.redis_password:
        options["password"] = settings.redis_password
    command_pool = ConnectionPool.from_url(
        settings.redis_url,
        socket_timeout=_COMMAND_SOCKET_TIMEOUT_SECONDS,
        **options,
    )
    blocking_pool = ConnectionPool.from_url(
        settings.redis_url,
        socket_timeout=block_ms / 1000 + _BLOCKING_TIMEOUT_MARGIN_SECONDS,
        **options,
    )
    return RedisClients(
        command=Redis.from_pool(command_pool),
        blocking=Redis.from_pool(blocking_pool),
    )
