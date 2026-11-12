"""连接池：按 (server_id, config_rev, auth_partition) 持有 SDK Client，每条连接由一个专属 task 托管。

anyio 要求 async with 的进入与退出在同一个 task 里，所以每条连接起一个托管 task，在里面
``async with`` 建连并一直持有；调用方在自己的 task 里直接用 Client 发请求，等待时同时监控
托管 task，连接在后台断掉时立即得到 McpConnectionLost，而不是等到读超时。

池与 SDK 无关（Client 的类型是泛型参数），容量、LRU 与空闲回收可以脱离 SDK 单测。
所有簿记都是同步代码：asyncio 单线程，两次 await 之间不会被打断，所以不需要锁。
"""

import asyncio
import logging
import math
import time
from collections.abc import Awaitable, Callable
from contextlib import AsyncExitStack
from dataclasses import dataclass

import anyio

from core.errors import AgentException

logger = logging.getLogger(__name__)

# 关闭一条连接（退出 SDK Client 与 HTTP 客户端）的时间上限，防止对端不响应时卡住托管 task。
_CLOSE_GRACE_SECONDS = 3.0


@dataclass(frozen=True)
class PoolKey:
    """连接池的键：不同 config_rev 或授权分区的连接绝不共用。"""

    server_id: int
    config_rev: int
    partition: str


type Opener[C] = Callable[[AsyncExitStack], Awaitable[C]]
"""建连：把需要的资源压进 AsyncExitStack 并返回已连上的 Client；中途失败时由栈清理已建的部分。"""


class McpConnectFailed(Exception):
    """建连阶段失败，请求确实没有发出去。

    retryable 为 False 表示重试也不会成功（地址不合法、server 已不可用等业务错误），
    user_message 是能直接给模型或用户看的中文说明。
    """

    def __init__(self, user_message: str, *, retryable: bool) -> None:
        super().__init__(user_message)
        self.user_message = user_message
        self.retryable = retryable


class McpConnectionLost(Exception):
    """调用进行中连接断开；请求可能已经发出，结果未知，不重试。"""


def root_cause(exc: BaseException) -> BaseException:
    """展开只含一个异常的 ExceptionGroup（SDK 的任务组会这样包装传输错误）。"""
    while isinstance(exc, BaseExceptionGroup) and len(exc.exceptions) == 1:
        exc = exc.exceptions[0]
    return exc


def _connect_failure(exc: BaseException) -> McpConnectFailed:
    """把建连阶段的异常归类成 McpConnectFailed；建连器自己抛出的直接用。"""
    cause = root_cause(exc)
    if isinstance(cause, McpConnectFailed):
        return cause
    if isinstance(cause, TimeoutError):
        return McpConnectFailed("建立连接超时", retryable=True)
    return McpConnectFailed("建立连接失败", retryable=True)


class _Connection[C]:
    """一条连接：托管 task 持有 ``async with`` 的整个生命周期，调用方只借用 client。"""

    def __init__(
        self,
        key: PoolKey,
        opener: Opener[C],
        *,
        connect_timeout: float,
        now: float,
        on_exit: Callable[[_Connection[C]], None],
    ) -> None:
        self.key = key
        self.in_use = 0
        self.last_used = now
        # 被 invalidate 摘下的忙连接：从池里移除，最后一个借用方归还时关闭。
        self.retired = False
        self.closed = asyncio.Event()
        self._stop = asyncio.Event()
        self._opener = opener
        self._connect_timeout = connect_timeout
        self._on_exit = on_exit
        self._ready: asyncio.Future[C] = asyncio.get_running_loop().create_future()
        self.task = asyncio.create_task(self._host(), name=f"mcp-conn-{key.server_id}")

    @property
    def connected(self) -> bool:
        ready = self._ready
        return ready.done() and not ready.cancelled() and ready.exception() is None

    async def wait_ready(self) -> C:
        """等建连完成；等待方被取消不影响建连本身，其他等待方照常拿到结果。"""
        await asyncio.wait({self._ready})
        return self._ready.result()

    def request_close(self) -> None:
        """通知托管 task 退出；还在建连的直接取消。"""
        if self._ready.done():
            self._stop.set()
        else:
            self.task.cancel()

    async def _host(self) -> None:
        context = {"mcp_server_id": self.key.server_id, "mcp_config_rev": self.key.config_rev}
        try:
            # 建连受 connect_timeout 限制；fail_after 必须包住整个 AsyncExitStack，
            # 否则 SDK 在栈里开的 cancel scope 与它交错退出，anyio 会报 scope 嵌套错误。
            with anyio.fail_after(self._connect_timeout) as scope:
                async with AsyncExitStack() as stack:
                    client = await self._opener(stack)
                    scope.deadline = math.inf
                    self._ready.set_result(client)
                    await self._stop.wait()
                    # 关闭对端可能不响应，给退出留一个上限。
                    scope.deadline = anyio.current_time() + _CLOSE_GRACE_SECONDS
        except Exception as exc:
            if not self._ready.done():
                failure = _connect_failure(exc)
                logger.warning(
                    "MCP 建连失败，server_id=%s，原因=%s，错误类型=%s",
                    self.key.server_id,
                    failure.user_message,
                    type(root_cause(exc)).__name__,
                    extra=context,
                )
                self._ready.set_exception(failure)
            elif not self._stop.is_set():
                logger.warning(
                    "MCP 连接在后台断开，server_id=%s，错误类型=%s",
                    self.key.server_id,
                    type(root_cause(exc)).__name__,
                    extra=context,
                )
            else:
                logger.info(
                    "MCP 连接关闭超时，已放弃等待，server_id=%s",
                    self.key.server_id,
                    extra=context,
                )
        finally:
            # 被取消或任何意外退出都要让等待方拿到结果，不能让它们永远挂着。
            if not self._ready.done():
                self._ready.set_exception(McpConnectFailed("MCP 连接已关闭", retryable=False))
            self.closed.set()
            self._on_exit(self)


class Lease[C]:
    """一次借用：client 供调用方发请求，run() 在等待时同时监控托管 task。"""

    def __init__(self, connection: _Connection[C], client: C) -> None:
        self._connection = connection
        self.client = client

    async def run[T](self, operation: Awaitable[T]) -> T:
        """等待 operation，托管 task 先结束（连接断开）就取消它并抛 McpConnectionLost。"""
        operation_task = asyncio.ensure_future(operation)
        closed_task = asyncio.ensure_future(self._connection.closed.wait())
        try:
            await asyncio.wait({operation_task, closed_task}, return_when=asyncio.FIRST_COMPLETED)
        finally:
            # 调用方被取消时两个任务都要收尾，不留后台任务。
            closed_task.cancel()
            if not operation_task.done():
                operation_task.cancel()
                await asyncio.gather(operation_task, return_exceptions=True)
        if operation_task.cancelled():
            raise McpConnectionLost("MCP 连接已断开")
        return operation_task.result()


class ConnectionPool[C]:
    """进程内连接池：容量上限、LRU 淘汰空闲连接、空闲超时回收，全部忙时新请求排队等待。"""

    def __init__(
        self,
        *,
        max_connections: int,
        idle_seconds: float,
        connect_timeout: float,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if max_connections < 1:
            raise AgentException.message("MCP 连接池容量必须大于 0", {"max": max_connections})
        self._max = max_connections
        self._idle_seconds = idle_seconds
        self._connect_timeout = connect_timeout
        self._clock = clock
        self._connections: dict[PoolKey, _Connection[C]] = {}
        # 已摘下但托管 task 还没退出的连接，同样占容量，关闭时也要等它们。
        self._draining: set[_Connection[C]] = set()
        self._freed = asyncio.Event()
        self._closed = False
        self._reaper: asyncio.Task[None] | None = None

    @property
    def size(self) -> int:
        return len(self._connections) + len(self._draining)

    def keys(self) -> list[PoolKey]:
        return list(self._connections)

    async def acquire(self, key: PoolKey, opener: Opener[C]) -> Lease[C]:
        """借一条连接：已有就复用；没有就新建，池满时先按 LRU 淘汰空闲连接，全忙就等。

        排队等空位最多等 connect_timeout，到时抛不可重试的 McpConnectFailed，不无限挂起；
        建连失败抛 McpConnectFailed，失败的连接立即从池里移除，下次重新建。
        """
        deadline = asyncio.get_running_loop().time() + self._connect_timeout
        while True:
            if self._closed:
                raise McpConnectFailed("MCP 运行时已关闭", retryable=False)
            connection = self._connections.get(key)
            if connection is None and self.size >= self._max:
                self._evict_idle()
            if connection is None and self.size < self._max:
                connection = _Connection(
                    key,
                    opener,
                    connect_timeout=self._connect_timeout,
                    now=self._clock(),
                    on_exit=self._on_exit,
                )
                self._connections[key] = connection
                self._ensure_reaper()
            if connection is not None:
                connection.in_use += 1
                try:
                    client = await connection.wait_ready()
                except BaseException:
                    self._release(connection)
                    raise
                return Lease(connection, client)
            self._freed.clear()
            try:
                async with asyncio.timeout_at(deadline):
                    await self._freed.wait()
            except TimeoutError:
                raise McpConnectFailed("MCP 连接数已满，等待空闲连接超时", retryable=False) from None

    def release(self, lease: Lease[C]) -> None:
        """归还借用；被 invalidate 摘下的连接在最后一个借用方归还时关闭。"""
        self._release(lease._connection)

    def _release(self, connection: _Connection[C]) -> None:
        connection.in_use -= 1
        connection.last_used = self._clock()
        if connection.in_use == 0 and connection.retired:
            connection.request_close()
        self._freed.set()

    def invalidate(self, server_id: int) -> int:
        """让某个 server 的全部连接失效：空闲的立即关闭，忙的摘下并在归还后关闭。返回摘下的条数。"""
        removed = [key for key in self._connections if key.server_id == server_id]
        for key in removed:
            self._retire(self._connections[key])
        return len(removed)

    def _retire(self, connection: _Connection[C]) -> None:
        if self._connections.get(connection.key) is connection:
            del self._connections[connection.key]
        self._draining.add(connection)
        connection.retired = True
        if connection.in_use == 0:
            connection.request_close()

    def _evict_idle(self) -> None:
        """池满时淘汰最久未用的一条空闲连接。"""
        idle = [conn for conn in self._connections.values() if conn.in_use == 0]
        if idle:
            self._retire(min(idle, key=lambda conn: conn.last_used))

    def _on_exit(self, connection: _Connection[C]) -> None:
        """托管 task 退出（关闭、断开或建连失败）时从池里移除，空出容量。"""
        if self._connections.get(connection.key) is connection:
            del self._connections[connection.key]
        self._draining.discard(connection)
        self._freed.set()

    def reap_idle(self) -> int:
        """关闭空闲超过 idle_seconds 的连接，正在执行调用的不回收；返回关闭的条数。"""
        deadline = self._clock() - self._idle_seconds
        stale = [
            conn
            for conn in self._connections.values()
            if conn.in_use == 0 and conn.connected and conn.last_used <= deadline
        ]
        for connection in stale:
            self._retire(connection)
        return len(stale)

    def _ensure_reaper(self) -> None:
        if self._reaper is None or self._reaper.done():
            self._reaper = asyncio.create_task(self._reap_loop(), name="mcp-pool-reaper")

    async def _reap_loop(self) -> None:
        """池里有连接时定期回收空闲连接；池空了就退出，下次建连再启动。"""
        interval = max(min(self._idle_seconds / 2, 60.0), 0.01)
        while self._connections:
            await asyncio.sleep(interval)
            self.reap_idle()

    async def aclose(self) -> None:
        """关闭全部连接：屏蔽外层取消，最多等 3 秒，到时未退出的托管 task 直接取消。

        anyio 的 shield 只挡 anyio 自己的取消，asyncio 原生的 task.cancel() 挡不住，
        所以收尾放进独立任务再用 asyncio.shield 等它：调用方被取消，收尾也照样做完。
        """
        self._closed = True
        self._freed.set()
        closing = asyncio.ensure_future(self._close_all())
        with anyio.CancelScope(shield=True):
            try:
                await asyncio.shield(closing)
            except asyncio.CancelledError:
                await asyncio.wait({closing})
                raise

    async def _close_all(self) -> None:
        if self._reaper is not None:
            self._reaper.cancel()
            await asyncio.gather(self._reaper, return_exceptions=True)
        for connection in list(self._connections.values()):
            self._retire(connection)
            connection.request_close()
        tasks = [connection.task for connection in self._draining]
        with anyio.move_on_after(_CLOSE_GRACE_SECONDS):
            await asyncio.gather(*tasks, return_exceptions=True)
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
