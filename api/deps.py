from typing import Annotated

import jwt
from fastapi import Depends
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy.ext.asyncio import AsyncSession

from core.config import settings
from core.errors import AgentException
from db.engine import get_db
from user.models import UserRecord
from auth.service import AuthService
from workspace.service import WorkspaceService

security = HTTPBearer(auto_error=False)
SecurityCredentials = Annotated[HTTPAuthorizationCredentials | None, Depends(security)]
DatabaseSession = Annotated[AsyncSession, Depends(get_db)]


async def get_current_user(
    credentials: SecurityCredentials,
    db: DatabaseSession,
) -> UserRecord:
    """获取当前登录用户"""
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
    """获取当前活跃用户"""
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
    try:
        token = credentials.credentials
        payload = jwt.decode(
            token,
            settings.jwt_secret_key,
            algorithms=[settings.jwt_algorithm],
        )
        workspace_id = payload.get("workspace_id")
        if type(workspace_id) is not int:
            raise AgentException.message("Token 中缺少有效的工作区", status_code=401)
    except (jwt.ExpiredSignatureError, jwt.InvalidTokenError) as exc:
        raise AgentException.message("Token 无效或已过期", status_code=401) from exc

    try:
        await WorkspaceService(db).require_active_member(workspace_id, current_user.id)
    except AgentException as exc:
        raise AgentException.message(
            exc.message,
            exc.details,
            status_code=403,
        ) from exc
    return workspace_id


__all__ = ["get_current_active_user", "get_current_user", "get_current_workspace_id", "get_db"]
