from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, Field, model_validator


class DepartmentCreateRequest(BaseModel):
    parent_id: int | None = Field(default=None, gt=0, description="父部门ID")
    name: str = Field(min_length=1, max_length=128, description="部门名称")
    code: str | None = Field(default=None, max_length=32, description="部门编码")
    description: str | None = Field(default=None, description="部门描述")
    manager_id: int | None = Field(default=None, gt=0, description="负责人用户ID")
    sort_order: int = Field(default=0, description="排序顺序")
    is_active: bool = Field(default=True, description="是否启用")

    model_config = {"str_strip_whitespace": True}


class DepartmentUpdateRequest(BaseModel):
    parent_id: int | None = Field(default=None, gt=0, description="父部门ID，空表示顶级部门")
    name: str | None = Field(default=None, min_length=1, max_length=128, description="部门名称")
    code: str | None = Field(default=None, max_length=32, description="部门编码")
    description: str | None = Field(default=None, description="部门描述")
    manager_id: int | None = Field(default=None, gt=0, description="负责人用户ID，空表示清除")
    sort_order: int | None = Field(default=None, description="排序顺序")
    is_active: bool | None = Field(default=None, description="是否启用")

    model_config = {"str_strip_whitespace": True}

    @model_validator(mode="after")
    def validate_changes(self) -> DepartmentUpdateRequest:
        if not self.model_fields_set:
            raise ValueError("至少需要提供一个待更新字段")
        for field_name in ("name", "sort_order", "is_active"):
            if field_name in self.model_fields_set and getattr(self, field_name) is None:
                raise ValueError(f"{field_name} 不能设置为空")
        return self


class DepartmentSetMemberRequest(BaseModel):
    department_id: int | None = Field(default=None, gt=0, description="目标部门ID，空表示移出部门")
    job_title: str | None = Field(default=None, max_length=128, description="职位/岗位")

    model_config = {"str_strip_whitespace": True}


class DepartmentResponse(BaseModel):
    id: int = Field(description="部门ID")
    workspace_id: int = Field(description="所属工作区ID")
    parent_id: int | None = Field(default=None, description="父部门ID")
    name: str = Field(description="部门名称")
    code: str | None = Field(default=None, description="部门编码")
    description: str | None = Field(default=None, description="部门描述")
    manager_id: int | None = Field(default=None, description="负责人用户ID")
    sort_order: int = Field(description="排序顺序")
    is_active: bool = Field(description="是否启用")
    created_at: datetime = Field(description="创建时间")
    updated_at: datetime = Field(description="更新时间")

    model_config = {"from_attributes": True}


class DepartmentTreeResponse(DepartmentResponse):
    children: list[DepartmentTreeResponse] = Field(default_factory=list, description="子部门")


class DepartmentMemberResponse(BaseModel):
    id: int = Field(description="工作区成员记录ID")
    workspace_id: int = Field(description="工作区ID")
    user_id: int = Field(description="用户ID")
    username: str = Field(description="用户名")
    email: str = Field(description="邮箱")
    full_name: str | None = Field(default=None, description="全名")
    avatar_url: str | None = Field(default=None, description="头像URL")
    role: str = Field(description="工作区角色")
    department_id: int | None = Field(default=None, description="所属部门ID")
    job_title: str | None = Field(default=None, description="职位/岗位")
    joined_at: datetime | None = Field(default=None, description="加入时间")


class DepartmentAssignmentResponse(BaseModel):
    id: int = Field(description="工作区成员记录ID")
    workspace_id: int = Field(description="工作区ID")
    user_id: int = Field(description="用户ID")
    department_id: int | None = Field(default=None, description="所属部门ID")
    job_title: str | None = Field(default=None, description="职位/岗位")
    updated_at: datetime = Field(description="更新时间")

    model_config = {"from_attributes": True}


class DepartmentDeleteResult(BaseModel):
    deleted: bool = Field(description="是否已删除")
