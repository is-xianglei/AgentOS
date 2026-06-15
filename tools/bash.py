from pydantic import BaseModel, Field

from tools.base import BaseTool

class BashInput(BaseModel):
    command: str = Field(description='Shell command to run')

class BashTool(BaseTool):
    name = 'Bash'
    description = 'description": "Run a shell command.'
    input_model = BashInput

    def run(self, input_object: BashInput) -> str:
        return f"bash -c '{input_object.command}'"