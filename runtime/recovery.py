"""孤儿执行回收:启动时执行一次,之后按 run_reaper_interval_seconds 周期执行。

扫描 lease_owner 非空且已过期的轮次(持有者进程已崩溃或失联);启动时另回收
status=running 且 lease_owner 为空的迁移前遗留轮次。回收只收尾,不重跑。
"""

import asyncio
import logging

from core.config import settings
from runtime.execution import run_in_session, write_detached_run_end

logger = logging.getLogger(__name__)


async def reap_orphan_turns(*, include_unleased_running: bool = False) -> int:
    """回收全部孤儿轮次,返回回收数;每个轮次独立事务,单个失败不影响其余。"""
    turn_ids = await run_in_session(
        lambda service: service.list_reapable_turn_ids(
            include_unleased_running=include_unleased_running,
        )
    )
    reaped = 0
    for turn_id in turn_ids:
        try:
            termination = await run_in_session(
                lambda service, turn_id=turn_id: service.reap_turn(
                    turn_id,
                    include_unleased_running=include_unleased_running,
                ),
                commit=True,
            )
        except Exception:
            logger.exception("回收孤儿轮次失败", extra={"turn_id": str(turn_id)})
            continue
        if termination is None:
            continue
        await write_detached_run_end(termination, cancelled=False)
        reaped += 1
        logger.warning(
            "已回收孤儿轮次",
            extra={
                "turn_id": str(turn_id),
                "session_id": termination.session_id,
                "status": termination.status,
                "session_status": termination.session_status,
            },
        )
    return reaped


async def run_reaper_loop(interval_seconds: float | None = None) -> None:
    """周期回收;单轮失败只记日志,循环不退出。"""
    interval = interval_seconds or settings.run_reaper_interval_seconds
    while True:
        await asyncio.sleep(interval)
        try:
            await reap_orphan_turns()
        except Exception:
            logger.exception("孤儿轮次回收轮次执行失败")
