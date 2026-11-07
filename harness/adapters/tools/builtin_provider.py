"""内置工具来源：取代原 build_tool_registry()，按固定顺序产出全部内置工具。"""

from harness.adapters.tools.builtin import (
    AgentTool,
    AskUserQuestionTool,
    BashTool,
    EchoTool,
    EditTool,
    EnterPlanModeTool,
    ExitPlanModeTool,
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
    WritePlanTool,
    WriteTool,
)
from harness.app.ports import ToolProvider
from harness.app.tool_set import ToolSet
from harness.domain.tool import Tool


class BuiltinToolProvider(ToolProvider):
    """内置工具来源。列表顺序就是发给模型的工具顺序，提示缓存依赖它，不要随意调整。"""

    def list_tools(self) -> list[Tool]:
        return [
            EchoTool(),
            # 强制人工交互工具，仅主运行时处理，不进入普通 ToolService 执行路径。
            AskUserQuestionTool(),
            EnterPlanModeTool(),
            ExitPlanModeTool(),
            WritePlanTool(),
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


def builtin_tool_set() -> ToolSet:
    """只含内置工具的工具集；目前全部调用方都只用内置工具，阶段 7 改由组装根装配。"""
    return ToolSet.from_providers(BuiltinToolProvider())
