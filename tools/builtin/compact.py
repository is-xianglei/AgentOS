from anthropic.types import MessageParam
from pydantic import BaseModel
from tools.base import BaseTool


class CompactInput(BaseModel):
    messages: list[MessageParam]

class CompactTool(BaseTool):
    name = 'compact'
    description = 'Trigger manual conversation compression.'
    input_model = CompactInput

    def run(self, input_object: CompactInput) -> str:
        pass



