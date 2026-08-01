from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from core.errors import AgentException
from plan.service import PlanService
from tools.base import BaseTool, ToolContext


class EnterPlanModeInput(BaseModel):
    model_config = ConfigDict(extra="forbid")


class AllowedPrompt(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    tool: Literal["Bash"] = Field(description="语义权限对应的工具，目前仅支持Bash声明")
    prompt: str = Field(
        min_length=1,
        max_length=1000,
        description="动作类别的语义描述，不是具体命令",
    )


class ExitPlanModeInput(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    allowed_prompts: list[AllowedPrompt] | None = Field(
        default=None,
        alias="allowedPrompts",
        max_length=64,
        description="实现计划可能需要的语义权限声明；仅展示和持久化，不自动授权",
    )


class WritePlanInput(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    plan: str = Field(
        min_length=1,
        max_length=500_000,
        description="完整的Markdown计划正文；每次调用替换当前版本",
    )


class EnterPlanModeTool(BaseTool):
    name = "EnterPlanMode"
    description = (
        "在执行复杂或需求不明确的实现任务前，请求用户允许进入只读的计划模式。"
    )
    input_model = EnterPlanModeInput

    async def run(self, args: BaseModel, ctx: ToolContext) -> str:
        raise AgentException.message("EnterPlanMode必须由主运行时的人工交互流程处理")


class ExitPlanModeTool(BaseTool):
    name = "ExitPlanMode"
    description = (
        "向用户展示已持久化的计划以供审批，并请求允许退出计划模式。"
        "仅可在计划编写完成后调用。"
    )
    input_model = ExitPlanModeInput

    async def run(self, args: BaseModel, ctx: ToolContext) -> str:
        raise AgentException.message("ExitPlanMode必须由主运行时的人工交互流程处理")


class WritePlanTool(BaseTool):
    name = "WritePlan"
    description = (
        "创建或完整替换已持久化的 Markdown 实施计划。"
        "这是计划模式生效期间唯一允许的写操作。"
    )
    input_model = WritePlanInput

    async def run(self, args: BaseModel, ctx: ToolContext) -> str:
        parsed = WritePlanInput.model_validate(args)
        plan = await PlanService(ctx.db).write(ctx.session_id, parsed.plan)
        return f"计划已保存，版本={plan.version}，字符数={len(plan.content)}。"
