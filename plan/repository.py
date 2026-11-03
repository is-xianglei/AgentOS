from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy import desc, select
from sqlalchemy.ext.asyncio import AsyncSession

from plan.models import SessionPlanRecord


class PlanRepository:
    def __init__(self, db: AsyncSession):
        self.db = db

    async def get_active(
        self,
        session_id: int,
        *,
        for_update: bool = False,
    ) -> SessionPlanRecord | None:
        """查询会话进行中的计划；for_update 时加行锁，串行化同一计划的并发写入与审批。"""
        stmt = select(SessionPlanRecord).where(
            SessionPlanRecord.session_id == session_id,
            SessionPlanRecord.status == "active",
            SessionPlanRecord.is_deleted.is_(False),
        )
        if for_update:
            stmt = stmt.with_for_update()
        return (await self.db.scalars(stmt)).first()

    async def get_latest(self, session_id: int) -> SessionPlanRecord | None:
        stmt = (
            select(SessionPlanRecord)
            .where(
                SessionPlanRecord.session_id == session_id,
                SessionPlanRecord.is_deleted.is_(False),
            )
            .order_by(desc(SessionPlanRecord.created_at))
            .limit(1)
        )
        return (await self.db.scalars(stmt)).first()

    async def create(
        self,
        session_id: int,
        turn_id: UUID,
        previous_mode: str,
    ) -> SessionPlanRecord:
        record = SessionPlanRecord(
            session_id=session_id,
            entered_turn_id=turn_id,
            previous_mode=previous_mode,
            status="active",
        )
        self.db.add(record)
        await self.db.flush()
        await self.db.refresh(record)
        return record

    async def update_content(
        self,
        record: SessionPlanRecord,
        content: str,
    ) -> SessionPlanRecord:
        record.content = content
        record.version += 1
        await self.db.flush()
        await self.db.refresh(record)
        return record

    async def record_feedback(
        self,
        record: SessionPlanRecord,
        feedback: str | None,
        edited_plan: str | None,
    ) -> SessionPlanRecord:
        """记录用户反馈；用户改过正文时同时更新正文，内容未变则不递增版本号。"""
        if edited_plan is not None and edited_plan != record.content:
            record.content = edited_plan
            record.version += 1
        record.feedback = feedback
        await self.db.flush()
        await self.db.refresh(record)
        return record

    async def approve(
        self,
        record: SessionPlanRecord,
        turn_id: UUID,
        user_id: int,
        allowed_prompts: list[dict[str, str]],
        feedback: str | None,
        edited_plan: str | None,
    ) -> SessionPlanRecord:
        """批准退出 Plan Mode：先按拒绝时的规则合入反馈与改稿，再记录批准信息。"""
        await self.record_feedback(record, feedback, edited_plan)
        # 批准即退出，两个时间点取同一时刻。
        now = datetime.now(UTC)
        record.status = "approved"
        record.exited_turn_id = turn_id
        record.allowed_prompts = allowed_prompts
        record.approved_by = user_id
        record.approved_at = now
        record.exited_at = now
        await self.db.flush()
        await self.db.refresh(record)
        return record
