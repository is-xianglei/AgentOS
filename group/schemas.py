from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field

GroupType = Literal["team", "community", "region", "custom"]
GroupVisibility = Literal["workspace", "private", "public"]
GroupJoinMode = Literal["invite", "open", "approval"]


class GroupCreateRequest(BaseModel):
    name: str = Field(min_length=1, max_length=128, description="群组名称")
    slug: str = Field(min_length=1, max_length=64, pattern=r"^[a-z0-9][a-z0-9-]*$")
    description: str | None = Field(default=None, description="群组描述")
    avatar_url: str | None = Field(default=None, max_length=512, description="群组头像URL")
    group_type: GroupType = Field(default="team", description="群组类型")
    visibility: GroupVisibility = Field(default="workspace", description="可见性")
    join_mode: GroupJoinMode = Field(default="invite", description="加入方式")
    settings: dict[str, Any] = Field(default_factory=dict, description="群组设置")


class GroupUpdateRequest(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=128)
    slug: str | None = Field(
        default=None,
        min_length=1,
        max_length=64,
        pattern=r"^[a-z0-9][a-z0-9-]*$",
    )
    description: str | None = Field(default=None, description="群组描述")
    avatar_url: str | None = Field(default=None, max_length=512, description="群组头像URL")
    group_type: GroupType | None = Field(default=None, description="群组类型")
    visibility: GroupVisibility | None = Field(default=None, description="可见性")
    join_mode: GroupJoinMode | None = Field(default=None, description="加入方式")
    settings: dict[str, Any] | None = Field(default=None, description="群组设置")
    is_active: bool | None = Field(default=None, description="是否启用")


class GroupResponse(BaseModel):
    id: int
    workspace_id: int
    name: str
    slug: str
    description: str | None = None
    avatar_url: str | None = None
    group_type: str
    created_by: int
    owner_id: int
    visibility: str
    join_mode: str
    settings: dict[str, Any] = Field(default_factory=dict)
    is_active: bool
    created_at: datetime
    updated_at: datetime

    model_config = {"from_attributes": True}


class GroupMemberInviteRequest(BaseModel):
    user_id: int = Field(description="被邀请用户ID")
    role: Literal["admin", "member"] = Field(default="member", description="群组角色")


class GroupMemberRoleUpdateRequest(BaseModel):
    role: Literal["admin", "member"] = Field(description="新的群组角色")


class GroupOwnerTransferRequest(BaseModel):
    user_id: int = Field(description="新负责人用户ID")


class GroupMemberResponse(BaseModel):
    id: int
    group_id: int
    user_id: int
    username: str
    email: str
    full_name: str | None = None
    avatar_url: str | None = None
    role: str
    status: str
    invited_by: int | None = None
    joined_at: datetime | None = None
    created_at: datetime


class GroupJoinResponse(BaseModel):
    group_id: int
    user_id: int
    role: str
    status: str
    joined_at: datetime | None = None

    model_config = {"from_attributes": True}
