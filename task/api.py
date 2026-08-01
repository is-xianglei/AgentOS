from typing import Annotated

from fastapi import APIRouter, Depends, Request
from sqlalchemy.ext.asyncio import AsyncSession

from api.deps import get_current_active_user, get_current_workspace_id, get_db
from core.responses import ApiResponse, ok
from task.schemas import TaskResponse
from task.service import TaskService
from user.models import UserRecord

router = APIRouter()
DatabaseSession = Annotated[AsyncSession, Depends(get_db)]
CurrentUser = Annotated[UserRecord, Depends(get_current_active_user)]
CurrentWorkspaceId = Annotated[int, Depends(get_current_workspace_id)]


@router.get("", summary="查询会话任务", response_model=ApiResponse[list[TaskResponse]])
async def list_tasks(
    session_id: int,
    request: Request,
    db: DatabaseSession,
    current_user: CurrentUser,
    workspace_id: CurrentWorkspaceId,
):
    tasks = await TaskService(db).list_by_session_for_user(
        session_id,
        current_user.id,
        workspace_id,
    )
    return ok([TaskResponse.model_validate(item) for item in tasks], request)
