from pydantic import BaseModel

from core.errors import AgentException
from interaction.schemas import AskUserQuestionRequestPayload
from tools.base import BaseTool, ToolContext

AskUserQuestionInput = AskUserQuestionRequestPayload


class AskUserQuestionTool(BaseTool):
    name = "AskUserQuestion"
    description = (
        "Ask the user 1-4 multiple-choice questions to clarify requirements, preferences, "
        "trade-offs, or decisions. Users may also provide a custom answer. In plan mode, "
        "use ExitPlanMode rather than this tool to request plan approval."
    )
    input_model = AskUserQuestionInput

    async def run(self, args: BaseModel, ctx: ToolContext) -> str:
        raise AgentException.message("AskUserQuestion必须由主运行时的人工交互流程处理")
