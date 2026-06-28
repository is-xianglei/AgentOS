from fastapi import APIRouter, Depends, Request
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_db
from app.core.responses import ok
from app.schemas.common import ApiResponse
from app.schemas.task import TaskResponse
from app.services.task_service import TaskService

router = APIRouter()


@router.get("", summary="查询会话任务", response_model=ApiResponse[list[TaskResponse]])
async def list_tasks(session_id: int, request: Request, db: AsyncSession = Depends(get_db)):
    service = TaskService(db)
    tasks = await service.list_by_session(session_id)
    return ok([TaskResponse.model_validate(item) for item in tasks], request)
