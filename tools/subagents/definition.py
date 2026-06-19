from enum import Enum

from anthropic.types import ToolParam
from pydantic import BaseModel, Field

# 四种内置SubAgent类型
class AgentType(str, Enum):
    PLAN = "Plan"
    EXPLORE = "Explore"
    VERIFICATION = "Verification"
    GENERAL_PURPOSE = "General_purpose"

class AgentDefinition(BaseModel):
    prompt: str = Field(description="SubAgent prompt")
    system_prompt: str = Field(default="", description="System prompt words for subagents")
    allowed_tools: tuple[str, ...] | str = "*"
    disallowed_tools: tuple[str, ...] = ("Agent",)
    agent_type: AgentType = AgentType.GENERAL_PURPOSE

    # 过滤出SubAgent都允许调用哪些工具
    def resolve_agent_tools(self) -> list[ToolParam]:
        from tools.tool_registry import tools
        if self.allowed_tools == '*':
            allowed = {t['name'] for t in tools} - set(self.disallowed_tools)
        else:
            allowed = set(self.allowed_tools) - set(self.disallowed_tools)
        return [t for t in tools if t['name'] in allowed]

