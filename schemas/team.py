from datetime import datetime

from pydantic import BaseModel, Field


class TeamResponse(BaseModel):
    id: int = Field(description="团队ID")
    session_id: int = Field(description="所属会话ID")
    team_name: str = Field(description="团队名称")
    creator_type: str = Field(description="创建者类型: user / agent")
    creator_id: str | None = Field(default=None, description="创建者标识")
    created_at: datetime = Field(description="创建时间")
    updated_at: datetime = Field(description="更新时间")

    model_config = {"from_attributes": True}


class TeamMemberResponse(BaseModel):
    id: int = Field(description="成员ID")
    session_id: int = Field(description="所属会话ID")
    team_id: int | None = Field(default=None, description="所属团队ID")
    name: str = Field(description="成员名称")
    role: str = Field(description="成员角色")
    status: str = Field(description="成员状态")
    agent_type: str | None = Field(default=None, description="成员对应的子代理类型")
    prompt: str | None = Field(default=None, description="成员初始任务提示")
    # 注意: history 是大文本对话历史,不放进默认 response 以免污染列表输出。
    created_at: datetime = Field(description="创建时间")
    updated_at: datetime = Field(description="更新时间")

    model_config = {"from_attributes": True}


class TeamMessageResponse(BaseModel):
    id: int = Field(description="消息ID")
    session_id: int = Field(description="所属会话ID")
    sender: str = Field(description="发送方")
    recipient: str = Field(description="接收方")
    message_type: str = Field(description="消息类型")
    content: str = Field(description="消息内容")
    consumer_instance_id: str | None = Field(default=None, description="限定消费实例ID")
    read_at: datetime | None = Field(default=None, description="读取时间")
    created_at: datetime = Field(description="创建时间")

    model_config = {"from_attributes": True}


class SubAgentRunResponse(BaseModel):
    id: int = Field(description="运行ID")
    session_id: int = Field(description="所属会话ID")
    member_name: str = Field(description="成员名称")
    status: str = Field(description="运行状态")
    prompt: str = Field(description="任务提示")
    report: str | None = Field(default=None, description="运行报告")
    error_message: str | None = Field(default=None, description="错误信息")
    started_at: datetime = Field(description="开始时间")
    finished_at: datetime | None = Field(default=None, description="结束时间")

    model_config = {"from_attributes": True}
