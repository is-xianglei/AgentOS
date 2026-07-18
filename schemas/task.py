from datetime import datetime

from pydantic import BaseModel, Field


class TaskResponse(BaseModel):
    id: int = Field(description="任务ID")
    session_id: int = Field(description="所属会话ID")
    subject: str = Field(description="任务主题")
    description: str = Field(description="任务描述")
    status: str = Field(description="任务状态")
    owner: str = Field(description="任务负责人")
    blocked_by: list = Field(description="阻塞任务列表")
    created_at: datetime = Field(description="创建时间")
    updated_at: datetime = Field(description="更新时间")

    model_config = {"from_attributes": True}
