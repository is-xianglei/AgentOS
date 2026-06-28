from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field


class SessionSendMessageRequest(BaseModel):
    session_id: int | None = Field(default=None, description="会话ID，不传则自动创建新会话")
    content: str = Field(description="用户消息")


class SessionResponse(BaseModel):
    id: int = Field(description="会话ID")
    title: str = Field(description="会话标题")
    status: str = Field(description="会话状态")
    model_name: str | None = Field(default=None, description="模型名称")
    system_prompt: str | None = Field(default=None, description="系统提示词")
    # ORM 属性名为 extra(DB 列名 metadata);注意 SQLAlchemy 模型自带 metadata 属性
    # (指 MetaData 对象),因此只能从 extra 读取,对外仍序列化为 metadata。
    metadata: dict[str, Any] = Field(
        default_factory=dict,
        description="扩展信息",
        validation_alias="extra",
    )
    created_at: datetime = Field(description="创建时间")
    updated_at: datetime = Field(description="更新时间")
    last_active_at: datetime = Field(description="最后活跃时间")

    model_config = {"from_attributes": True, "populate_by_name": True}


class SessionMessageResponse(BaseModel):
    id: int = Field(description="消息ID")
    session_id: int = Field(description="所属会话ID")
    role: Literal["user", "assistant", "tool"] | str = Field(description="消息角色")
    content: Any = Field(description="消息内容")
    token_estimate: int = Field(description="预估token数")
    created_at: datetime = Field(description="创建时间")

    model_config = {"from_attributes": True}


class SendMessageAccepted(BaseModel):
    session_id: int = Field(description="会话ID")
    stream: bool = Field(description="是否返回流")
