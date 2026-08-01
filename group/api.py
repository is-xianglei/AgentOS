from typing import Annotated

from fastapi import APIRouter, Depends, Query, Request
from sqlalchemy.ext.asyncio import AsyncSession

from api.deps import get_current_active_user, get_current_workspace_id, get_db
from core.errors import AgentException
from core.responses import ApiResponse, ok
from group.schemas import (
    GroupCreateRequest,
    GroupJoinResponse,
    GroupMemberInviteRequest,
    GroupMemberResponse,
    GroupMemberRoleUpdateRequest,
    GroupOwnerTransferRequest,
    GroupResponse,
    GroupUpdateRequest,
)
from group.service import GroupService
from user.models import UserRecord

router = APIRouter()
DatabaseSession = Annotated[AsyncSession, Depends(get_db)]
CurrentUser = Annotated[UserRecord, Depends(get_current_active_user)]
CurrentWorkspaceId = Annotated[int, Depends(get_current_workspace_id)]


def _require_path_workspace(workspace_id: int, current_workspace_id: int) -> None:
    if workspace_id != current_workspace_id:
        raise AgentException.message("路径工作区与当前工作区不一致", status_code=403)


def _member_response(member, user_info: dict[str, object]) -> GroupMemberResponse:
    return GroupMemberResponse(
        id=member.id,
        group_id=member.group_id,
        user_id=member.user_id,
        username=str(user_info["username"]),
        email=str(user_info["email"]),
        full_name=user_info["full_name"],
        avatar_url=user_info["avatar_url"],
        role=member.role,
        status=member.status,
        invited_by=member.invited_by,
        joined_at=member.joined_at,
        created_at=member.created_at,
    )


@router.post(
    "/workspaces/{workspace_id}/groups",
    summary="创建群组",
    response_model=ApiResponse[GroupResponse],
)
async def create_group(
    workspace_id: int,
    payload: GroupCreateRequest,
    request: Request,
    db: DatabaseSession,
    current_user: CurrentUser,
    current_workspace_id: CurrentWorkspaceId,
):
    _require_path_workspace(workspace_id, current_workspace_id)
    group = await GroupService(db).create_group(
        workspace_id,
        current_user.id,
        **payload.model_dump(),
    )
    return ok(GroupResponse.model_validate(group), request)


@router.get(
    "/workspaces/{workspace_id}/groups",
    summary="查询群组列表",
    response_model=ApiResponse[list[GroupResponse]],
)
async def list_groups(
    workspace_id: int,
    request: Request,
    db: DatabaseSession,
    current_user: CurrentUser,
    current_workspace_id: CurrentWorkspaceId,
    group_type: str | None = Query(default=None),
    search: str | None = Query(default=None, max_length=128),
):
    _require_path_workspace(workspace_id, current_workspace_id)
    groups = await GroupService(db).list_groups(
        workspace_id,
        current_user.id,
        group_type=group_type,
        search=search,
    )
    return ok([GroupResponse.model_validate(item) for item in groups], request)


@router.get(
    "/workspaces/{workspace_id}/groups/{group_id}",
    summary="查询群组详情",
    response_model=ApiResponse[GroupResponse],
)
async def get_group(
    workspace_id: int,
    group_id: int,
    request: Request,
    db: DatabaseSession,
    current_user: CurrentUser,
    current_workspace_id: CurrentWorkspaceId,
):
    _require_path_workspace(workspace_id, current_workspace_id)
    group = await GroupService(db).get_group(workspace_id, group_id, current_user.id)
    return ok(GroupResponse.model_validate(group), request)


@router.patch(
    "/workspaces/{workspace_id}/groups/{group_id}",
    summary="更新群组",
    response_model=ApiResponse[GroupResponse],
)
async def update_group(
    workspace_id: int,
    group_id: int,
    payload: GroupUpdateRequest,
    request: Request,
    db: DatabaseSession,
    current_user: CurrentUser,
    current_workspace_id: CurrentWorkspaceId,
):
    _require_path_workspace(workspace_id, current_workspace_id)
    group = await GroupService(db).update_group(
        workspace_id,
        group_id,
        current_user.id,
        payload.model_dump(exclude_unset=True),
    )
    return ok(GroupResponse.model_validate(group), request)


@router.delete(
    "/workspaces/{workspace_id}/groups/{group_id}",
    summary="删除群组",
    response_model=ApiResponse[dict],
)
async def delete_group(
    workspace_id: int,
    group_id: int,
    request: Request,
    db: DatabaseSession,
    current_user: CurrentUser,
    current_workspace_id: CurrentWorkspaceId,
):
    _require_path_workspace(workspace_id, current_workspace_id)
    await GroupService(db).delete_group(workspace_id, group_id, current_user.id)
    return ok({"deleted": True}, request)


@router.get(
    "/workspaces/{workspace_id}/groups/{group_id}/members",
    summary="查询群组成员",
    response_model=ApiResponse[list[GroupMemberResponse]],
)
async def list_group_members(
    workspace_id: int,
    group_id: int,
    request: Request,
    db: DatabaseSession,
    current_user: CurrentUser,
    current_workspace_id: CurrentWorkspaceId,
    include_pending: bool = Query(default=False),
):
    _require_path_workspace(workspace_id, current_workspace_id)
    members = await GroupService(db).list_members(
        workspace_id,
        group_id,
        current_user.id,
        include_pending=include_pending,
    )
    return ok([_member_response(member, info) for member, info in members], request)


@router.post(
    "/workspaces/{workspace_id}/groups/{group_id}/members/invite",
    summary="邀请群组成员",
    response_model=ApiResponse[GroupJoinResponse],
)
async def invite_group_member(
    workspace_id: int,
    group_id: int,
    payload: GroupMemberInviteRequest,
    request: Request,
    db: DatabaseSession,
    current_user: CurrentUser,
    current_workspace_id: CurrentWorkspaceId,
):
    _require_path_workspace(workspace_id, current_workspace_id)
    member = await GroupService(db).invite_member(
        workspace_id,
        group_id,
        current_user.id,
        payload.user_id,
        payload.role,
    )
    return ok(GroupJoinResponse.model_validate(member), request)


@router.post(
    "/workspaces/{workspace_id}/groups/{group_id}/members/join",
    summary="加入或申请加入群组",
    response_model=ApiResponse[GroupJoinResponse],
)
async def join_group(
    workspace_id: int,
    group_id: int,
    request: Request,
    db: DatabaseSession,
    current_user: CurrentUser,
    current_workspace_id: CurrentWorkspaceId,
):
    _require_path_workspace(workspace_id, current_workspace_id)
    member = await GroupService(db).join_group(workspace_id, group_id, current_user.id)
    return ok(GroupJoinResponse.model_validate(member), request)


@router.post(
    "/workspaces/{workspace_id}/groups/{group_id}/members/leave",
    summary="退出群组",
    response_model=ApiResponse[dict],
)
async def leave_group(
    workspace_id: int,
    group_id: int,
    request: Request,
    db: DatabaseSession,
    current_user: CurrentUser,
    current_workspace_id: CurrentWorkspaceId,
):
    _require_path_workspace(workspace_id, current_workspace_id)
    await GroupService(db).remove_member(
        workspace_id,
        group_id,
        current_user.id,
        current_user.id,
    )
    return ok({"left": True}, request)


@router.post(
    "/workspaces/{workspace_id}/groups/{group_id}/members/{user_id}/approve",
    summary="批准群组加入申请",
    response_model=ApiResponse[GroupJoinResponse],
)
async def approve_group_member(
    workspace_id: int,
    group_id: int,
    user_id: int,
    request: Request,
    db: DatabaseSession,
    current_user: CurrentUser,
    current_workspace_id: CurrentWorkspaceId,
):
    _require_path_workspace(workspace_id, current_workspace_id)
    member = await GroupService(db).approve_member(
        workspace_id,
        group_id,
        user_id,
        current_user.id,
    )
    return ok(GroupJoinResponse.model_validate(member), request)


@router.delete(
    "/workspaces/{workspace_id}/groups/{group_id}/members/{user_id}/request",
    summary="拒绝群组加入申请",
    response_model=ApiResponse[dict],
)
async def reject_group_member(
    workspace_id: int,
    group_id: int,
    user_id: int,
    request: Request,
    db: DatabaseSession,
    current_user: CurrentUser,
    current_workspace_id: CurrentWorkspaceId,
):
    _require_path_workspace(workspace_id, current_workspace_id)
    await GroupService(db).reject_member(
        workspace_id,
        group_id,
        user_id,
        current_user.id,
    )
    return ok({"rejected": True}, request)


@router.patch(
    "/workspaces/{workspace_id}/groups/{group_id}/members/{user_id}/role",
    summary="更新群组成员角色",
    response_model=ApiResponse[GroupJoinResponse],
)
async def update_group_member_role(
    workspace_id: int,
    group_id: int,
    user_id: int,
    payload: GroupMemberRoleUpdateRequest,
    request: Request,
    db: DatabaseSession,
    current_user: CurrentUser,
    current_workspace_id: CurrentWorkspaceId,
):
    _require_path_workspace(workspace_id, current_workspace_id)
    member = await GroupService(db).update_member_role(
        workspace_id,
        group_id,
        user_id,
        payload.role,
        current_user.id,
    )
    return ok(GroupJoinResponse.model_validate(member), request)


@router.delete(
    "/workspaces/{workspace_id}/groups/{group_id}/members/{user_id}",
    summary="移除群组成员",
    response_model=ApiResponse[dict],
)
async def remove_group_member(
    workspace_id: int,
    group_id: int,
    user_id: int,
    request: Request,
    db: DatabaseSession,
    current_user: CurrentUser,
    current_workspace_id: CurrentWorkspaceId,
):
    _require_path_workspace(workspace_id, current_workspace_id)
    await GroupService(db).remove_member(
        workspace_id,
        group_id,
        user_id,
        current_user.id,
    )
    return ok({"removed": True}, request)


@router.post(
    "/workspaces/{workspace_id}/groups/{group_id}/owner",
    summary="转让群组负责人",
    response_model=ApiResponse[GroupResponse],
)
async def transfer_group_owner(
    workspace_id: int,
    group_id: int,
    payload: GroupOwnerTransferRequest,
    request: Request,
    db: DatabaseSession,
    current_user: CurrentUser,
    current_workspace_id: CurrentWorkspaceId,
):
    _require_path_workspace(workspace_id, current_workspace_id)
    group = await GroupService(db).transfer_owner(
        workspace_id,
        group_id,
        payload.user_id,
        current_user.id,
    )
    return ok(GroupResponse.model_validate(group), request)


@router.get(
    "/users/me/groups",
    summary="查询我在当前工作区加入的群组",
    response_model=ApiResponse[list[GroupResponse]],
)
async def list_my_groups(
    request: Request,
    db: DatabaseSession,
    current_user: CurrentUser,
    workspace_id: CurrentWorkspaceId,
):
    groups = await GroupService(db).list_user_groups(workspace_id, current_user.id)
    return ok([GroupResponse.model_validate(item) for item in groups], request)
