from pydantic import BaseModel

from core.errors import AgentException
from harness.domain.tool import LEAD_ONLY, PydanticTool, ToolContext, tool_policy
from interaction.schemas import AskUserQuestionRequestPayload

AskUserQuestionInput = AskUserQuestionRequestPayload


class AskUserQuestionTool(PydanticTool):
    name = "AskUserQuestion"
    description = (
        "Ask the user 1-4 multiple-choice questions to clarify requirements, preferences, "
        "trade-offs, or decisions. Users may also provide a custom answer. In plan mode, "
        "use ExitPlanMode rather than this tool to request plan approval."
    )
    input_model = AskUserQuestionInput
    # 控制平面能力，只属于主 Agent/Orchestrator。
    policy = tool_policy(
        "AskUserQuestion",
        "interaction",
        bindable_profiles=LEAD_ONLY,
        callable_profiles=LEAD_ONLY,
    )

    async def run(self, args: BaseModel, ctx: ToolContext) -> str:
        raise AgentException.message("AskUserQuestion必须由主运行时的人工交互流程处理")
