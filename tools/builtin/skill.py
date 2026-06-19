from pydantic import BaseModel, Field
from tools.base import BaseTool
from tools.skill_registry import SkillRegistry

class SkillInput(BaseModel):
    # LLM调用时传递的skill名字
    name: str = Field(description='Name of the skill')

class SkillTool(BaseTool):
    name: str = 'Name of the skill'
    description: str = 'Description of the skill'
    input_model = SkillInput

    def __init__(self, registry: SkillRegistry):
        self.skill_registry = registry

    def run(self, input_object: SkillInput) -> str:
        return self.skill_registry.skill_content(input_object.name)