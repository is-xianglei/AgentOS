from fastapi import APIRouter, Depends, Query, Request
from sqlalchemy.ext.asyncio import AsyncSession

from api.deps import get_current_active_user, get_db
from core.responses import ok
from user.models import UserRecord
from schemas.common import ApiResponse
from user.schemas import (
    UserChangePasswordRequest,
    UserCreateRequest,
    UserResponse,
    UserUpdateRequest,
)
from workspace.schemas import WorkspaceResponse
from user.service import UserService
from workspace.service import WorkspaceService

router = APIRouter()


@router.get("/me", summary="获取当前用户信息", response_model=ApiResponse[UserResponse])
async def get_current_user_info(
    request: Request,
    current_user: UserRecord = Depends(get_current_active_user),
):
    """获取当前登录用户的信息"""
    return ok(UserResponse.model_validate(current_user), request)


@router.get("/me/workspaces", summary="获取当前用户的工作区列表", response_model=ApiResponse[list[WorkspaceResponse]])
async def get_current_user_workspaces(
    request: Request,
    current_user: UserRecord = Depends(get_current_active_user),
    db: AsyncSession = Depends(get_db),
):
    """获取当前用户加入的所有工作区列表"""
    workspace_service = WorkspaceService(db)
    workspaces = await workspace_service.list_user_workspaces(current_user.id)
    return ok([WorkspaceResponse.model_validate(ws) for ws in workspaces], request)


@router.patch("/me", summary="更新当前用户信息", response_model=ApiResponse[UserResponse])
async def update_current_user(
    payload: UserUpdateRequest,
    request: Request,
    current_user: UserRecord = Depends(get_current_active_user),
    db: AsyncSession = Depends(get_db),
):
    """更新当前登录用户的信息"""
    service = UserService(db)
    user = await service.update_user(
        user_id=current_user.id,
        full_name=payload.full_name,
        avatar_url=payload.avatar_url,
    )
    return ok(UserResponse.model_validate(user), request)


@router.post("/me/change-password", summary="修改当前用户密码", response_model=ApiResponse[UserResponse])
async def change_current_user_password(
    payload: UserChangePasswordRequest,
    request: Request,
    current_user: UserRecord = Depends(get_current_active_user),
    db: AsyncSession = Depends(get_db),
):
    """修改当前登录用户的密码"""
    service = UserService(db)
    user = await service.change_password(
        user_id=current_user.id,
        old_password=payload.old_password,
        new_password=payload.new_password,
    )
    return ok(UserResponse.model_validate(user), request)


@router.delete("/me", summary="删除当前用户", response_model=ApiResponse[dict])
async def delete_current_user(
    request: Request,
    current_user: UserRecord = Depends(get_current_active_user),
    db: AsyncSession = Depends(get_db),
):
    """删除当前登录用户（软删除）"""
    service = UserService(db)
    await service.delete_user(current_user.id)
    return ok({"deleted": True}, request)


@router.post("", summary="创建用户", response_model=ApiResponse[UserResponse])
async def create_user(
    payload: UserCreateRequest, request: Request, db: AsyncSession = Depends(get_db)
):
    service = UserService(db)
    user = await service.create_user(
        username=payload.username,
        email=payload.email,
        password=payload.password,
        full_name=payload.full_name,
    )
    return ok(UserResponse.model_validate(user), request)


@router.get("", summary="查询用户列表", response_model=ApiResponse[list[UserResponse]])
async def list_users(
    request: Request,
    limit: int = Query(default=100, ge=1, le=1000),
    offset: int = Query(default=0, ge=0),
    db: AsyncSession = Depends(get_db),
):
    service = UserService(db)
    users = await service.list_users(limit=limit, offset=offset)
    return ok([UserResponse.model_validate(user) for user in users], request)


@router.get("/{user_id}", summary="查询用户详情", response_model=ApiResponse[UserResponse])
async def get_user(user_id: int, request: Request, db: AsyncSession = Depends(get_db)):
    service = UserService(db)
    user = await service.get_user(user_id)
    return ok(UserResponse.model_validate(user), request)


@router.patch("/{user_id}", summary="更新用户信息", response_model=ApiResponse[UserResponse])
async def update_user(
    user_id: int,
    payload: UserUpdateRequest,
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    service = UserService(db)
    user = await service.update_user(
        user_id=user_id,
        full_name=payload.full_name,
        avatar_url=payload.avatar_url,
        bio=payload.bio,
        extra_data=payload.extra_data,
    )
    return ok(UserResponse.model_validate(user), request)


@router.post(
    "/{user_id}/change-password",
    summary="修改密码",
    response_model=ApiResponse[UserResponse],
)
async def change_password(
    user_id: int,
    payload: UserChangePasswordRequest,
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    service = UserService(db)
    user = await service.change_password(
        user_id=user_id,
        old_password=payload.old_password,
        new_password=payload.new_password,
    )
    return ok(UserResponse.model_validate(user), request)


@router.delete("/{user_id}", summary="删除用户", response_model=ApiResponse[dict])
async def delete_user(user_id: int, request: Request, db: AsyncSession = Depends(get_db)):
    service = UserService(db)
    await service.delete_user(user_id)
    return ok({"deleted": True}, request)
