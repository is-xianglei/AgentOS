from typing import Annotated

from fastapi import APIRouter, Depends, Query, Request
from sqlalchemy.ext.asyncio import AsyncSession

from api.deps import get_current_active_user, get_current_workspace_id, get_db
from core.responses import ApiResponse, ok
from tools.schemas import ToolDetailResponse, ToolListResponse, ToolResponse
from tools.service import ToolCatalogService
from user.models import UserRecord

router = APIRouter()
DatabaseSession = Annotated[AsyncSession, Depends(get_db)]
CurrentUser = Annotated[UserRecord, Depends(get_current_active_user)]
CurrentWorkspaceId = Annotated[int, Depends(get_current_workspace_id)]
PageLimit = Annotated[int, Query(ge=1, le=100)]
PageOffset = Annotated[int, Query(ge=0)]


@router.get("", summary="查询内置工具目录", response_model=ApiResponse[ToolListResponse])
async def list_tools(
    request: Request,
    db: DatabaseSession,
    current_user: CurrentUser,
    workspace_id: CurrentWorkspaceId,
    keyword: str | None = Query(default=None, max_length=120),
    is_enabled: bool | None = Query(default=True),
    limit: PageLimit = 50,
    offset: PageOffset = 0,
):
    page = await ToolCatalogService(db).list_tools(
        keyword=keyword,
        is_enabled=is_enabled,
        limit=limit,
        offset=offset,
    )
    data = ToolListResponse(
        items=[ToolResponse.model_validate(record) for record in page.items],
        total=page.total,
        limit=page.limit,
        offset=page.offset,
    )
    return ok(data, request)


@router.get(
    "/{tool_id}",
    summary="查询内置工具详情",
    response_model=ApiResponse[ToolDetailResponse],
)
async def get_tool(
    tool_id: int,
    request: Request,
    db: DatabaseSession,
    current_user: CurrentUser,
    workspace_id: CurrentWorkspaceId,
):
    record = await ToolCatalogService(db).get_required(tool_id)
    return ok(ToolDetailResponse.model_validate(record), request)
