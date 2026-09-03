from __future__ import annotations

from datetime import datetime
from typing import Annotated

from pydantic import BaseModel, Field, field_validator, model_validator

from workflow.models import WorkflowMode

PositiveId = Annotated[int, Field(gt=0)]


class WorkflowAgentItem(BaseModel):
    agent_id: PositiveId = Field(description="成员Agent ID")
    sort_order: int = Field(default=0, ge=0, description="执行/展示顺序，越小越靠前")

    model_config = {"extra": "forbid"}


def _require_unique_agents(items: list[WorkflowAgentItem]) -> list[WorkflowAgentItem]:
    seen: set[int] = set()
    duplicated: list[int] = []
    for item in items:
        if item.agent_id in seen:
            duplicated.append(item.agent_id)
        seen.add(item.agent_id)
    if duplicated:
        raise ValueError(f"成员Agent重复绑定: {sorted(set(duplicated))}")
    return items


class WorkflowCreateRequest(BaseModel):
    name: str = Field(min_length=1, max_length=128, description="Workflow名称")
    description: str | None = Field(default=None, description="Workflow用途说明")
    mode: WorkflowMode = Field(description="编排模式")
    is_enabled: bool = Field(default=True, description="是否启用")
    agents: list[WorkflowAgentItem] = Field(min_length=1, description="成员Agent列表，至少1个")

    model_config = {"extra": "forbid"}

    @field_validator("name")
    @classmethod
    def normalize_name(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("Workflow名称不能为空")
        return value

    @field_validator("agents")
    @classmethod
    def validate_agents(cls, value: list[WorkflowAgentItem]) -> list[WorkflowAgentItem]:
        return _require_unique_agents(value)


class WorkflowUpdateRequest(BaseModel):
    name: str | None = Field(
        default=None,
        min_length=1,
        max_length=128,
        description="Workflow名称",
    )
    description: str | None = Field(default=None, description="Workflow用途说明")
    mode: WorkflowMode | None = Field(default=None, description="编排模式")
    is_enabled: bool | None = Field(default=None, description="是否启用")
    agents: list[WorkflowAgentItem] | None = Field(
        default=None,
        min_length=1,
        description="成员Agent列表，提供时整体替换，至少1个",
    )

    model_config = {"extra": "forbid"}

    @field_validator("name")
    @classmethod
    def normalize_name(cls, value: str | None) -> str | None:
        if value is None:
            return None
        value = value.strip()
        if not value:
            raise ValueError("Workflow名称不能为空")
        return value

    @field_validator("agents")
    @classmethod
    def validate_agents(
        cls,
        value: list[WorkflowAgentItem] | None,
    ) -> list[WorkflowAgentItem] | None:
        if value is None:
            return None
        return _require_unique_agents(value)

    @model_validator(mode="after")
    def validate_changes(self) -> WorkflowUpdateRequest:
        if not self.model_fields_set:
            raise ValueError("至少需要提供一个待更新字段")
        for field_name in ("name", "mode", "is_enabled", "agents"):
            if field_name in self.model_fields_set and getattr(self, field_name) is None:
                raise ValueError(f"{field_name} 不能设置为空")
        return self


class WorkflowAgentResponse(BaseModel):
    agent_id: int = Field(description="成员Agent ID")
    agent_name: str | None = Field(default=None, description="成员Agent名称")
    sort_order: int = Field(description="执行/展示顺序，越小越靠前")

    model_config = {"from_attributes": True}


class WorkflowResponse(BaseModel):
    id: int = Field(description="Workflow ID")
    workspace_id: int = Field(description="所属工作区ID")
    name: str = Field(description="Workflow名称")
    description: str | None = Field(default=None, description="Workflow用途说明")
    mode: WorkflowMode = Field(description="编排模式")
    is_enabled: bool = Field(description="是否启用")
    created_by_user_id: int = Field(description="创建者用户ID")
    agents: list[WorkflowAgentResponse] = Field(
        default_factory=list,
        description="成员Agent，按 (sort_order, agent_id) 升序",
    )
    created_at: datetime = Field(description="创建时间")
    updated_at: datetime = Field(description="更新时间")

    model_config = {"from_attributes": True}


class WorkflowListResponse(BaseModel):
    items: list[WorkflowResponse] = Field(description="当前页Workflow")
    total: int = Field(description="Workflow总数")
    limit: int = Field(description="分页大小")
    offset: int = Field(description="分页偏移")


class WorkflowModeInfo(BaseModel):
    mode: WorkflowMode = Field(description="模式标识")
    name: str = Field(description="模式中文名")
    description: str = Field(description="模式说明")


class WorkflowDeleteResult(BaseModel):
    deleted: bool = Field(description="是否已删除")
