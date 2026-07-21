from fastapi import APIRouter, Depends, status
from sqlalchemy.ext.asyncio import AsyncSession

from api.deps import get_current_active_user, get_db
from core.errors import AgentException
from models.user import UserRecord
from schemas.auth import (
    AuthResponse,
    LoginRequest,
    RefreshTokenRequest,
    RegisterRequest,
    SwitchWorkspaceRequest,
    TokenResponse,
    UserInfo,
)
from services.auth_service import AuthService
from services.workspace_service import WorkspaceService

router = APIRouter()


@router.post("/register", response_model=AuthResponse, status_code=status.HTTP_201_CREATED)
async def register(
    payload: RegisterRequest,
    db: AsyncSession = Depends(get_db),
):
    """用户注册"""
    auth_service = AuthService(db)
    result = await auth_service.register(
        email=payload.email,
        username=payload.username,
        password=payload.password,
        full_name=payload.full_name,
    )

    return AuthResponse(
        user=UserInfo.model_validate(result["user"]),
        workspace_id=result.get("workspace_id"),
        access_token=result["access_token"],
        refresh_token=result["refresh_token"],
    )


@router.post("/login", response_model=AuthResponse)
async def login(
    payload: LoginRequest,
    db: AsyncSession = Depends(get_db),
):
    """用户登录"""
    auth_service = AuthService(db)
    result = await auth_service.login(
        email=payload.email,
        password=payload.password,
    )

    return AuthResponse(
        user=UserInfo.model_validate(result["user"]),
        workspace_id=result.get("workspace_id"),
        access_token=result["access_token"],
        refresh_token=result["refresh_token"],
    )


@router.post("/refresh", response_model=TokenResponse)
async def refresh_token(
    payload: RefreshTokenRequest,
    db: AsyncSession = Depends(get_db),
):
    """刷新 Access Token"""
    auth_service = AuthService(db)
    result = await auth_service.refresh_token(payload.refresh_token)

    return TokenResponse(
        access_token=result["access_token"],
        refresh_token=result["refresh_token"],
    )


@router.post("/switch-workspace", response_model=TokenResponse)
async def switch_workspace(
    payload: SwitchWorkspaceRequest,
    current_user: UserRecord = Depends(get_current_active_user),
    db: AsyncSession = Depends(get_db),
):
    """切换工作区并重新生成 Token"""
    # 验证用户是该工作区的成员
    workspace_service = WorkspaceService(db)
    if not await workspace_service.is_member(payload.workspace_id, current_user.id):
        raise AgentException.message("不是该工作区的成员")

    # 生成包含新 workspace_id 的 Token
    auth_service = AuthService(db)
    result = await auth_service.switch_workspace(current_user.id, payload.workspace_id)

    return TokenResponse(
        access_token=result["access_token"],
        refresh_token=result["refresh_token"],
    )

