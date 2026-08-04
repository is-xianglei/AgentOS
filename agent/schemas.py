from __future__ import annotations

from datetime import datetime
from typing import Annotated

from pydantic import BaseModel, Field, field_validator, model_validator

PositiveId = Annotated[int, Field(gt=0)]


class _AgentFields(BaseModel):
    name: str = Field(min_length=1, max_length=128, description="Agent名称")
    description: str | None = Field(default=None, description="Agent描述")
    system_prompt: str = Field(min_length=1, description="系统提示词")
    model_name: str | None = Field(default=None, max_length=128, description="模型名称")
    is_enabled: bool = Field(default=True, description="是否启用")

    model_config = {"extra": "forbid"}

    @field_validator("name")
    @classmethod
    def normalize_name(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("Agent名称不能为空")
        return value

    @field_validator("model_name")
    @classmethod
    def normalize_model_name(cls, value: str | None) -> str | None:
        if value is None:
            return None
        value = value.strip()
        return value or None


class AgentCreateRequest(_AgentFields):
    tool_ids: list[PositiveId] = Field(default_factory=list, description="绑定工具ID")
    skill_ids: list[PositiveId] = Field(default_factory=list, description="绑定Skill ID")


class AgentUpdateRequest(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=128, description="Agent名称")
    description: str | None = Field(default=None, description="Agent描述")
    system_prompt: str | None = Field(default=None, min_length=1, description="系统提示词")
    model_name: str | None = Field(default=None, max_length=128, description="模型名称")
    is_enabled: bool | None = Field(default=None, description="是否启用")

    model_config = {"extra": "forbid"}

    @field_validator("name")
    @classmethod
    def normalize_name(cls, value: str | None) -> str | None:
        if value is None:
            return None
        value = value.strip()
        if not value:
            raise ValueError("Agent名称不能为空")
        return value

    @field_validator("model_name")
    @classmethod
    def normalize_model_name(cls, value: str | None) -> str | None:
        if value is None:
            return None
        value = value.strip()
        return value or None

    @model_validator(mode="after")
    def validate_changes(self) -> AgentUpdateRequest:
        if not self.model_fields_set:
            raise ValueError("至少需要提供一个待更新字段")
        for field_name in ("name", "system_prompt", "is_enabled"):
            if field_name in self.model_fields_set and getattr(self, field_name) is None:
                raise ValueError(f"{field_name} 不能设置为空")
        return self


class AgentToolBindingsRequest(BaseModel):
    tool_ids: list[PositiveId] = Field(default_factory=list, description="工具ID全集")

    model_config = {"extra": "forbid"}


class AgentSkillBindingsRequest(BaseModel):
    skill_ids: list[PositiveId] = Field(default_factory=list, description="Skill ID全集")

    model_config = {"extra": "forbid"}


class AgentResponse(BaseModel):
    id: int = Field(description="Agent ID")
    workspace_id: int = Field(description="所属工作区ID")
    name: str = Field(description="Agent名称")
    description: str | None = Field(default=None, description="Agent描述")
    system_prompt: str = Field(description="系统提示词")
    model_name: str | None = Field(default=None, description="模型名称")
    is_enabled: bool = Field(description="是否启用")
    created_by_user_id: int = Field(description="创建者用户ID")
    tool_ids: list[int] = Field(default_factory=list, description="绑定工具ID")
    skill_ids: list[int] = Field(default_factory=list, description="绑定Skill ID")
    created_at: datetime = Field(description="创建时间")
    updated_at: datetime = Field(description="更新时间")

    model_config = {"from_attributes": True}


class AgentListResponse(BaseModel):
    items: list[AgentResponse] = Field(description="当前页Agent")
    total: int = Field(description="Agent总数")
    limit: int = Field(description="分页大小")
    offset: int = Field(description="分页偏移")


class AgentDeleteResult(BaseModel):
    deleted: bool = Field(description="是否已删除")
