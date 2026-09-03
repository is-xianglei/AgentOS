from typing import Annotated, cast

from fastapi import APIRouter, Depends, Query, Request
from sqlalchemy.ext.asyncio import AsyncSession

from api.deps import get_current_active_user, get_current_workspace_id, get_db
from core.responses import ApiResponse, ok
from user.models import UserRecord
from workflow.models import WorkflowRecord
from workflow.schemas import (
    WorkflowAgentItem,
    WorkflowAgentResponse,
    WorkflowCreateRequest,
    WorkflowDeleteResult,
    WorkflowListResponse,
    WorkflowModeInfo,
    WorkflowResponse,
    WorkflowUpdateRequest,
)
from workflow.service import WorkflowMemberInput, WorkflowService, WorkflowUpdateFields

router = APIRouter()
DatabaseSession = Annotated[AsyncSession, Depends(get_db)]
CurrentUser = Annotated[UserRecord, Depends(get_current_active_user)]
CurrentWorkspaceId = Annotated[int, Depends(get_current_workspace_id)]


def _member_inputs(items: list[WorkflowAgentItem]) -> list[WorkflowMemberInput]:
    return [
        WorkflowMemberInput(agent_id=item.agent_id, sort_order=item.sort_order) for item in items
    ]


def _workflow_response(record: WorkflowRecord) -> WorkflowResponse:
    response = WorkflowResponse.model_validate(record)
    members = sorted(record.agents, key=lambda item: (item.sort_order, item.agent_id))
    return response.model_copy(
        update={
            "agents": [
                WorkflowAgentResponse(
                    agent_id=item.agent_id,
                    agent_name=item.agent.name if item.agent is not None else None,
                    sort_order=item.sort_order,
                )
                for item in members
            ],
        }
    )


@router.post(
    "",
    summary="创建Workflow",
    response_model=ApiResponse[WorkflowResponse],
)
async def create_workflow(
    payload: WorkflowCreateRequest,
    request: Request,
    db: DatabaseSession,
    current_user: CurrentUser,
    workspace_id: CurrentWorkspaceId,
):
    record = await WorkflowService(db).create_workflow(
        workspace_id=workspace_id,
        actor_user_id=current_user.id,
        name=payload.name,
        description=payload.description,
        mode=payload.mode,
        is_enabled=payload.is_enabled,
        members=_member_inputs(payload.agents),
    )
    return ok(_workflow_response(record), request)


@router.get(
    "",
    summary="查询Workflow列表",
    response_model=ApiResponse[WorkflowListResponse],
)
async def list_workflows(
    request: Request,
    db: DatabaseSession,
    current_user: CurrentUser,
    workspace_id: CurrentWorkspaceId,
    keyword: str | None = Query(default=None, max_length=128),
    is_enabled: bool | None = Query(default=None),
    limit: int = Query(default=20, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
):
    page = await WorkflowService(db).list_workflows(
        workspace_id,
        current_user.id,
        keyword=keyword,
        is_enabled=is_enabled,
        limit=limit,
        offset=offset,
    )
    return ok(
        WorkflowListResponse(
            items=[_workflow_response(record) for record in page.items],
            total=page.total,
            limit=page.limit,
            offset=page.offset,
        ),
        request,
    )


# 静态路由必须先于 /{workflow_id} 声明，否则 "modes" 会被当成路径参数解析。
@router.get(
    "/modes",
    summary="查询编排模式元数据",
    response_model=ApiResponse[list[WorkflowModeInfo]],
    dependencies=[Depends(get_current_active_user)],
)
async def list_workflow_modes(request: Request):
    modes = [
        WorkflowModeInfo(mode=meta.mode, name=meta.name, description=meta.description)
        for meta in WorkflowService.list_modes()
    ]
    return ok(modes, request)


@router.get(
    "/{workflow_id}",
    summary="查询Workflow详情",
    response_model=ApiResponse[WorkflowResponse],
)
async def get_workflow(
    workflow_id: int,
    request: Request,
    db: DatabaseSession,
    current_user: CurrentUser,
    workspace_id: CurrentWorkspaceId,
):
    record = await WorkflowService(db).get_workflow(workspace_id, workflow_id, current_user.id)
    return ok(_workflow_response(record), request)


@router.patch(
    "/{workflow_id}",
    summary="更新Workflow",
    response_model=ApiResponse[WorkflowResponse],
)
async def update_workflow(
    workflow_id: int,
    payload: WorkflowUpdateRequest,
    request: Request,
    db: DatabaseSession,
    current_user: CurrentUser,
    workspace_id: CurrentWorkspaceId,
):
    updates = cast(
        WorkflowUpdateFields,
        payload.model_dump(exclude_unset=True, exclude={"agents"}),
    )
    record = await WorkflowService(db).update_workflow(
        workspace_id,
        workflow_id,
        current_user.id,
        updates,
        members=_member_inputs(payload.agents) if payload.agents is not None else None,
    )
    return ok(_workflow_response(record), request)


@router.delete(
    "/{workflow_id}",
    summary="删除Workflow",
    response_model=ApiResponse[WorkflowDeleteResult],
)
async def delete_workflow(
    workflow_id: int,
    request: Request,
    db: DatabaseSession,
    current_user: CurrentUser,
    workspace_id: CurrentWorkspaceId,
):
    await WorkflowService(db).delete_workflow(workspace_id, workflow_id, current_user.id)
    return ok(WorkflowDeleteResult(deleted=True), request)
