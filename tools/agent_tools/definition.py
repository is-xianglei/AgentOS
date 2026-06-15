from enum import Enum

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

    def resolve_agent_tools(self):

        pass

