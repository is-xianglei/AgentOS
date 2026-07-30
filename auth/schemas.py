from datetime import datetime

from pydantic import BaseModel, EmailStr, Field


class RegisterRequest(BaseModel):
    """用户注册请求"""
    email: EmailStr = Field(description="邮箱")
    username: str = Field(min_length=3, max_length=64, description="用户名")
    password: str = Field(min_length=6, max_length=128, description="密码")
    full_name: str | None = Field(default=None, max_length=128, description="全名")


class LoginRequest(BaseModel):
    """用户登录请求"""
    email: EmailStr = Field(description="邮箱")
    password: str = Field(min_length=6, max_length=128, description="密码")


class TokenResponse(BaseModel):
    """Token 响应"""
    access_token: str = Field(description="访问令牌")
    refresh_token: str = Field(description="刷新令牌")
    token_type: str = Field(default="bearer", description="令牌类型")


class RefreshTokenRequest(BaseModel):
    """刷新 Token 请求"""
    refresh_token: str = Field(description="刷新令牌")


class AuthResponse(BaseModel):
    """认证响应（包含用户信息和 Token）"""
    user: "UserInfo"
    workspace_id: int | None = Field(default=None, description="当前工作区ID")
    access_token: str = Field(description="访问令牌")
    refresh_token: str = Field(description="刷新令牌")
    token_type: str = Field(default="bearer", description="令牌类型")


class UserInfo(BaseModel):
    """用户基本信息"""
    id: int = Field(description="用户ID")
    username: str = Field(description="用户名")
    email: str = Field(description="邮箱")
    full_name: str | None = Field(default=None, description="全名")
    avatar_url: str | None = Field(default=None, description="头像URL")
    phone: str | None = Field(default=None, description="手机号")
    email_verified: bool = Field(default=False, description="邮箱是否验证")
    suspended: bool = Field(default=False, description="是否被暂停使用")
    auth_provider: str = Field(default="local", description="认证提供商")
    last_login_at: datetime | None = Field(default=None, description="最后登录时间")
    created_at: datetime = Field(description="创建时间")

    model_config = {"from_attributes": True}


class SwitchWorkspaceRequest(BaseModel):
    """切换工作区请求"""
    workspace_id: int = Field(description="目标工作区ID")
