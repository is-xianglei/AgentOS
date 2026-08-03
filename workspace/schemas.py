from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, EmailStr, Field

# ===== 工作区相关 Schema =====


class WorkspaceCreateRequest(BaseModel):
    name: str = Field(min_length=1, max_length=255, description="工作区名称")
    slug: str = Field(min_length=1, max_length=64, description="工作区标识(URL友好)")
    display_name: str = Field(min_length=1, max_length=255, description="显示名称")
    logo_url: str | None = Field(default=None, max_length=512, description="Logo URL")
    workspace_type: Literal["personal", "team", "enterprise"] = Field(
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
    settings: dict[str, Any] | None = Field(default=None, description="工作区设置")


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
    quotas: dict[str, Any] = Field(default_factory=dict, description="配额配置")
    settings: dict[str, Any] = Field(default_factory=dict, description="工作区设置")
    membership_role: Literal["owner", "admin", "member"] | None = Field(
        default=None,
        description="当前用户在该工作区中的角色；仅我的工作区列表返回",
    )
    created_at: datetime = Field(description="创建时间")
    updated_at: datetime = Field(description="更新时间")

    model_config = {"from_attributes": True}


# ===== 工作区成员相关 Schema =====


class WorkspaceMemberInviteRequest(BaseModel):
    email: EmailStr = Field(description="被邀请用户的邮箱")
    role: Literal["admin", "member"] = Field(default="member", description="工作区角色")


class WorkspaceMemberRoleUpdateRequest(BaseModel):
    role: Literal["admin", "member"] = Field(description="新的工作区角色")


class WorkspaceMemberResponse(BaseModel):
    id: int = Field(description="成员记录ID")
    workspace_id: int = Field(description="工作区ID")
    user_id: int = Field(description="用户ID")
    role: str = Field(description="工作区角色")
    department_id: int | None = Field(default=None, description="所属部门ID")
    job_title: str | None = Field(default=None, description="职位/岗位")
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
    role: str = Field(description="工作区角色")
    department_id: int | None = Field(default=None, description="所属部门ID")
    job_title: str | None = Field(default=None, description="职位/岗位")
    invited_by: int | None = Field(default=None, description="邀请人用户ID")
    invited_at: datetime | None = Field(default=None, description="邀请时间")
    joined_at: datetime | None = Field(default=None, description="加入时间")
    created_at: datetime = Field(description="创建时间")

    model_config = {"from_attributes": True}
