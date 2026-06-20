import time
from enum import Enum
from typing import Optional
from pydantic import BaseModel, Field

# ========== agent_teams.py ==========
class Team(BaseModel):
    team_name: str = Field(description='Team name')
    members: Optional[list[TeamMember]] = Field(
        default=[],
        description='Members of the team'
    )

class TeamMember(BaseModel):
    name: str = Field(description='member name')
    role: str = Field(description='member role')
    # working/idle/shutdown
    status: str = Field(description='member status')

# ========== team_message_bus.py ==========
class MailMessageType(str, Enum):
    MESSAGE = "message"
    BROADCAST = "broadcast"
    SHUTDOWN_REQUEST = "shutdown_request",
    SHUTDOWN_RESPONSE = "shutdown_response",
    PLAN_APPROVAL_RESPONSE = "plan_approval_response",

class TeamMailMessage(BaseModel):
    type: MailMessageType = Field(description='Type of message')
    # 发件人
    sender: str = Field(description='From address')
    # 收件人
    to: str = Field(description='To address')
    # 消息内容
    content: str = Field(description='Message content')
    # 消息时间
    timestamp: int = Field(
        default=time.time(),
        description='Timestamp of the message'
    )
    # 扩展参数
    extra: dict = Field(
        default={},
        description='Extra data'
    )
