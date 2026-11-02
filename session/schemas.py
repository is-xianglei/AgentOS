from datetime import datetime
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator


class _StrictRequest(BaseModel):
    """会话写接口的严格请求基类，避免客户端字段被静默丢弃。"""

    model_config = ConfigDict(extra="forbid")


class SessionSendMessageRequest(_StrictRequest):
    session_id: int | None = Field(default=None, description="会话ID，不传则自动创建新会话")
    agent_id: int | None = Field(
        default=None,
        description="新会话使用的Agent ID，空表示使用默认Agent",
    )
    workflow_id: int | None = Field(
        default=None,
        description="使用的Workflow ID，与agent_id互斥",
    )
    content: str = Field(description="用户消息")
    model_provider_id: str | None = Field(default=None, description="指定模型供应商；新会话可显式传 null 使用默认")
    mode_slug: Literal["standard", "turbo"] = "standard"
    thinking_effort: Literal["fast", "low", "medium", "high", "xhigh", "max"] = "fast"

    @model_validator(mode="before")
    @classmethod
    def reject_unsupported_orchestration_plan(cls, data: object) -> object:
        """明确拒绝尚未提供资源实现的旧编排方案字段。"""
        if isinstance(data, dict) and "orchestration_plan_id" in data:
            raise ValueError(
                "当前不支持 orchestration_plan_id，请使用已存在的 workflow_id"
            )
        return data


class SessionRenameRequest(_StrictRequest):
    title: str | None = Field(default=None, min_length=1, max_length=200, description="会话新标题")
    model_provider_id: str | None = None
    mode_slug: Literal["standard", "turbo"] | None = None
    thinking_effort: Literal["fast", "low", "medium", "high", "xhigh", "max"] | None = None

    @model_validator(mode="after")
    def validate_patch(self) -> SessionRenameRequest:
        if not self.model_fields_set or any(
            name in self.model_fields_set and getattr(self, name) is None
            for name in ("title", "mode_slug", "thinking_effort")
        ):
            raise ValueError("请提供有效的会话标题或模型选项")
        return self


class SessionBatchDeleteRequest(_StrictRequest):
    ids: list[int] = Field(min_length=1, description="待删除的会话ID列表")


class SessionDeleteResult(BaseModel):
    deleted: int = Field(description="实际删除的会话数量")


class SessionResponse(BaseModel):
    id: int = Field(description="会话ID")
    title: str = Field(description="会话标题")
    status: str = Field(description="会话状态")
    model_name: str | None = Field(default=None, description="模型名称")
    model_provider_id: str | None = None
    mode_slug: Literal["standard", "turbo"] = "standard"
    thinking_effort: Literal["fast", "low", "medium", "high", "xhigh", "max"] = "fast"
    system_prompt: str | None = Field(default=None, description="系统提示词")
    user_id: int | None = Field(default=None, description="创建用户ID")
    workspace_id: int | None = Field(default=None, description="所属工作区ID")
    agent_id: int | None = Field(default=None, description="绑定的Agent ID")
    workflow_id: int | None = Field(
        default=None,
        description="锁定的Workflow ID，空表示普通Agent会话",
    )
    visibility: str = Field(description="可见性")
    shared_with: list[int] = Field(default_factory=list, description="共享用户ID列表")
    shared_with_departments: list[int] = Field(
        default_factory=list,
        description="共享部门ID列表",
    )
    shared_with_groups: list[int] = Field(
        default_factory=list,
        description="共享群组ID列表",
    )
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


class SessionShareTargetsRequest(_StrictRequest):
    users: list[int] = Field(default_factory=list, description="共享用户ID列表")
    departments: list[int] = Field(default_factory=list, description="共享部门ID列表")
    groups: list[int] = Field(default_factory=list, description="共享群组ID列表")


class SessionShareRequest(_StrictRequest):
    """覆盖会话当前的完整共享范围。"""

    visibility: Literal["private", "workspace", "public"] = Field(description="可见性")
    share_with: SessionShareTargetsRequest = Field(default_factory=SessionShareTargetsRequest)


class SessionShareResponse(BaseModel):
    session_id: int = Field(description="会话ID")
    visibility: str = Field(description="可见性")
    users: list[int] = Field(default_factory=list, description="共享用户ID列表")
    departments: list[int] = Field(default_factory=list, description="共享部门ID列表")
    groups: list[int] = Field(default_factory=list, description="共享群组ID列表")


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


class ActiveTurnResponse(BaseModel):
    """会话最新一轮的状态;executing 为 true 时可调用订阅接口续播。"""

    turn_id: UUID = Field(description="最新交互轮次ID")
    status: str = Field(description="轮次业务状态")
    executing: bool = Field(description="是否有执行中的任务(执行租约有效)")
    last_event_offset: int = Field(description="已分配的最大事件offset")


class TurnCancelResponse(BaseModel):
    turn_id: UUID = Field(description="交互轮次ID")
    status: str = Field(description="取消后的轮次状态")
    session_status: str = Field(description="取消后的会话状态")
