from pydantic import BaseModel, Field

from app.tools.base import BaseTool, ToolContext


class EchoInput(BaseModel):
    text: str = Field(description="要回显的文本")


class EchoTool(BaseTool):
    name = "echo"
    description = "回显输入文本，用于验证工具链路。"
    input_model = EchoInput

    async def run(self, args: EchoInput, ctx: ToolContext) -> str:
        return args.text
