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
        """要求会话处于 Plan Mode；写入和审批前传 for_update=True 锁住计划行。"""
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
        """进入 Plan Mode。

        还没有进行中的计划时没有行可锁，并发进入最终由部分唯一索引
        uq_session_plans_one_active 兜底。
        """
        if await self.repo.get_active(session_id, for_update=True) is not None:
            raise AgentException.message("当前会话已经处于Plan Mode")
        return await self.repo.create(session_id, turn_id, previous_mode)

    async def write(self, session_id: int, content: str) -> SessionPlanRecord:
        """整体替换计划正文，每次写入版本号加一。"""
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
        """用户拒绝退出，计划保持进行中，记录反馈和可能附带的改稿。"""
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
        """用户批准计划并退出 Plan Mode。"""
        plan = await self.get_active_required(session_id, for_update=True)
        # 用户批准时可能附带改稿，按最终生效的正文判断是否为空。
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
