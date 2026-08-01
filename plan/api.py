from typing import Annotated

from fastapi import APIRouter, Depends, Request
from sqlalchemy.ext.asyncio import AsyncSession

from api.deps import get_current_active_user, get_current_workspace_id, get_db
from core.errors import AgentException
from core.responses import ApiResponse, ok
from plan.schemas import SessionPlanResponse
from plan.service import PlanService
from session.service import SessionService
from user.models import UserRecord

router = APIRouter()
DatabaseSession = Annotated[AsyncSession, Depends(get_db)]
CurrentUser = Annotated[UserRecord, Depends(get_current_active_user)]
CurrentWorkspaceId = Annotated[int, Depends(get_current_workspace_id)]


@router.get(
    "/{session_id}",
    summary="查询会话当前或最近的计划",
    response_model=ApiResponse[SessionPlanResponse | None],
)
async def get_session_plan(
    session_id: int,
    request: Request,
    db: DatabaseSession,
    current_user: CurrentUser,
    workspace_id: CurrentWorkspaceId,
):
    session_service = SessionService(db)
    session = await session_service.get_required(session_id)
    if not await session_service.check_access(session, current_user.id, workspace_id):
        raise AgentException.message("无权限访问该会话", status_code=403)

    plan = await PlanService(db).get_latest(session_id)
    return ok(SessionPlanResponse.model_validate(plan) if plan is not None else None, request)
