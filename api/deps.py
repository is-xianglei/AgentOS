import jwt
from fastapi import Depends
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy.ext.asyncio import AsyncSession

from core.config import settings
from core.errors import AgentException
from db.session import get_db
from models.user import UserRecord
from services.auth_service import AuthService

security = HTTPBearer()


async def get_current_user(
    credentials: HTTPAuthorizationCredentials = Depends(security),
    db: AsyncSession = Depends(get_db),
) -> UserRecord:
    """获取当前登录用户"""
    token = credentials.credentials
    auth_service = AuthService(db)
    return await auth_service.verify_token(token)


async def get_current_active_user(
    current_user: UserRecord = Depends(get_current_user),
) -> UserRecord:
    """获取当前活跃用户"""
    if current_user.is_deleted:
        raise AgentException.message("用户账号已被删除")
    if current_user.suspended:
        raise AgentException.message("用户账号已被禁用")
    return current_user


async def get_current_workspace_id(
    credentials: HTTPAuthorizationCredentials = Depends(security),
) -> int | None:
    """从 Token 中提取当前工作区 ID"""
    try:
        token = credentials.credentials
        payload = jwt.decode(
            token,
            settings.jwt_secret_key,
            algorithms=[settings.jwt_algorithm],
        )
        return payload.get("workspace_id")
    except (jwt.ExpiredSignatureError, jwt.InvalidTokenError):
        return None


__all__ = ["get_db", "get_current_user", "get_current_active_user", "get_current_workspace_id"]
