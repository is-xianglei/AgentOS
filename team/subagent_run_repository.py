from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from team.models import SubAgentRunRecord


class SubAgentRunRepository:
    def __init__(self, db: AsyncSession):
        self.db = db

    async def create(
        self,
        session_id: int,
        member_name: str,
        prompt: str,
        *,
        agent_id: int | None = None,
    ) -> SubAgentRunRecord:
        """建运行记录;agent_id 仅 workflow 成员传入,内置子代理与团队成员留空。"""
        run = SubAgentRunRecord(
            session_id=session_id,
            member_name=member_name,
            agent_id=agent_id,
            status="running",
            prompt=prompt,
        )
        self.db.add(run)
        await self.db.flush()
        await self.db.refresh(run)
        return run

    async def succeed(self, run: SubAgentRunRecord, report: str) -> SubAgentRunRecord:
        run.status = "succeeded"
        run.report = report
        run.finished_at = datetime.now(UTC)
        await self.db.flush()
        await self.db.refresh(run)
        return run

    async def fail(self, run: SubAgentRunRecord, error_message: str) -> SubAgentRunRecord:
        run.status = "failed"
        run.error_message = error_message
        run.finished_at = datetime.now(UTC)
        await self.db.flush()
        await self.db.refresh(run)
        return run

    async def list_by_session(self, session_id: int) -> list[SubAgentRunRecord]:
        stmt = (
            select(SubAgentRunRecord)
            .where(SubAgentRunRecord.session_id == session_id)
            .order_by(SubAgentRunRecord.id)
        )
        return list(await self.db.scalars(stmt))
