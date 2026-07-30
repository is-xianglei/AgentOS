from datetime import datetime
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, Field


class SessionSendMessageRequest(BaseModel):
    session_id: int | None = Field(default=None, description="会话ID，不传则自动创建新会话")
    content: str = Field(description="用户消息")


class SessionApprovalRequest(BaseModel):
    request_id: str = Field(description="待批准请求ID(permission_request 事件里的 request_id)")
    decision: Literal["allow_once", "always_allow", "deny"] = Field(description="裁决结果")
    updated_input: dict[str, Any] | None = Field(
        default=None, description="批准时可选修改的工具入参"
    )
    always_scope: Literal["session", "global"] = Field(
        default="session", description="decision=always_allow 时规则落库的作用域"
    )


class SessionRenameRequest(BaseModel):
    title: str = Field(min_length=1, max_length=200, description="会话新标题")


class SessionBatchDeleteRequest(BaseModel):
    ids: list[int] = Field(min_length=1, description="待删除的会话ID列表")


class SessionDeleteResult(BaseModel):
    deleted: int = Field(description="实际删除的会话数量")


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
    turn_id: UUID | None = Field(default=None, description="所属交互轮次ID")
    role: Literal["user", "assistant", "tool"] | str = Field(description="消息角色")
    content: Any = Field(description="消息内容")
    token_estimate: int = Field(description="预估token数")
    created_at: datetime = Field(description="创建时间")

    model_config = {"from_attributes": True}


class SendMessageAccepted(BaseModel):
    session_id: int = Field(description="会话ID")
    stream: bool = Field(description="是否返回流")
