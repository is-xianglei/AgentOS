from fastapi import APIRouter, Depends, Query, Request
from sqlalchemy.ext.asyncio import AsyncSession

from api.deps import get_db
from core.responses import ok
from schemas.common import ApiResponse
from workspace.schemas import (
    WorkspaceCreateRequest,
    WorkspaceMemberDetailResponse,
    WorkspaceMemberInviteRequest,
    WorkspaceMemberResponse,
    WorkspaceResponse,
    WorkspaceUpdateRequest,
)
from workspace.service import WorkspaceService

router = APIRouter()


@router.post("", summary="创建工作区", response_model=ApiResponse[WorkspaceResponse])
async def create_workspace(
    payload: WorkspaceCreateRequest,
    request: Request,
    db: AsyncSession = Depends(get_db),
    creator_user_id: int = Query(..., description="创建者用户ID"),
):
    """创建工作区，并将创建者自动加入为成员"""
    service = WorkspaceService(db)
    workspace = await service.create_workspace(
        creator_user_id=creator_user_id,
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


@router.get("", summary="查询工作区列表", response_model=ApiResponse[list[WorkspaceResponse]])
async def list_workspaces(
    request: Request,
    limit: int = Query(default=100, ge=1, le=1000),
    offset: int = Query(default=0, ge=0),
    db: AsyncSession = Depends(get_db),
):
    """查询所有工作区列表"""
    service = WorkspaceService(db)
    workspaces = await service.list_workspaces(limit=limit, offset=offset)
    return ok([WorkspaceResponse.model_validate(ws) for ws in workspaces], request)


@router.get(
    "/{workspace_id}", summary="查询工作区详情", response_model=ApiResponse[WorkspaceResponse]
)
async def get_workspace(
    workspace_id: int, request: Request, db: AsyncSession = Depends(get_db)
):
    """查询指定工作区的详细信息"""
    service = WorkspaceService(db)
    workspace = await service.get_workspace(workspace_id)
    return ok(WorkspaceResponse.model_validate(workspace), request)


@router.patch(
    "/{workspace_id}", summary="更新工作区信息", response_model=ApiResponse[WorkspaceResponse]
)
async def update_workspace(
    workspace_id: int,
    payload: WorkspaceUpdateRequest,
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    """更新工作区的基本信息和设置"""
    service = WorkspaceService(db)
    workspace = await service.update_workspace(
        workspace_id=workspace_id,
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
    workspace_id: int, request: Request, db: AsyncSession = Depends(get_db)
):
    """删除工作区（软删除）"""
    service = WorkspaceService(db)
    await service.delete_workspace(workspace_id)
    return ok({"deleted": True}, request)


# ===== 成员管理 API =====


@router.get(
    "/{workspace_id}/members",
    summary="查询工作区成员列表",
    response_model=ApiResponse[list[WorkspaceMemberDetailResponse]],
)
async def list_members(
    workspace_id: int, request: Request, db: AsyncSession = Depends(get_db)
):
    """查询工作区的所有成员及其详细信息"""
    service = WorkspaceService(db)
    members_data = await service.list_members(workspace_id)

    # 组装响应数据
    members = []
    for member, user_info in members_data:
        members.append(
            WorkspaceMemberDetailResponse(
                id=member.id,
                workspace_id=member.workspace_id,
                user_id=member.user_id,
                username=user_info["username"],
                email=user_info["email"],
                full_name=user_info["full_name"],
                avatar_url=user_info["avatar_url"],
                invited_by=member.invited_by,
                invited_at=member.invited_at,
                joined_at=member.joined_at,
                created_at=member.created_at,
            )
        )

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
    db: AsyncSession = Depends(get_db),
    inviter_user_id: int = Query(..., description="邀请人用户ID"),
):
    """邀请用户加入工作区"""
    service = WorkspaceService(db)
    member = await service.invite_member(
        workspace_id=workspace_id, inviter_user_id=inviter_user_id, email=payload.email
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
    db: AsyncSession = Depends(get_db),
    user_id: int = Query(..., description="用户ID"),
):
    """用户接受工作区邀请"""
    service = WorkspaceService(db)
    member = await service.accept_invite(workspace_id=workspace_id, user_id=user_id)
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
    db: AsyncSession = Depends(get_db),
    operator_user_id: int = Query(..., description="操作者用户ID"),
):
    """从工作区中移除指定成员"""
    service = WorkspaceService(db)
    await service.remove_member(
        workspace_id=workspace_id, user_id=user_id, operator_user_id=operator_user_id
    )
    return ok({"removed": True}, request)
