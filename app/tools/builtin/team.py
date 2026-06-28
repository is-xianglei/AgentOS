"""团队协作工具

- TeamCreate   建团队
- TeamSpawn    派持久成员(teammate)
- TeamList     列成员
- SendMessage  发消息 / 广播 / 关停握手 / 计划批复(聚合于此)
- ReadInbox    读并清空某收件人收件箱
- ListMessages 列出本会话所有消息

"""

import json
from typing import Literal

from pydantic import BaseModel, Field

from app.schemas.team import TeamMemberResponse, TeamMessageResponse, TeamResponse
from app.services.team_service import TeamService
from app.tools.base import BaseTool, ToolContext


# ----- 模块内共享序列化 helper(保留一份) -----


def _team_data(team) -> dict:
    return TeamResponse.model_validate(team).model_dump(mode="json")


def _member_data(member) -> dict:
    return TeamMemberResponse.model_validate(member).model_dump(mode="json")


def _message_data(message) -> dict:
    return TeamMessageResponse.model_validate(message).model_dump(mode="json")


# ----- TeamCreate -----


class TeamCreateInput(BaseModel):
    team_name: str | None = Field(default=None, description="团队名称,可选,缺省为 default")
    creator_id: str | None = Field(default=None, description="创建者标识,可选")

class TeamCreateTool(BaseTool):
    name = "TeamCreate"
    description = (
        "为当前会话创建(或获取已存在的)团队。团队与会话 1:1,后续派遣队友、"
        "收发消息都归属在此团队下。通常在组建多人协作前先调用一次。"
    )
    input_model = TeamCreateInput

    async def run(self, args: TeamCreateInput, ctx: ToolContext) -> str:
        service = TeamService(ctx.db)
        team = await service.get_or_create_team(
            ctx.session_id,
            team_name=args.team_name or "default",
            creator_type="agent",  # 工具由大模型触发,创建者类型固定为 agent
            creator_id=args.creator_id,
        )
        return json.dumps(_team_data(team), ensure_ascii=False)


# ----- TeamSpawn -----


class TeamSpawnInput(BaseModel):
    name: str = Field(description="队友名称,会话内唯一")
    role: str = Field(description="队友角色描述")
    prompt: str = Field(description="队友的初始任务提示")
    agent_type: str = Field(default="general_purpose", description="队友对应的子代理类型")


class TeamSpawnTool(BaseTool):
    name = "TeamSpawn"
    description = (
        "派遣一个持久的队友(teammate)进入当前团队:落地成员记录并写入其角色、"
        "子代理类型与初始任务提示,置为 working。name/role/prompt 必填。"
    )
    input_model = TeamSpawnInput

    async def run(self, args: TeamSpawnInput, ctx: ToolContext) -> str:
        service = TeamService(ctx.db)
        member = await service.spawn_member(
            ctx.session_id,
            name=args.name,
            role=args.role,
            prompt=args.prompt,
            agent_type=args.agent_type,
            creator_type="agent",
            creator_id=None,  # ToolContext 无创建者信息
        )
        return json.dumps(_member_data(member), ensure_ascii=False)


# ----- TeamList -----


class TeamListInput(BaseModel):
    pass


class TeamListTool(BaseTool):
    name = "TeamList"
    description = "列出当前会话团队的所有成员(含状态、角色、子代理类型)。"
    input_model = TeamListInput

    async def run(self, args: TeamListInput, ctx: ToolContext) -> str:
        service = TeamService(ctx.db)
        members = await service.list_members(ctx.session_id)
        return json.dumps([_member_data(m) for m in members], ensure_ascii=False)


# ----- SendMessage(聚合:普通消息 / 广播 / 关停握手 / 计划批复) -----

# recipient="*" 表士广播;type 区分普通消息与握手类消息,握手类同样落库为
# 一条 team_messages 记录(message_type 用 type 值),不放内存。
_MessageType = Literal[
    "message",
    "shutdown_request",
    "shutdown_response",
    "plan_approval_response",
]


class SendMessageInput(BaseModel):
    sender: str = Field(description="发送方名称(队友名或 lead)")
    recipient: str = Field(
        description='接收方名称;特殊值 "*" 表示广播给全体成员(自动排除 sender)'
    )
    content: str = Field(description="消息内容(或关停原因 / 计划批复内容)")
    type: _MessageType = Field(
        default="message",
        description=(
            "消息类型:message=普通直发消息(默认);"
            "shutdown_request=请求队友收尾关停;"
            "shutdown_response=对关停请求的回应;"
            "plan_approval_response=对计划的批复。"
            '握手类型(非 message)不支持广播(recipient 不能为 "*")。'
        ),
    )


class SendMessageTool(BaseTool):
    name = "SendMessage"
    description = (
        "向团队成员发送消息。一个工具聚合四种发送场景:\n"
        '- 普通直发:recipient 填对方名,type=message(默认)。\n'
        '- 广播:recipient="*",消息发给除自己外的全体成员(仅支持 type=message)。\n'
        "- 关停握手:type=shutdown_request / shutdown_response。\n"
        "- 计划批复:type=plan_approval_response。\n"
        "所有消息(含握手)都会落库为一条团队消息,接收方通过 ReadInbox 感知。\n"
        "读取自己的收件箱用 ReadInbox;查看全会话消息流用 ListMessages。"
    )
    input_model = SendMessageInput

    async def run(self, args: SendMessageInput, ctx: ToolContext) -> str:
        service = TeamService(ctx.db)

        # 广播分支:recipient="*"。广播只承载普通消息语义,拒绝握手类型。
        if args.recipient == "*":
            if args.type != "message":
                return json.dumps(
                    {
                        "error": "BROADCAST_TYPE_UNSUPPORTED",
                        "message": (
                            f"广播(recipient='*')不支持握手类型 '{args.type}',"
                            "请对单个接收方发送握手消息。"
                        ),
                    },
                    ensure_ascii=False,
                )
            delivered = await service.broadcast(
                ctx.session_id, sender=args.sender, content=args.content
            )
            return json.dumps({"delivered": delivered}, ensure_ascii=False)

        # 单点发送分支:message_type 直接透传 type 值,握手类型即作为一条消息落库。
        message = await service.send_message(
            ctx.session_id,
            sender=args.sender,
            recipient=args.recipient,
            content=args.content,
            message_type=args.type,
        )
        return json.dumps(_message_data(message), ensure_ascii=False)


# ----- ReadInbox -----

class ReadInboxInput(BaseModel):
    recipient: str = Field(description="收件人名称(队友名或 lead),读取其未读消息")


class ReadInboxTool(BaseTool):
    name = "ReadInbox"
    description = (
        "读取并清空指定收件人的收件箱:返回其全部未读消息(含他人发来的普通消息、"
        "广播、关停请求与计划批复),并标记为已读。队友轮询自身消息时使用。"
    )
    input_model = ReadInboxInput

    async def run(self, args: ReadInboxInput, ctx: ToolContext) -> str:
        service = TeamService(ctx.db)
        messages = await service.read_inbox(ctx.session_id, args.recipient)
        return json.dumps([_message_data(m) for m in messages], ensure_ascii=False)


# ----- ListMessages -----

class ListMessagesInput(BaseModel):
    pass


class ListMessagesTool(BaseTool):
    name = "ListMessages"
    description = (
        "列出当前会话的全部团队消息(不区分收件人、不改读取状态),用于审视整个"
        "协作消息流;只想取自己的未读消息请改用 ReadInbox。"
    )
    input_model = ListMessagesInput

    async def run(self, args: ListMessagesInput, ctx: ToolContext) -> str:
        service = TeamService(ctx.db)
        messages = await service.list_messages(ctx.session_id)
        return json.dumps([_message_data(m) for m in messages], ensure_ascii=False)

