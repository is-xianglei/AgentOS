from typing import Annotated

import jwt
from fastapi import Depends
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy.ext.asyncio import AsyncSession

from auth.service import AuthService
from core.config import settings
from core.errors import AgentException
from database.engine import get_db
from harness.app.agent_service import AgentService
from harness.app.mcp_admin import McpAdminService
from harness.bootstrap import default_harness
from user.models import UserRecord
from workspace.service import WorkspaceService

security = HTTPBearer(auto_error=False)
SecurityCredentials = Annotated[HTTPAuthorizationCredentials | None, Depends(security)]
DatabaseSession = Annotated[AsyncSession, Depends(get_db)]


async def get_current_user(
    credentials: SecurityCredentials,
    db: DatabaseSession,
) -> UserRecord:
    """校验 Bearer Access Token 并返回用户；认证失败统一改写为 401，文案沿用原错误。"""
    if credentials is None:
        raise AgentException.message("缺少认证凭据", status_code=401)
    token = credentials.credentials
    auth_service = AuthService(db)
    try:
        return await auth_service.verify_token(token)
    except AgentException as exc:
        raise AgentException.message(
            exc.message,
            exc.details,
            status_code=401,
        ) from exc


async def get_current_active_user(
    current_user: Annotated[UserRecord, Depends(get_current_user)],
) -> UserRecord:
    """要求用户未删除、未停用。

    verify_token 已拒绝这两种用户（报 401）；这里作为纵深防御保留，报 403。
    """
    if current_user.is_deleted:
        raise AgentException.message("用户账号已被删除", status_code=403)
    if current_user.suspended:
        raise AgentException.message("用户账号已被禁用", status_code=403)
    return current_user


async def get_current_workspace_id(
    credentials: SecurityCredentials,
    current_user: Annotated[UserRecord, Depends(get_current_active_user)],
    db: DatabaseSession,
) -> int:
    """解析当前工作区，并实时校验用户仍是有效成员。"""
    if credentials is None:
        raise AgentException.message("缺少认证凭据", status_code=401)
    # 签名与用户状态已由 get_current_user 校验；verify_token 不返回 claims，这里再解码一次取工作区。
    token = credentials.credentials
    try:
        payload = jwt.decode(
            token,
            settings.jwt_secret_key,
            algorithms=[settings.jwt_algorithm],
        )
    except (jwt.ExpiredSignatureError, jwt.InvalidTokenError) as exc:
        raise AgentException.message("Token 无效或已过期", status_code=401) from exc

    # 用 type() 而非 isinstance，避免 JSON 里的 true/false 被当作工作区 ID 1/0。
    workspace_id = payload.get("workspace_id")
    if type(workspace_id) is not int:
        raise AgentException.message("Token 中缺少有效的工作区", status_code=401)

    # 已认证但不再是该工作区的有效成员，按无权访问返回 403。
    try:
        await WorkspaceService(db).require_active_member(workspace_id, current_user.id)
    except AgentException as exc:
        raise AgentException.message(
            exc.message,
            exc.details,
            status_code=403,
        ) from exc
    return workspace_id


async def get_current_admin_user(
    current_user: Annotated[UserRecord, Depends(get_current_active_user)],
    workspace_id: Annotated[int, Depends(get_current_workspace_id)],
    db: DatabaseSession,
) -> UserRecord:
    """校验当前用户是当前工作区的 owner 或 admin。"""
    await WorkspaceService(db).require_workspace_role(
        workspace_id,
        current_user.id,
        {"owner", "admin"},
    )
    return current_user


async def get_current_platform_admin(
    current_user: Annotated[UserRecord, Depends(get_current_active_user)],
) -> UserRecord:
    """校验当前用户是平台管理员；这是平台级权限，不看当前工作区。"""
    if not current_user.is_platform_admin:
        raise AgentException.message("需要平台管理员权限", status_code=403)
    return current_user


async def get_agent_service(
    db: DatabaseSession,
    current_user: Annotated[UserRecord, Depends(get_current_active_user)],
    workspace_id: Annotated[int, Depends(get_current_workspace_id)],
) -> AgentService:
    """按请求经组装根装配 AgentService：运行时绑定本次请求的数据库会话与可信用户、工作区。

    具体的运行时类只在组装根 harness/bootstrap.py 出现，路由只认 AgentService。
    """
    return default_harness().agent_service(
        db,
        user_id=current_user.id,
        workspace_id=workspace_id,
    )


async def get_mcp_admin_service(
    db: DatabaseSession,
    current_user: Annotated[UserRecord, Depends(get_current_active_user)],
    workspace_id: Annotated[int, Depends(get_current_workspace_id)],
) -> McpAdminService:
    """按请求经组装根装配 MCP 管理入口；MCP 总开关关闭时管理接口一律不可用。"""
    if not settings.mcp_enabled:
        raise AgentException.message("MCP 功能未启用（AGENTOS_MCP_ENABLED=false）", status_code=403)
    return default_harness().mcp_admin_service(
        db,
        user_id=current_user.id,
        workspace_id=workspace_id,
    )


__all__ = [
    "get_agent_service",
    "get_current_active_user",
    "get_current_admin_user",
    "get_current_platform_admin",
    "get_current_user",
    "get_current_workspace_id",
    "get_db",
    "get_mcp_admin_service",
]
