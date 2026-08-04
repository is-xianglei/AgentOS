from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field


class ToolResponse(BaseModel):
    id: int = Field(description="工具ID")
    tool_key: str = Field(description="工具稳定标识")
    name: str = Field(description="工具名称")
    description: str = Field(description="工具描述")
    tool_type: str = Field(description="工具类型")
    runtime_name: str = Field(description="代码注册表名称")
    is_system: bool = Field(description="是否系统内置工具")
    is_enabled: bool = Field(description="是否启用")
    created_at: datetime = Field(description="创建时间")
    updated_at: datetime = Field(description="更新时间")

    model_config = {"from_attributes": True}


class ToolDetailResponse(ToolResponse):
    input_schema: dict[str, Any] = Field(description="工具输入JSON Schema")
    implementation_config: dict[str, Any] = Field(description="工具实现配置")


class ToolListResponse(BaseModel):
    items: list[ToolResponse] = Field(description="工具列表")
    total: int = Field(description="匹配总数")
    limit: int = Field(description="分页大小")
    offset: int = Field(description="分页偏移")
