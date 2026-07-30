from datetime import datetime

from pydantic import BaseModel, EmailStr, Field


class UserCreateRequest(BaseModel):
    username: str = Field(min_length=3, max_length=64, description="用户名")
    email: EmailStr = Field(description="邮箱")
    password: str = Field(min_length=6, max_length=128, description="密码")
    full_name: str | None = Field(default=None, max_length=128, description="全名")


class UserUpdateRequest(BaseModel):
    full_name: str | None = Field(default=None, max_length=128, description="真实姓名")
    avatar_url: str | None = Field(default=None, max_length=512, description="头像URL")
    phone: str | None = Field(default=None, max_length=32, description="手机号")
    preferences: dict | None = Field(default=None, description="用户偏好设置")


class UserChangePasswordRequest(BaseModel):
    old_password: str = Field(min_length=6, max_length=128, description="旧密码")
    new_password: str = Field(min_length=6, max_length=128, description="新密码")


class UserResponse(BaseModel):
    id: int = Field(description="用户ID")
    username: str = Field(description="用户名")
    email: str = Field(description="邮箱")
    full_name: str | None = Field(default=None, description="真实姓名")
    avatar_url: str | None = Field(default=None, description="头像URL")
    phone: str | None = Field(default=None, description="手机号")
    email_verified: bool = Field(default=False, description="邮箱是否验证")
    suspended: bool = Field(default=False, description="是否被暂停使用")
    auth_provider: str = Field(default="local", description="认证提供商")
    preferences: dict = Field(default_factory=dict, description="用户偏好设置")
    last_login_at: datetime | None = Field(default=None, description="最后登录时间")
    created_at: datetime = Field(description="创建时间")
    updated_at: datetime = Field(description="更新时间")

    model_config = {"from_attributes": True}
