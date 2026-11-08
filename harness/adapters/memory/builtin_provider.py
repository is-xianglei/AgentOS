"""内置记忆 Provider：把应用层的记忆端口接到 memory 域的召回与提取任务上。

召回包装 MemoryRecallService（按回合冻结召回结果，子代理只读继承），回合写入包装
MemoryJobService（在回合终态的事务里登记提取任务）与 MemoryJobRunner（提交后就地执行）。
memory 域内部的 Worker、Dream、保留清理与管理接口属于内置实现自己的功能，不经过这里。
"""

import asyncio
import logging
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from core.config import settings
from harness.app.memory_port import MemoryProvider
from harness.domain.memory import MemoryRecall, MemoryWriteTicket, TurnRef
from memory.jobs.service import MemoryJobRunner, MemoryJobService
from memory.recall.service import MemoryRecallService

logger = logging.getLogger(__name__)


class BuiltinMemoryProvider(MemoryProvider):
    """内置记忆实现，持有调用方的数据库会话；召回与入队都在这个会话的事务里进行。"""

    def __init__(self, db: AsyncSession) -> None:
        self.db = db
        self.recall_service = MemoryRecallService(db)

    async def recall(self, turn: TurnRef) -> MemoryRecall | None:
        """读取或冻结本轮的召回结果：目录进系统指令，正文进本轮用户消息前缀。"""
        context = await self.recall_service.get_or_create_context(
            turn.workspace_id,
            turn.user_id,
            turn.turn_id,
            session_id=turn.session_id,
        )
        if context is None:
            return None
        return MemoryRecall(
            instruction=context.rendered_catalog,
            message_prefix=context.rendered_memories,
        )

    async def inherited(self, turn: TurnRef) -> MemoryRecall | None:
        """只读父回合已冻结的召回结果；父回合须属于同一租户和子代理所在的会话。"""
        context = await self.recall_service.get_frozen_context(
            turn.workspace_id,
            turn.user_id,
            turn.turn_id,
            session_id=turn.session_id,
        )
        if context is None:
            return None
        return MemoryRecall(
            instruction=context.rendered_catalog,
            message_prefix=context.rendered_memories,
        )

    async def on_turn_completed(self, turn: TurnRef) -> MemoryWriteTicket | None:
        """按提取开关幂等登记提取任务；入队与回合终态同一事务，失败只回滚 SAVEPOINT。"""
        if not settings.memory_extraction_enabled:
            return None
        # Memory 是旁路子系统，入队失败不得回滚 Turn 完成与 Session 恢复。
        # 失败语句会让事务进入 aborted，故用 SAVEPOINT 隔离后回滚，
        # 保证调用方随后的 commit 仍能提交主链路状态；本轮记忆则直接放弃。
        nested = await self.db.begin_nested()
        try:
            job = await MemoryJobService(self.db).enqueue_extract(turn.session_id, turn.turn_id)
        except asyncio.CancelledError:
            raise
        except Exception:
            await nested.rollback()
            logger.exception(
                "创建Memory提取任务失败，本轮不提取记忆",
                extra={"session_id": str(turn.session_id), "turn_id": str(turn.turn_id)},
            )
            return None
        await nested.commit()
        logger.info(
            "Memory提取任务已入队",
            extra={
                "session_id": str(turn.session_id),
                "turn_id": str(turn.turn_id),
                "job_id": str(job.id),
                "mode": (job.payload or {}).get("mode"),
            },
        )
        return MemoryWriteTicket(turn=turn, ref=str(job.id))

    async def after_commit(self, ticket: MemoryWriteTicket) -> None:
        """回合终态提交后就地执行提取；失败时由持久 Job 和独立 Worker 接管。"""
        if not settings.memory_extraction_inline:
            return
        job_id = UUID(ticket.ref)
        logger.info(
            "开始就地执行Memory提取任务",
            extra={"job_id": str(job_id)},
        )
        try:
            # 就地执行只认领本回合所属用户的 Job；Runner 自己开短事务，不用调用方的会话。
            scope = (ticket.turn.workspace_id, ticket.turn.user_id)
            if None in scope:
                raise RuntimeError("就地执行Memory任务缺少可信作用域")
            await MemoryJobRunner(claim_scope=scope).run_once(job_id=job_id)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception(
                "就地执行Memory提取任务失败，等待独立Worker接管",
                extra={"job_id": str(job_id)},
            )
