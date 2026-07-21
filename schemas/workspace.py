from datetime import datetime

from pydantic import BaseModel, Field


# ===== 工作区相关 Schema =====


class WorkspaceCreateRequest(BaseModel):
    name: str = Field(min_length=1, max_length=255, description="工作区名称")
    slug: str = Field(min_length=1, max_length=64, description="工作区标识(URL友好)")
    display_name: str = Field(min_length=1, max_length=255, description="显示名称")
    logo_url: str | None = Field(default=None, max_length=512, description="Logo URL")
    workspace_type: str = Field(
        default="team", description="工作区类型: personal/team/enterprise"
    )
    industry: str | None = Field(default=None, max_length=64, description="所属行业")
    company_size: str | None = Field(default=None, max_length=32, description="公司规模")
    billing_email: str | None = Field(default=None, max_length=255, description="账单邮箱")


class WorkspaceUpdateRequest(BaseModel):
    display_name: str | None = Field(default=None, max_length=255, description="显示名称")
    logo_url: str | None = Field(default=None, max_length=512, description="Logo URL")
    industry: str | None = Field(default=None, max_length=64, description="所属行业")
    company_size: str | None = Field(default=None, max_length=32, description="公司规模")
    billing_email: str | None = Field(default=None, max_length=255, description="账单邮箱")
    settings: dict | None = Field(default=None, description="工作区设置")


class WorkspaceResponse(BaseModel):
    id: int = Field(description="工作区ID")
    name: str = Field(description="工作区名称")
    slug: str = Field(description="工作区标识")
    display_name: str = Field(description="显示名称")
    logo_url: str | None = Field(default=None, description="Logo URL")
    workspace_type: str = Field(description="工作区类型")
    suspended: bool = Field(description="是否被暂停")
    industry: str | None = Field(default=None, description="所属行业")
    company_size: str | None = Field(default=None, description="公司规模")
    billing_email: str | None = Field(default=None, description="账单邮箱")
    plan: str = Field(description="订阅计划")
    quotas: dict = Field(default_factory=dict, description="配额配置")
    settings: dict = Field(default_factory=dict, description="工作区设置")
    created_at: datetime = Field(description="创建时间")
    updated_at: datetime = Field(description="更新时间")

    model_config = {"from_attributes": True}


# ===== 工作区成员相关 Schema =====


class WorkspaceMemberInviteRequest(BaseModel):
    email: str = Field(description="被邀请用户的邮箱")


class WorkspaceMemberResponse(BaseModel):
    id: int = Field(description="成员记录ID")
    workspace_id: int = Field(description="工作区ID")
    user_id: int = Field(description="用户ID")
    invited_by: int | None = Field(default=None, description="邀请人用户ID")
    invited_at: datetime | None = Field(default=None, description="邀请时间")
    joined_at: datetime | None = Field(default=None, description="加入时间")
    created_at: datetime = Field(description="创建时间")
    updated_at: datetime = Field(description="更新时间")

    model_config = {"from_attributes": True}


class WorkspaceMemberDetailResponse(BaseModel):
    """包含用户详细信息的成员响应"""

    id: int = Field(description="成员记录ID")
    workspace_id: int = Field(description="工作区ID")
    user_id: int = Field(description="用户ID")
    username: str = Field(description="用户名")
    email: str = Field(description="邮箱")
    full_name: str | None = Field(default=None, description="全名")
    avatar_url: str | None = Field(default=None, description="头像URL")
    invited_by: int | None = Field(default=None, description="邀请人用户ID")
    invited_at: datetime | None = Field(default=None, description="邀请时间")
    joined_at: datetime | None = Field(default=None, description="加入时间")
    created_at: datetime = Field(description="创建时间")

    model_config = {"from_attributes": True}
