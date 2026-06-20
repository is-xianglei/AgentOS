from tools import SkillRegistry
from tools.builtin.compact import CompactTool
from tools.builtin.task import TaskTool, TaskCreateTool, TaskUpdateTool, TaskListTool
from tools.subagents.agent_tool import AgentDefinitionTool
from tools.builtin.bash import BashTool
from tools.builtin.skill import SkillTool
from tools.builtin.weather import WeatherTool
from tools.builtin.task import TaskManager
from paths import root_dir
from tools.team.agent_teams import TeamSpawnTeammateTool, TeamListTeammateTool, TeammateManager
from tools.team.team_message_bus import TeamBusSendMessageTool, TeamBusReceiveMessageTool, TeamBusBroadcastMessageTool, \
    TeamMessageBus

skill_registry = SkillRegistry(skill_dir=root_dir / 'skills')
task_manager = TaskManager(tasks_dir=root_dir / 'tasks')
team_message_bus = TeamMessageBus(mail_box_dir=root_dir / 'teams' /'mail_box')
teammate_manager = TeammateManager(team_dir=root_dir / 'teams', message_bus=team_message_bus)


registered_tools = [
    BashTool(),
    WeatherTool(),
    # TodoTool(),
    SkillTool(registry=skill_registry),
    AgentDefinitionTool(),
    CompactTool(),
    TaskTool(task_manager=task_manager),
    TaskCreateTool(task_manager=task_manager),
    TaskUpdateTool(task_manager=task_manager),
    TaskListTool(task_manager=task_manager),
    # Team
    TeamSpawnTeammateTool(teammate=teammate_manager),
    TeamListTeammateTool(teammate=teammate_manager),
    TeamBusSendMessageTool(team_message_bus=team_message_bus),
    TeamBusReceiveMessageTool(team_message_bus=team_message_bus),
    TeamBusBroadcastMessageTool(team_message_bus=team_message_bus).set_team_manager(teammate_manager),
]

tools = [tool.to_param() for tool in registered_tools]

tool_handlers = {tool.name: tool for tool in registered_tools}