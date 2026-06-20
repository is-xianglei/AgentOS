import json
from pathlib import Path
from pydantic import BaseModel
from pydantic import Field
from tools.team.team_types import MailMessageType, TeamMailMessage
from tools.base import BaseTool


# 每个团队成员对应一个 JSONL 收件箱
class TeamMessageBus:

    def __init__(self, mail_box_dir: Path):
        self.mail_box_dir = mail_box_dir
        self.mail_box_dir.mkdir(parents=True, exist_ok=True)

    # 发送信件
    def send_message(self, message: TeamMailMessage):
        if message.type not in MailMessageType:
            return f'Error: Invalid type {message.type}. Valid types are {list(MailMessageType.__members__.values())}'
        mail_path = self.mail_box_dir / f'{message.to}.jsonl'
        with open(mail_path, 'a', encoding='utf-8') as f:
            f.write(message.model_dump_json()+'\n')
        return f'Sent {message.type} message to {message.to}'

    # 接收信件
    def receive_mail(self, name: str) -> list[TeamMailMessage]:
        mail_path = self.mail_box_dir / f'{name}.jsonl'
        if not mail_path.exists():
            return []
        lines: list[str] = mail_path.read_text(encoding='utf-8').strip().splitlines()
        messages: list[TeamMailMessage] = []
        for line in lines:
            messages.append(TeamMailMessage(**json.loads(line)))
        mail_path.write_text("")
        return messages

    # 广播通知
    def broadcast_message(self, sender: str, content: str, teammates: list[str]) -> str:
        teammates.remove(sender)
        for name in teammates:
            message = TeamMailMessage(
                type=MailMessageType.BROADCAST,
                sender=sender,
                to=name,
                content=content
            )
            self.send_message(message)
        return f'Broadcasted to {len(teammates)} teammates'

class TeamBusSendMessageTool(BaseTool):
    name = 'send_message'
    description = "Send a message to a teammate's inbox."
    input_model = TeamMailMessage

    def __init__(self, team_message_bus: TeamMessageBus):
        self.team_message_bus = team_message_bus

    def run(self, input_object: TeamMailMessage) -> str:
        return self.team_message_bus.send_message(input_object)

class ReceiveMessageInput(BaseModel):
    name: str = Field(description='Name of the message')

class TeamBusReceiveMessageTool(BaseTool):
    name = 'read_inbox'
    description = "Read and drain the lead's inbox."
    input_model = ReceiveMessageInput

    def __init__(self, team_message_bus: TeamMessageBus):
        self.team_message_bus = team_message_bus

    def run(self, input_object: ReceiveMessageInput) -> str:
        mail_messages: list[TeamMailMessage] = self.team_message_bus.receive_mail(input_object.name)
        return json.dumps(mail_messages, indent=2)


class BroadcastMessageInput(BaseModel):
    sender: str = Field(description='Name of the message')
    content: str = Field(description='Message content')

class TeamBusBroadcastMessageTool(BaseTool):
    name = 'broadcast'
    description = 'Send a message to all teammates.'
    input_model = BroadcastMessageInput

    def __init__(self, team_message_bus: TeamMessageBus):
        self.team_message_bus = team_message_bus
        self.team_manager = None

    def set_team_manager(self, team_manager) -> TeamBusBroadcastMessageTool:
        self.team_manager = team_manager
        return self

    def run(self, input_object: BroadcastMessageInput) -> str:
        member_names: list[str] = self.team_manager.member_names()
        return self.team_message_bus.broadcast_message(input_object.sender, input_object.content, member_names)
