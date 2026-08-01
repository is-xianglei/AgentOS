from typing import Annotated

from fastapi import APIRouter, Depends, Query, Request
from sqlalchemy.ext.asyncio import AsyncSession

from api.deps import get_current_active_user, get_current_workspace_id, get_db
from core.errors import AgentException
from core.responses import ApiResponse, ok
from user.models import UserRecord
from workspace.schemas import (
    WorkspaceCreateRequest,
    WorkspaceMemberDetailResponse,
    WorkspaceMemberInviteRequest,
    WorkspaceMemberResponse,
    WorkspaceMemberRoleUpdateRequest,
    WorkspaceResponse,
    WorkspaceUpdateRequest,
)
from workspace.service import WorkspaceService

router = APIRouter()
DatabaseSession = Annotated[AsyncSession, Depends(get_db)]
CurrentUser = Annotated[UserRecord, Depends(get_current_active_user)]
CurrentWorkspaceId = Annotated[int, Depends(get_current_workspace_id)]


def _require_path_workspace(workspace_id: int, current_workspace_id: int) -> None:
    """路径中的租户必须与 Token 的可信工作区一致。"""
    if workspace_id != current_workspace_id:
        raise AgentException.message("路径工作区与当前工作区不一致", status_code=403)


@router.post("", summary="创建工作区", response_model=ApiResponse[WorkspaceResponse])
async def create_workspace(
    payload: WorkspaceCreateRequest,
    request: Request,
    db: DatabaseSession,
    current_user: CurrentUser,
):
    service = WorkspaceService(db)
    workspace = await service.create_workspace(
        creator_user_id=current_user.id,
        name=payload.name,
        slug=payload.slug,
        display_name=payload.display_name,
        workspace_type=payload.workspace_type,
        logo_url=payload.logo_url,
        industry=payload.industry,
        company_size=payload.company_size,
        billing_email=payload.billing_email,
    )
    return ok(WorkspaceResponse.model_validate(workspace), request)


@router.get("", summary="查询我的工作区", response_model=ApiResponse[list[WorkspaceResponse]])
async def list_workspaces(
    request: Request,
    db: DatabaseSession,
    current_user: CurrentUser,
    limit: int = Query(default=100, ge=1, le=1000),
    offset: int = Query(default=0, ge=0),
):
    workspaces = await WorkspaceService(db).list_user_workspaces(current_user.id)
    selected = workspaces[offset : offset + limit]
    return ok([WorkspaceResponse.model_validate(item) for item in selected], request)


@router.get(
    "/{workspace_id}",
    summary="查询工作区详情",
    response_model=ApiResponse[WorkspaceResponse],
)
async def get_workspace(
    workspace_id: int,
    request: Request,
    db: DatabaseSession,
    current_user: CurrentUser,
    current_workspace_id: CurrentWorkspaceId,
):
    _require_path_workspace(workspace_id, current_workspace_id)
    await WorkspaceService(db).require_active_membership(workspace_id, current_user.id)
    workspace = await WorkspaceService(db).get_workspace(workspace_id)
    return ok(WorkspaceResponse.model_validate(workspace), request)


@router.patch(
    "/{workspace_id}",
    summary="更新工作区信息",
    response_model=ApiResponse[WorkspaceResponse],
)
async def update_workspace(
    workspace_id: int,
    payload: WorkspaceUpdateRequest,
    request: Request,
    db: DatabaseSession,
    current_user: CurrentUser,
    current_workspace_id: CurrentWorkspaceId,
):
    _require_path_workspace(workspace_id, current_workspace_id)
    workspace = await WorkspaceService(db).update_workspace(
        workspace_id=workspace_id,
        actor_user_id=current_user.id,
        display_name=payload.display_name,
        logo_url=payload.logo_url,
        industry=payload.industry,
        company_size=payload.company_size,
        billing_email=payload.billing_email,
        settings=payload.settings,
    )
    return ok(WorkspaceResponse.model_validate(workspace), request)


@router.delete("/{workspace_id}", summary="删除工作区", response_model=ApiResponse[dict])
async def delete_workspace(
    workspace_id: int,
    request: Request,
    db: DatabaseSession,
    current_user: CurrentUser,
    current_workspace_id: CurrentWorkspaceId,
):
    _require_path_workspace(workspace_id, current_workspace_id)
    await WorkspaceService(db).delete_workspace(workspace_id, current_user.id)
    return ok({"deleted": True}, request)


@router.get(
    "/{workspace_id}/members",
    summary="查询工作区成员列表",
    response_model=ApiResponse[list[WorkspaceMemberDetailResponse]],
)
async def list_members(
    workspace_id: int,
    request: Request,
    db: DatabaseSession,
    current_user: CurrentUser,
    current_workspace_id: CurrentWorkspaceId,
):
    _require_path_workspace(workspace_id, current_workspace_id)
    members_data = await WorkspaceService(db).list_members(workspace_id, current_user.id)
    members = [
        WorkspaceMemberDetailResponse(
            id=member.id,
            workspace_id=member.workspace_id,
            user_id=member.user_id,
            username=str(user_info["username"]),
            email=str(user_info["email"]),
            full_name=user_info["full_name"],
            avatar_url=user_info["avatar_url"],
            role=member.role,
            department_id=member.department_id,
            job_title=member.job_title,
            invited_by=member.invited_by,
            invited_at=member.invited_at,
            joined_at=member.joined_at,
            created_at=member.created_at,
        )
        for member, user_info in members_data
    ]
    return ok(members, request)


@router.post(
    "/{workspace_id}/members/invite",
    summary="邀请成员加入工作区",
    response_model=ApiResponse[WorkspaceMemberResponse],
)
async def invite_member(
    workspace_id: int,
    payload: WorkspaceMemberInviteRequest,
    request: Request,
    db: DatabaseSession,
    current_user: CurrentUser,
    current_workspace_id: CurrentWorkspaceId,
):
    _require_path_workspace(workspace_id, current_workspace_id)
    member = await WorkspaceService(db).invite_member(
        workspace_id=workspace_id,
        inviter_user_id=current_user.id,
        email=str(payload.email),
        role=payload.role,
    )
    return ok(WorkspaceMemberResponse.model_validate(member), request)


@router.post(
    "/{workspace_id}/members/accept",
    summary="接受工作区邀请",
    response_model=ApiResponse[WorkspaceMemberResponse],
)
async def accept_invite(
    workspace_id: int,
    request: Request,
    db: DatabaseSession,
    current_user: CurrentUser,
):
    member = await WorkspaceService(db).accept_invite(workspace_id, current_user.id)
    return ok(WorkspaceMemberResponse.model_validate(member), request)


@router.patch(
    "/{workspace_id}/members/{user_id}/role",
    summary="更新工作区成员角色",
    response_model=ApiResponse[WorkspaceMemberResponse],
)
async def update_member_role(
    workspace_id: int,
    user_id: int,
    payload: WorkspaceMemberRoleUpdateRequest,
    request: Request,
    db: DatabaseSession,
    current_user: CurrentUser,
    current_workspace_id: CurrentWorkspaceId,
):
    _require_path_workspace(workspace_id, current_workspace_id)
    member = await WorkspaceService(db).update_member_role(
        workspace_id,
        user_id,
        payload.role,
        current_user.id,
    )
    return ok(WorkspaceMemberResponse.model_validate(member), request)


@router.delete(
    "/{workspace_id}/members/{user_id}",
    summary="移除工作区成员",
    response_model=ApiResponse[dict],
)
async def remove_member(
    workspace_id: int,
    user_id: int,
    request: Request,
    db: DatabaseSession,
    current_user: CurrentUser,
    current_workspace_id: CurrentWorkspaceId,
):
    _require_path_workspace(workspace_id, current_workspace_id)
    await WorkspaceService(db).remove_member(workspace_id, user_id, current_user.id)
    return ok({"removed": True}, request)
