from typing import Annotated

from fastapi import APIRouter, Depends, Request
from sqlalchemy.ext.asyncio import AsyncSession

from api.deps import get_current_active_user, get_current_workspace_id, get_db
from core.responses import ApiResponse, ok
from team.schemas import SubAgentRunResponse, TeamMemberResponse, TeamMessageResponse
from team.service import TeamService
from user.models import UserRecord

router = APIRouter()
DatabaseSession = Annotated[AsyncSession, Depends(get_db)]
CurrentUser = Annotated[UserRecord, Depends(get_current_active_user)]
CurrentWorkspaceId = Annotated[int, Depends(get_current_workspace_id)]


@router.get(
    "/members", summary="查询团队成员", response_model=ApiResponse[list[TeamMemberResponse]]
)
async def list_members(
    session_id: int,
    request: Request,
    db: DatabaseSession,
    current_user: CurrentUser,
    workspace_id: CurrentWorkspaceId,
):
    members = await TeamService(db).list_members_for_user(
        session_id,
        current_user.id,
        workspace_id,
    )
    return ok([TeamMemberResponse.model_validate(item) for item in members], request)


@router.get(
    "/messages",
    summary="查询团队消息",
    response_model=ApiResponse[list[TeamMessageResponse]],
)
async def list_team_messages(
    session_id: int,
    request: Request,
    db: DatabaseSession,
    current_user: CurrentUser,
    workspace_id: CurrentWorkspaceId,
):
    messages = await TeamService(db).list_messages_for_user(
        session_id,
        current_user.id,
        workspace_id,
    )
    return ok([TeamMessageResponse.model_validate(item) for item in messages], request)


@router.get(
    "/subagent-runs",
    summary="查询子代理运行记录",
    response_model=ApiResponse[list[SubAgentRunResponse]],
)
async def list_subagent_runs(
    session_id: int,
    request: Request,
    db: DatabaseSession,
    current_user: CurrentUser,
    workspace_id: CurrentWorkspaceId,
):
    runs = await TeamService(db).list_subagent_runs_for_user(
        session_id,
        current_user.id,
        workspace_id,
    )
    return ok([SubAgentRunResponse.model_validate(item) for item in runs], request)
