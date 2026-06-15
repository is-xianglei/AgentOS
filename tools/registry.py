from tools.agent_tools.agent_tool import AgentDefinitionTool
from tools.bash import BashTool
from tools.todo import TodoTool
from tools.weather import WeatherTool

registered_tools = [
    BashTool(),
    WeatherTool(),
    TodoTool(),
    AgentDefinitionTool(),
]

tools = [tool.to_param() for tool in registered_tools]

tool_handlers = {tool.name: tool for tool in registered_tools}