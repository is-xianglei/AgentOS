import asyncio
from collections.abc import AsyncIterator
from typing import Any

from app.core.events import RuntimeEvent, StreamEvent

# 队列结束哨兵:消费端取到它即停止迭代。
_DONE = object()


class StreamBus:
    """会话级事件总线。

    主代理与所有(含并发)子代理共享同一个实例,各自把事件对象 emit 进来,
    SSE 端通过 stream() 单点消费。基于有界队列 asyncio.Queue,满时 emit 自然背压。
    """

    def __init__(self, maxsize: int = 1000):
        self._queue: asyncio.Queue[Any] = asyncio.Queue(maxsize=maxsize)
        self._sequence = 0
        self._closed = False

    async def emit(self, event: "StreamEvent | RuntimeEvent") -> dict[str, Any]:
        """给事件对象注入全局单调 sequence,序列化为 dict 后入队。

        接受 StreamEvent(协议事件)或 RuntimeEvent(会话控制事件)。sequence 在
        对象上回填,再 to_dict() 序列化。返回带 sequence 的 dict。
        """
        if self._closed:
            return event.to_dict()
        self._sequence += 1
        event.sequence = self._sequence
        payload = event.to_dict()
        await self._queue.put(payload)
        return payload

    async def close(self) -> None:
        """放入结束哨兵,通知消费端停止。幂等。"""
        if self._closed:
            return
        self._closed = True
        await self._queue.put(_DONE)

    async def stream(self) -> AsyncIterator[dict[str, Any]]:
        """从队列中取出事件,直到取到结束哨兵(_DONE)。"""
        while True:
            item = await self._queue.get()
            if item is _DONE:
                return
            yield item
