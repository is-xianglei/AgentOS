from anthropic.types import ToolParam

from core.errors import AgentException
from tools.base import BaseTool
from tools.builtin import (
    AgentTool,
    BashTool,
    EchoTool,
    EditTool,
    GlobTool,
    GrepTool,
    ListMessagesTool,
    ReadInboxTool,
    ReadTool,
    SendMessageTool,
    SkillResourceTool,
    SkillRunTool,
    SkillTool,
    TaskCreateTool,
    TaskGetTool,
    TaskListTool,
    TaskUpdateTool,
    TeamCreateTool,
    TeamListTool,
    TeamSpawnTool,
    WeatherTool,
    WriteTool,
)


class ToolRegistry:
    def __init__(self, tools: list[BaseTool]):
        self._tools = {tool.name: tool for tool in tools}

    def to_anthropic_tools(self) -> list[ToolParam]:
        return [tool.to_param() for tool in self._tools.values()]

    def get(self, name: str) -> BaseTool:
        tool = self._tools.get(name)
        if tool is None:
            raise AgentException.message("工具不存在")
        return tool

    def without(self, *names: str) -> "ToolRegistry":
        excluded = set(names)
        return ToolRegistry([tool for name, tool in self._tools.items() if name not in excluded])

    def only(self, *names: str) -> "ToolRegistry":
        included = set(names)
        return ToolRegistry([tool for name, tool in self._tools.items() if name in included])

    def with_tools(self, *tools: BaseTool) -> "ToolRegistry":
        """在现有工具集基础上追加 / 覆盖若干工具,返回新注册表(不改原实例)。

        用于 teammate 场景:把被黑名单剔除的 Team 以受限实例补回。同名工具以新传入的为准。
        """
        merged = list(self._tools.values()) + list(tools)
        return ToolRegistry(merged)


def build_tool_registry() -> ToolRegistry:
    return ToolRegistry(
        [
            EchoTool(),
            # 文件工具
            ReadTool(),
            WriteTool(),
            EditTool(),
            GlobTool(),
            GrepTool(),
            # Shell 工具
            BashTool(),
            # 任务工具
            TaskCreateTool(),
            TaskGetTool(),
            TaskUpdateTool(),
            TaskListTool(),
            # 团队工具
            TeamCreateTool(),
            TeamSpawnTool(),
            TeamListTool(),
            SendMessageTool(),
            ReadInboxTool(),
            ListMessagesTool(),
            AgentTool(),
            WeatherTool(),
            # Skill 工具组:发现走系统提示注入(catalog),加载/读资源/执行走这三个工具。
            SkillTool(),
            SkillResourceTool(),
            SkillRunTool(),
        ]
    )
