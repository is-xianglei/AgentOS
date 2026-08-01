from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from core.errors import AgentException
from plan.models import SessionPlanRecord
from plan.repository import PlanRepository


class PlanService:
    def __init__(self, db: AsyncSession):
        self.db = db
        self.repo = PlanRepository(db)

    async def get_active(self, session_id: int) -> SessionPlanRecord | None:
        return await self.repo.get_active(session_id)

    async def get_latest(self, session_id: int) -> SessionPlanRecord | None:
        return await self.repo.get_latest(session_id)

    async def get_active_required(
        self,
        session_id: int,
        *,
        for_update: bool = False,
    ) -> SessionPlanRecord:
        plan = await self.repo.get_active(session_id, for_update=for_update)
        if plan is None:
            raise AgentException.message("当前会话未处于Plan Mode")
        return plan

    async def enter(
        self,
        session_id: int,
        turn_id: UUID,
        previous_mode: str = "default",
    ) -> SessionPlanRecord:
        if await self.repo.get_active(session_id, for_update=True) is not None:
            raise AgentException.message("当前会话已经处于Plan Mode")
        return await self.repo.create(session_id, turn_id, previous_mode)

    async def write(self, session_id: int, content: str) -> SessionPlanRecord:
        content = content.strip()
        if not content:
            raise AgentException.message("计划正文不能为空")
        if len(content) > 500_000:
            raise AgentException.message("计划正文不能超过500000字符")
        plan = await self.get_active_required(session_id, for_update=True)
        return await self.repo.update_content(plan, content)

    async def reject_exit(
        self,
        session_id: int,
        feedback: str | None,
        edited_plan: str | None,
    ) -> SessionPlanRecord:
        plan = await self.get_active_required(session_id, for_update=True)
        return await self.repo.record_feedback(plan, feedback, edited_plan)

    async def approve_exit(
        self,
        session_id: int,
        turn_id: UUID,
        user_id: int,
        allowed_prompts: list[dict[str, str]],
        feedback: str | None,
        edited_plan: str | None,
    ) -> SessionPlanRecord:
        plan = await self.get_active_required(session_id, for_update=True)
        final_content = edited_plan if edited_plan is not None else plan.content
        if not final_content.strip():
            raise AgentException.message("计划正文为空，不能退出Plan Mode")
        return await self.repo.approve(
            plan,
            turn_id,
            user_id,
            allowed_prompts,
            feedback,
            edited_plan,
        )
