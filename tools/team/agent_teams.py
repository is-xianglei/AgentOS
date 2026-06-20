import json
import threading
from pathlib import Path
from llm_client import client, LLM_MODEL
from anthropic.types import MessageParam, ToolResultBlockParam
from pydantic import BaseModel
from tools.base import BaseTool
from tools.builtin.bash import BashTool
from tools.builtin.weather import WeatherTool
from tools.team.team_types import Team, TeamMember, TeamMailMessage
from tools.team.team_message_bus import TeamMessageBus

class TeammateManager:

    def __init__(self, team_dir: Path, message_bus: TeamMessageBus):
        self.team_dir = team_dir
        self.team_dir.mkdir(exist_ok=True)
        self.config_path = self.team_dir / 'config.json'
        self.config: Team = self._load_team_config()
        self.threads = {}
        self.message_bus = message_bus

    def _load_team_config(self) -> Team:
        if self.config_path.exists():
            config_json: str = self.config_path.read_text(encoding='utf-8')
            config_dict: dict = json.loads(config_json)
            return Team(**config_dict)
        else:
            return Team(team_name='default', members=[])

    def _save_team_config(self):
        self.config_path.write_text(self.config.model_dump_json(indent=2), encoding='utf-8')

    def _find_member(self, name: str) -> TeamMember | None:
        team: Team = self.config
        for member in team.members:
            if member.name == name:
                return member
        return None

    def _teammate_loop(self, name: str, role: str, prompt: str):
        system_prompt = f'''
            You are '{name}', role: {role}.
            Use send_message to communicate. Complete your task.
        '''

        messages: list[MessageParam] = [
            MessageParam(role='user', content=prompt)
        ]

        registered_tools = [
            BashTool(),
            WeatherTool(),
        ]
        tools = [tool.to_param() for tool in registered_tools]

        tool_handlers = {tool.name: tool for tool in registered_tools}

        for _ in range(50):
            mail_messages: list[TeamMailMessage] = self.message_bus.receive_mail(name)
            for message in mail_messages:
                messages.append(
                    MessageParam(role='user', content=message.content)
                )
                response = client.messages.create(
                    model=LLM_MODEL,
                    system=system_prompt,
                    tools=tools,
                    max_tokens=1024,
                )
                messages.append(
                    MessageParam(role='assistant', content=response.content)
                )

                if response.stop_reason != 'tool_use':
                    break

                tool_results = []
                for block in response.content:
                    if block.type == 'tool_use':
                        handler = tool_handlers[block.name]
                        tool_result = ToolResultBlockParam(
                            type='tool_result',
                            tool_use_id=block.id,
                            content=handler.run_with_dict(block.input)
                        )
                        tool_results.append(tool_result)
                messages.append(MessageParam(role='user', content=tool_results))

        for member in self.config.members:
            if member.name == name:
                member.status = 'idle'
        self._save_team_config()

    def spawn(self, name: str, role: str, prompt: str) -> str:
        team_member: TeamMember = self._find_member(name)
        if team_member:
            if team_member.status not in ['idle', 'shutdown']:
                return f'Error: {name} is currently {team_member.status}'
            team_member.status = 'working'
            team_member.role = role
        else:
            member = TeamMember(name=name, role=role, status='working')
            team: Team = self.config
            team.members.append(member)
            team_member = member
        self._save_team_config()

        thread = threading.Thread(
            target=self._teammate_loop,
            args=(name, role, prompt),
            daemon=True
        )
        self.threads[team_member.name] = thread
        thread.start()
        return f"Spawned {name} {{'role': '{role}'}}"

    def teams(self) -> str:
        team: Team = self.config
        if team.members:
            return 'No teammates.'
        team_name: str = team.team_name
        lines: list[str] = [f'Team: {team_name}']
        for member in team.members:
            lines.append(f'{member.name}-({member.role}: {member.status})')
        return '\n'.join(lines)

    def member_names(self) -> list[str]:
        return [member.name for member in self.config.members]

class TeamSpawnInput(BaseModel):
    name: str
    role: str
    prompt: str

class TeamSpawnTeammateTool(BaseTool):
    name = 'spawn_teammate'
    description = 'Spawn a persistent teammate that runs in its own thread.'
    input_model = TeamSpawnInput

    def __init__(self, teammate: TeammateManager):
        self.teammate = teammate

    def run(self, input_object: TeamSpawnInput) -> str:
        return self.teammate.spawn(input_object.name, input_object.role, input_object.prompt)

class TeamListTeammateTool(BaseTool):
    name = 'list_teammates'
    description = 'List all teammates with name, role, status.'
    input_model = {}

    def __init__(self, teammate: TeammateManager):
        self.teammate = teammate

    def run(self, input_object: BaseModel) -> str:
        return self.teammate.teams()