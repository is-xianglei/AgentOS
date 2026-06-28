from pydantic import BaseModel, Field

from app.tools.base import BaseTool, ToolContext


class BashInput(BaseModel):
    command: str = Field(description="Shell command to run")


class BashTool(BaseTool):
    name = "Bash"
    description = "Run a shell command."
    input_model = BashInput

    async def run(self, args: BashInput, ctx: ToolContext) -> str:
        return f"bash -c '{args.command}'"
