"""会话级事件总线:事件先写入 run 事件流(Redis Stream 或本地后端),SSE 从事件流读取。

StreamBus 是 emit 协议,主代理、子代理、队友、Workflow 成员与 TaskService/TeamService 共用;
RunStreamBus 是一次执行的写入实现;NullStreamBus 供执行之外的运行时(准入、交互取消)使用。
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import time
from collections.abc import Callable
from typing import Protocol

from core.delta_coalescer import DeltaCoalescer
from core.errors import LeaseLostError
from core.events import BusEvent, RunEvent
from core.run_events import RunEventStore
from core.run_state import TurnState, fold

logger = logging.getLogger(__name__)

# projection 写入节奏:每 PROJECTION_INTERVAL 个 offset,以及关键事件。
PROJECTION_INTERVAL = 64
_CHECKPOINT_TYPES = frozenset({"run_started", "run_end", "session_ready", "interaction_request"})
# 写入失败持续发生时,每隔这么多次才再记一条完整异常日志,避免刷屏。
_FAILURE_LOG_EVERY = 100


class StreamBus(Protocol):
    """事件总线的 emit 协议:调用方只管 emit,offset、合并与持久化由实现负责。"""

    async def emit(self, event: BusEvent) -> None: ...


class OffsetSource(Protocol):
    """offset 分配方;租约丢失时抛 LeaseLostError。"""

    async def take(self) -> int: ...


class NullStreamBus:
    """丢弃全部事件:用于没有对应执行、也没有订阅方的运行时实例。"""

    async def emit(self, event: BusEvent) -> None:
        return None


class RunStreamBus:
    """一次执行的写入端:合并增量 → 分配 offset → 折叠 projection → XADD(+HSET)。

    asyncio.Lock 保证「分配 offset 加写入」原子且顺序一致(并行 Workflow 成员会并发 emit);
    事件的 sequence 就是 session_turns.last_event_offset 分配的全局 offset。
    租约丢失后不再写入任何事件,并回调 on_lease_lost 让执行方取消执行。
    """

    def __init__(
        self,
        store: RunEventStore,
        turn_id: str,
        offsets: OffsetSource,
        state: TurnState,
        *,
        on_lease_lost: Callable[[], None] | None = None,
    ) -> None:
        self._store = store
        self._turn_id = turn_id
        self._offsets = offsets
        self._state = state
        self._on_lease_lost = on_lease_lost
        self._coalescer = DeltaCoalescer()
        self._lock = asyncio.Lock()
        self._timer: asyncio.Task[None] | None = None
        self._closed = False
        self._lease_lost = False
        self._checkpoint_offset = state.last_offset
        self._interval = max(1, min(PROJECTION_INTERVAL, store.maxlen // 2))
        self._failures = 0

    @property
    def lease_lost(self) -> bool:
        return self._lease_lost

    @property
    def state(self) -> TurnState:
        return self._state

    async def emit(self, event: BusEvent) -> None:
        if self._closed or self._lease_lost:
            return
        async with self._lock:
            for ready in self._coalescer.feed(event):
                if not await self._write_live(ready):
                    break
        self._arm_timer()

    async def write_terminal(self, event: RunEvent, offset: int) -> None:
        """写入已由持有者在租约内分配好 offset 的 run_end。"""
        async with self._lock:
            await self._append(event, offset)

    async def close(self) -> None:
        """刷出缓冲中的增量并停止计时器;之后的 emit 一律丢弃。幂等。

        先在锁内刷出再停计时器:计时器若正持锁写入,等它写完而不是从中打断。
        """
        async with self._lock:
            if not self._closed and not self._lease_lost:
                for ready in self._coalescer.drain():
                    if not await self._write_live(ready):
                        break
            self._closed = True
        if self._timer is not None:
            self._timer.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._timer
            self._timer = None

    def _arm_timer(self) -> None:
        if self._coalescer.deadline() is None or self._closed or self._lease_lost:
            return
        if self._timer is None or self._timer.done():
            self._timer = asyncio.create_task(self._flush_later())

    async def _flush_later(self) -> None:
        """缓冲尾巴到期后刷出,模型沉默时文本也不会滞留。"""
        while True:
            deadline = self._coalescer.deadline()
            if deadline is None:
                return
            delay = deadline - time.monotonic()
            if delay > 0:
                await asyncio.sleep(delay)
            async with self._lock:
                if self._closed or self._lease_lost:
                    return
                for ready in self._coalescer.flush_due():
                    if not await self._write_live(ready):
                        return

    async def _write_live(self, event: BusEvent) -> bool:
        """分配 offset 后写入;租约丢失返回 False。调用方须持有锁。"""
        try:
            offset = await self._offsets.take()
        except LeaseLostError:
            self.mark_lease_lost()
            return False
        except Exception:
            self._log_failure("分配事件offset失败，本条事件已丢弃")
            return True
        await self._append(event, offset)
        return True

    async def _append(self, event: BusEvent, offset: int) -> None:
        event.sequence = offset
        data = json.dumps(event.to_dict(), ensure_ascii=False, default=str)
        # 折叠用反序列化后的副本,与订阅方从 Stream 读到的形状完全一致(UUID 等已转字符串)。
        payload = json.loads(data)
        # 内存 projection 先折叠:即使本次写入失败,后续 projection 也只会更完整。
        self._state = fold(self._state, payload)
        checkpoint = (
            payload["type"] in _CHECKPOINT_TYPES
            or offset - self._checkpoint_offset >= self._interval
        )
        projection = (
            json.dumps(self._state.to_dict(), ensure_ascii=False) if checkpoint else None
        )
        try:
            await self._store.append(self._turn_id, data, projection)
        except Exception:
            # 数据库是最终事实来源:事件流写失败只影响实时展示,不中断执行。
            self._log_failure("写入run事件流失败，本条事件已丢弃")
            return
        if checkpoint:
            self._checkpoint_offset = offset

    def mark_lease_lost(self) -> None:
        """停止写入并回调 on_lease_lost;重复调用为空操作。"""
        if self._lease_lost:
            return
        self._lease_lost = True
        logger.warning("执行租约已丢失，停止写入run事件流", extra={"turn_id": self._turn_id})
        if self._on_lease_lost is not None:
            self._on_lease_lost()

    def _log_failure(self, message: str) -> None:
        self._failures += 1
        if self._failures == 1 or self._failures % _FAILURE_LOG_EVERY == 0:
            logger.exception(
                message,
                extra={"turn_id": self._turn_id, "failure_count": self._failures},
            )
