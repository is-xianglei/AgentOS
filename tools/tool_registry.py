from tools import SkillRegistry
from tools.builtin.compact import CompactTool
from tools.builtin.task import TaskTool, TaskCreateTool, TaskUpdateTool, TaskListTool
from tools.subagents.agent_tool import AgentDefinitionTool
from tools.builtin.bash import BashTool
from tools.builtin.skill import SkillTool
from tools.builtin.todo import TodoTool
from tools.builtin.weather import WeatherTool
from tools.builtin.task import TaskManager
from paths import root_dir

skill_registry = SkillRegistry(skill_dir=root_dir / 'skills')
task_manager = TaskManager(tasks_dir=root_dir / 'tasks')

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
]

tools = [tool.to_param() for tool in registered_tools]

tool_handlers = {tool.name: tool for tool in registered_tools}