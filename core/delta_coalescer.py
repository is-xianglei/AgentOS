"""合并相邻的流式增量,让每条写入 Stream 的事件都值一次写入(规则照 HugAgentOS)。

- 距上次刷新已超过 DELTA_FLUSH_INTERVAL_SECONDS 的增量立即发出,首个 token 不被延迟;
  否则留在缓冲里,直到间隔已到或攒满 DELTA_FLUSH_CHARS 个字符;
- 同一时间只缓冲一路增量:不同路的增量或任何其他事件会先把缓冲刷出,
  合并后的序列与原序列同序,只是相邻同路增量被拼接;
- 「同路」指同一 actor(role/name/run_id/agent_id)同一 block 的 text_delta 或 thinking_delta;
- deadline() 告诉调用方缓冲尾巴何时到期,避免模型沉默时文本滞留在缓冲里。
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import replace
from typing import Any

from core.events import BusEvent, EventType, StreamEvent

DELTA_FLUSH_INTERVAL_SECONDS = 0.05
DELTA_FLUSH_CHARS = 256

_DELTA_TYPES = frozenset({EventType.TEXT_DELTA, EventType.THINKING_DELTA})


def _delta_key(event: BusEvent) -> tuple[Any, ...] | None:
    """增量所属的路;非增量事件返回 None。"""
    if not isinstance(event, StreamEvent) or event.type not in _DELTA_TYPES:
        return None
    actor = event.actor
    index = event.block.index if event.block is not None else None
    return (event.type, actor.role, actor.name, actor.run_id, actor.agent_id, index)


class DeltaCoalescer:
    def __init__(self, *, clock: Callable[[], float] = time.monotonic) -> None:
        self._clock = clock
        self._key: tuple[Any, ...] | None = None
        self._first: StreamEvent | None = None
        self._parts: list[str] = []
        self._size = 0
        self._last_flush = float("-inf")

    def feed(self, event: BusEvent, now: float | None = None) -> list[BusEvent]:
        """接收一个事件,按序返回此刻应写出的事件。"""
        key = _delta_key(event)
        if key is None:
            return [*self.drain(now), event]
        assert isinstance(event, StreamEvent)
        if not event.text:
            return []
        now = self._clock() if now is None else now
        out = self.drain(now) if key != self._key else []
        if self._key is None:
            self._key, self._first = key, event
        self._parts.append(event.text)
        self._size += len(event.text)
        if (
            self._size >= DELTA_FLUSH_CHARS
            or now - self._last_flush >= DELTA_FLUSH_INTERVAL_SECONDS
        ):
            out.extend(self.drain(now))
        return out

    def drain(self, now: float | None = None) -> list[BusEvent]:
        """刷出缓冲中的增量(合并为一个事件)。"""
        if self._key is None or self._first is None:
            return []
        merged = replace(self._first, text="".join(self._parts), sequence=None)
        self._key, self._first = None, None
        self._parts, self._size = [], 0
        self._last_flush = self._clock() if now is None else now
        return [merged]

    def deadline(self) -> float | None:
        """缓冲到期的单调时钟时间;无缓冲时为 None。"""
        if self._key is None:
            return None
        return self._last_flush + DELTA_FLUSH_INTERVAL_SECONDS

    def flush_due(self, now: float | None = None) -> list[BusEvent]:
        """缓冲已到期则刷出。"""
        now = self._clock() if now is None else now
        due = self.deadline()
        return self.drain(now) if due is not None and now >= due else []
