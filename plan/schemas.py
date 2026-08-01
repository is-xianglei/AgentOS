from datetime import datetime
from typing import Any
from uuid import UUID

from pydantic import BaseModel, Field


class SessionPlanResponse(BaseModel):
    id: UUID = Field(description="计划ID")
    session_id: int = Field(description="所属会话ID")
    entered_turn_id: UUID = Field(description="进入Plan Mode的轮次ID")
    exited_turn_id: UUID | None = Field(default=None, description="退出Plan Mode的轮次ID")
    status: str = Field(description="计划状态")
    content: str = Field(description="Markdown计划正文")
    previous_mode: str = Field(description="进入前运行模式")
    allowed_prompts: list[dict[str, Any]] = Field(description="语义权限声明")
    feedback: str | None = Field(default=None, description="最近用户反馈")
    version: int = Field(description="内容版本号")
    approved_by: int | None = Field(default=None, description="批准用户ID")
    approved_at: datetime | None = Field(default=None, description="批准时间")
    created_at: datetime = Field(description="创建时间")
    updated_at: datetime = Field(description="更新时间")

    model_config = {"from_attributes": True}
