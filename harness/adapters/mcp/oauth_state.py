"""OAuth 授权进行中的 state 存储：Redis 后端与进程内后端，接口一致（OAuthStateStore 端口）。

Redis key 为 {prefix}:mcp:oauth:state:{sha256(state)}，值是 McpOAuthPending 的 JSON，
SET EX 写入、GETDEL 一次性取出，多实例共享：发起授权与回跳完成可能落在不同 worker 上。key 里不放 state 原文，state 只出现在授权地址里。
disable_redis_events=true 时退回进程内后端，只适合单进程本地调试，与 run 事件流的取舍一致（core/run_events.py）。
Redis 客户端复用 core/redis_client.py 的构造方式，由组装根注入。
"""

import hashlib
import json
import time
from collections.abc import Callable

from core.redis_client import RedisClients
from harness.app.mcp_port import OAuthStateStore
from harness.domain.mcp_oauth import McpOAuthPending

_KEY_PREFIX = "agentos"


def _digest(state: str) -> str:
    return hashlib.sha256(state.encode("utf-8")).hexdigest()


class RedisOAuthStateStore(OAuthStateStore):
    """Redis 后端：SET EX 写入，GETDEL 原子地读出并删除，同一个 state 只能完成一次。"""

    def __init__(self, clients: RedisClients, *, key_prefix: str = _KEY_PREFIX) -> None:
        self._clients = clients
        self._redis = clients.command
        self._prefix = key_prefix

    def key(self, state: str) -> str:
        return f"{self._prefix}:mcp:oauth:state:{_digest(state)}"

    async def put(self, state: str, pending: McpOAuthPending, *, ttl_seconds: int) -> None:
        data = json.dumps(pending.to_dict(), ensure_ascii=False)
        await self._redis.set(self.key(state), data, ex=ttl_seconds)

    async def take(self, state: str) -> McpOAuthPending | None:
        raw = await self._redis.getdel(self.key(state))
        if raw is None:
            return None
        return McpOAuthPending.from_dict(json.loads(raw))

    async def aclose(self) -> None:
        await self._clients.aclose()


class LocalOAuthStateStore(OAuthStateStore):
    """进程内后端（disable_redis_events=true）：字典加过期时间，只适用单进程部署；过期在访问时惰性清理。"""

    def __init__(self, *, clock: Callable[[], float] = time.monotonic) -> None:
        self._clock = clock
        self._entries: dict[str, tuple[float, str]] = {}

    async def put(self, state: str, pending: McpOAuthPending, *, ttl_seconds: int) -> None:
        now = self._clock()
        # 顺手清掉已过期的条目，防止只发起不完成的流程让字典无限增长。
        for key in [k for k, (deadline, _) in self._entries.items() if deadline <= now]:
            del self._entries[key]
        data = json.dumps(pending.to_dict(), ensure_ascii=False)
        self._entries[_digest(state)] = (now + ttl_seconds, data)

    async def take(self, state: str) -> McpOAuthPending | None:
        entry = self._entries.pop(_digest(state), None)
        if entry is None:
            return None
        deadline, data = entry
        if deadline <= self._clock():
            return None
        return McpOAuthPending.from_dict(json.loads(data))

    async def aclose(self) -> None:
        self._entries.clear()
