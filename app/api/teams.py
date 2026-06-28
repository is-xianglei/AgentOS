from fastapi import APIRouter, Depends, Request
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_db
from app.core.responses import ok
from app.schemas.common import ApiResponse
from app.schemas.team import SubAgentRunResponse, TeamMemberResponse, TeamMessageResponse
from app.services.team_service import TeamService

router = APIRouter()


@router.get("/members", summary="查询团队成员", response_model=ApiResponse[list[TeamMemberResponse]])
async def list_members(session_id: int, request: Request, db: AsyncSession = Depends(get_db)):
    service = TeamService(db)
    members = await service.list_members(session_id)
    return ok([TeamMemberResponse.model_validate(item) for item in members], request)


@router.get(
    "/messages",
    summary="查询团队消息",
    response_model=ApiResponse[list[TeamMessageResponse]],
)
async def list_team_messages(session_id: int, request: Request, db: AsyncSession = Depends(get_db)):
    service = TeamService(db)
    messages = await service.list_messages(session_id)
    return ok([TeamMessageResponse.model_validate(item) for item in messages], request)


@router.get(
    "/subagent-runs",
    summary="查询子代理运行记录",
    response_model=ApiResponse[list[SubAgentRunResponse]],
)
async def list_subagent_runs(session_id: int, request: Request, db: AsyncSession = Depends(get_db)):
    service = TeamService(db)
    runs = await service.list_subagent_runs(session_id)
    return ok([SubAgentRunResponse.model_validate(item) for item in runs], request)
