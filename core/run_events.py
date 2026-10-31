"""run 事件流存储:一个 turn 一条 Stream,Redis Streams 后端与进程内本地后端接口一致。

Redis key 布局:
- {prefix}:run:{turn_id}:events —— Stream,每条 entry 只有 data 字段,值为事件 JSON;
  XADD 时按 MAXLEN≈maxlen 近似裁剪(只会多留,不会少于 maxlen)。
- {prefix}:run:{turn_id}:projection —— Hash,state 字段为折叠后的 TurnState JSON;
  与对应 XADD 放在同一个 MULTI 里写入,保证 projection 永远对齐 Stream 的某个前缀。

游标是 Stream entry id("毫秒-序号"),两个后端语法一致;事件自身的 sequence 是
session_turns.last_event_offset 分配的全局 offset,与游标是两套编号。
写入方由执行租约保证单写者,故不需要 WATCH 乐观锁。
"""

from __future__ import annotations

import asyncio
import json
import time
from collections import deque
from dataclasses import dataclass
from typing import Any, Protocol

from core.config import settings
from core.errors import AgentException
from core.redis_client import RedisClients, build_redis_clients

START_CURSOR = "0-0"
# 订阅方单次阻塞读时长;阻塞连接池的 socket_timeout 据此放大。
STREAM_BLOCK_MS = 5000
READ_BATCH = 200
_KEY_PREFIX = "agentos"

Entry = tuple[str, dict[str, Any]]


def cursor_key(cursor: str) -> tuple[int, int]:
    """把 entry id 转成可比较的元组。"""
    millis, _, seq = cursor.partition("-")
    return int(millis), int(seq or 0)


@dataclass(frozen=True)
class StreamCapture:
    """原子读出的 projection 与 Stream 现存的全部 entry。"""

    projection: dict[str, Any] | None
    entries: list[Entry]

    @property
    def exists(self) -> bool:
        return self.projection is not None or bool(self.entries)

    @property
    def tail_cursor(self) -> str:
        return self.entries[-1][0] if self.entries else START_CURSOR


class RunEventStore(Protocol):
    """run 事件流存储协议;append 的 data / projection 是已序列化的 JSON 文本。"""

    maxlen: int

    async def append(self, turn_id: str, data: str, projection: str | None = None) -> str: ...

    async def capture(self, turn_id: str) -> StreamCapture: ...

    async def wait(
        self,
        turn_id: str,
        after: str,
        *,
        timeout_ms: int,
        count: int = READ_BATCH,
    ) -> list[Entry]: ...

    async def first_cursor(self, turn_id: str) -> str | None: ...

    async def last_cursor(self, turn_id: str) -> str | None: ...

    async def expire(self, turn_id: str, ttl_seconds: float) -> None: ...

    async def persist(self, turn_id: str) -> None: ...

    async def ping(self) -> None: ...

    async def aclose(self) -> None: ...


def _decode(entries: Any) -> list[Entry]:
    decoded: list[Entry] = []
    for cursor, fields in entries or ():
        raw = fields.get("data") if isinstance(fields, dict) else None
        if raw is None:
            continue
        try:
            decoded.append((str(cursor), json.loads(raw)))
        except (TypeError, ValueError):
            continue
    return decoded


class RedisRunEventStore:
    """Redis Streams 后端;客户端由调用方注入,便于测试按事件循环各建一套连接。"""

    def __init__(self, clients: RedisClients, *, maxlen: int, key_prefix: str = _KEY_PREFIX):
        self._clients = clients
        self.maxlen = maxlen
        self._prefix = key_prefix

    def events_key(self, turn_id: str) -> str:
        return f"{self._prefix}:run:{turn_id}:events"

    def projection_key(self, turn_id: str) -> str:
        return f"{self._prefix}:run:{turn_id}:projection"

    async def append(self, turn_id: str, data: str, projection: str | None = None) -> str:
        async with self._clients.command.pipeline(transaction=True) as pipe:
            pipe.xadd(
                self.events_key(turn_id),
                {"data": data},
                maxlen=self.maxlen,
                approximate=True,
            )
            if projection is not None:
                pipe.hset(self.projection_key(turn_id), mapping={"state": projection})
            results = await pipe.execute()
        return str(results[0])

    async def capture(self, turn_id: str) -> StreamCapture:
        async with self._clients.command.pipeline(transaction=True) as pipe:
            pipe.hget(self.projection_key(turn_id), "state")
            pipe.xrange(self.events_key(turn_id), "-", "+")
            raw, entries = await pipe.execute()
        return StreamCapture(json.loads(raw) if raw else None, _decode(entries))

    async def wait(
        self,
        turn_id: str,
        after: str,
        *,
        timeout_ms: int,
        count: int = READ_BATCH,
    ) -> list[Entry]:
        # key 不存在时 XREAD 照常阻塞等待,首个 XADD 创建 key 后即被唤醒。
        result = await self._clients.blocking.xread(
            {self.events_key(turn_id): after},
            count=count,
            block=timeout_ms,
        )
        for _key, entries in result or ():
            return _decode(entries)
        return []

    async def first_cursor(self, turn_id: str) -> str | None:
        entries = await self._clients.command.xrange(self.events_key(turn_id), "-", "+", count=1)
        return str(entries[0][0]) if entries else None

    async def last_cursor(self, turn_id: str) -> str | None:
        entries = await self._clients.command.xrevrange(
            self.events_key(turn_id), "+", "-", count=1
        )
        return str(entries[0][0]) if entries else None

    async def expire(self, turn_id: str, ttl_seconds: float) -> None:
        ttl_ms = max(1, int(ttl_seconds * 1000))
        async with self._clients.command.pipeline(transaction=True) as pipe:
            pipe.pexpire(self.events_key(turn_id), ttl_ms)
            pipe.pexpire(self.projection_key(turn_id), ttl_ms)
            await pipe.execute()

    async def persist(self, turn_id: str) -> None:
        async with self._clients.command.pipeline(transaction=True) as pipe:
            pipe.persist(self.events_key(turn_id))
            pipe.persist(self.projection_key(turn_id))
            await pipe.execute()

    async def ping(self) -> None:
        await self._clients.command.ping()

    async def aclose(self) -> None:
        await self._clients.aclose()


class _LocalLog:
    """单个 turn 的进程内日志;游标在时钟回拨时仍单调。"""

    __slots__ = ("entries", "expires_at", "last_millis", "projection", "seq")

    def __init__(self, maxlen: int) -> None:
        self.entries: deque[Entry] = deque(maxlen=maxlen)
        self.projection: dict[str, Any] | None = None
        self.expires_at: float | None = None
        self.last_millis = 0
        self.seq = 0

    def mint(self) -> str:
        millis = max(int(time.time() * 1000), self.last_millis)
        self.seq = self.seq + 1 if millis == self.last_millis else 0
        self.last_millis = millis
        return f"{millis}-{self.seq}"


class LocalRunEventStore:
    """进程内后端(disable_redis_events=true):deque 加唤醒等待者,仅适用单进程部署。

    写入方与订阅方同在一个事件循环,无需序列化以外的同步手段;过期在访问时惰性清理。
    """

    def __init__(self, *, maxlen: int) -> None:
        self.maxlen = maxlen
        self._logs: dict[str, _LocalLog] = {}
        self._waiters: dict[str, set[asyncio.Event]] = {}

    def _log(self, turn_id: str, *, create: bool) -> _LocalLog | None:
        log = self._logs.get(turn_id)
        if log is not None and log.expires_at is not None and log.expires_at <= time.time():
            self._logs.pop(turn_id, None)
            log = None
        if log is None and create:
            log = self._logs[turn_id] = _LocalLog(self.maxlen)
        return log

    def _since(self, turn_id: str, after: str, count: int) -> list[Entry]:
        log = self._log(turn_id, create=False)
        if log is None:
            return []
        floor = cursor_key(after)
        return [entry for entry in log.entries if cursor_key(entry[0]) > floor][:count]

    async def append(self, turn_id: str, data: str, projection: str | None = None) -> str:
        log = self._log(turn_id, create=True)
        assert log is not None
        cursor = log.mint()
        # 存反序列化后的独立副本,与 Redis 后端一样不和写入方共享可变对象。
        log.entries.append((cursor, json.loads(data)))
        if projection is not None:
            log.projection = json.loads(projection)
        for ready in self._waiters.pop(turn_id, set()):
            ready.set()
        return cursor

    async def capture(self, turn_id: str) -> StreamCapture:
        log = self._log(turn_id, create=False)
        if log is None:
            return StreamCapture(None, [])
        projection = json.loads(json.dumps(log.projection)) if log.projection else None
        return StreamCapture(projection, list(log.entries))

    async def wait(
        self,
        turn_id: str,
        after: str,
        *,
        timeout_ms: int,
        count: int = READ_BATCH,
    ) -> list[Entry]:
        ready = asyncio.Event()
        # 先登记再检查,避免检查与登记之间落下的 append 丢失唤醒。
        self._waiters.setdefault(turn_id, set()).add(ready)
        try:
            found = self._since(turn_id, after, count)
            if found:
                return found
            try:
                await asyncio.wait_for(ready.wait(), timeout_ms / 1000)
            except TimeoutError:
                return []
            return self._since(turn_id, after, count)
        finally:
            waiters = self._waiters.get(turn_id)
            if waiters is not None:
                waiters.discard(ready)
                if not waiters:
                    self._waiters.pop(turn_id, None)

    async def first_cursor(self, turn_id: str) -> str | None:
        log = self._log(turn_id, create=False)
        return log.entries[0][0] if log and log.entries else None

    async def last_cursor(self, turn_id: str) -> str | None:
        log = self._log(turn_id, create=False)
        return log.entries[-1][0] if log and log.entries else None

    async def expire(self, turn_id: str, ttl_seconds: float) -> None:
        log = self._log(turn_id, create=False)
        if log is not None:
            log.expires_at = time.time() + ttl_seconds

    async def persist(self, turn_id: str) -> None:
        log = self._log(turn_id, create=False)
        if log is not None:
            log.expires_at = None

    async def ping(self) -> None:
        return None

    async def aclose(self) -> None:
        return None


_store: RunEventStore | None = None


def get_run_event_store() -> RunEventStore:
    """返回本进程的事件流存储(惰性构造,需在事件循环内首次调用)。

    disable_redis_events=false 且 redis_url 为空时抛中文异常,不静默降级到本地后端。
    """
    global _store
    if _store is None:
        if settings.disable_redis_events:
            _store = LocalRunEventStore(maxlen=settings.run_stream_maxlen)
        elif not settings.redis_url:
            raise AgentException.message(
                "未配置 AGENTOS_REDIS_URL;如需单进程本地事件流,请设置 "
                "AGENTOS_DISABLE_REDIS_EVENTS=true",
            )
        else:
            _store = RedisRunEventStore(
                build_redis_clients(block_ms=STREAM_BLOCK_MS),
                maxlen=settings.run_stream_maxlen,
            )
    return _store


async def check_run_event_store() -> None:
    """启动自检:配置缺失或 Redis 不可达时立即失败,而不是拖到首次对话。"""
    store = get_run_event_store()
    try:
        await store.ping()
    except Exception as exc:
        raise AgentException.message(
            "run 事件流 Redis 不可连接,请检查 AGENTOS_REDIS_URL / AGENTOS_REDIS_PASSWORD",
            {"error_type": type(exc).__name__},
        ) from exc


async def close_run_event_store() -> None:
    """关闭连接池并清空单例;lifespan 退出时调用。"""
    global _store
    if _store is not None:
        store, _store = _store, None
        await store.aclose()
