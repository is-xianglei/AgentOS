"""SSE 编码的唯一实现:data 帧、心跳注释行、响应头与 pump 队列。"""

import asyncio
import contextlib
import json
import logging
from collections.abc import AsyncIterator
from typing import Any

from fastapi.responses import StreamingResponse

logger = logging.getLogger(__name__)

SSE_MEDIA_TYPE = "text/event-stream; charset=utf-8"
SSE_HEADERS = {"Cache-Control": "no-cache", "X-Accel-Buffering": "no"}
HEARTBEAT_INTERVAL_SECONDS = 15.0
HEARTBEAT_FRAME = ": heartbeat\n\n"
_QUEUE_SIZE = 64
_END = object()


def format_sse(event: dict[str, Any]) -> str:
    """把事件字典序列化为 SSE 数据帧。"""
    return f"data: {json.dumps(event, ensure_ascii=False, default=str)}\n\n"


async def with_heartbeat(
    events: AsyncIterator[dict[str, Any]],
    *,
    heartbeat_seconds: float = HEARTBEAT_INTERVAL_SECONDS,
) -> AsyncIterator[str]:
    """把事件迭代器编码为 SSE 帧,静默超过心跳间隔时插入注释行供反向代理保活。

    pump 任务加有界队列把读取与写出解耦;客户端断开时只取消 pump(停止跟随),
    后台执行不受影响。
    """
    queue: asyncio.Queue[Any] = asyncio.Queue(maxsize=_QUEUE_SIZE)

    async def pump() -> None:
        try:
            async for event in events:
                await queue.put(event)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            await queue.put(exc)
        else:
            await queue.put(_END)

    task = asyncio.create_task(pump())
    try:
        while True:
            try:
                item = await asyncio.wait_for(queue.get(), heartbeat_seconds)
            except TimeoutError:
                yield HEARTBEAT_FRAME
                continue
            if item is _END:
                return
            if isinstance(item, Exception):
                logger.error(
                    "读取run事件流失败，SSE提前结束，错误类型=%s",
                    type(item).__name__,
                    exc_info=item,
                )
                yield format_sse(
                    {"type": "stream_error", "message": "事件流读取失败，请通过订阅接口重新连接"}
                )
                return
            yield format_sse(item)
    finally:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task
        aclose = getattr(events, "aclose", None)
        if aclose is not None:
            with contextlib.suppress(Exception):
                await aclose()


def sse_response(
    events: AsyncIterator[dict[str, Any]],
    *,
    heartbeat_seconds: float = HEARTBEAT_INTERVAL_SECONDS,
) -> StreamingResponse:
    """统一的 SSE 响应:带禁缓存与禁代理缓冲响应头。"""
    return StreamingResponse(
        with_heartbeat(events, heartbeat_seconds=heartbeat_seconds),
        media_type=SSE_MEDIA_TYPE,
        headers=dict(SSE_HEADERS),
    )
