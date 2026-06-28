import json
from dataclasses import asdict, dataclass
from typing import Any

from pydantic import BaseModel, Field

from app.tools.base import BaseTool, ToolContext
from app.tools.subagents.definition import AgentType
from app.tools.subagents.registry import get_subagent_spec


@dataclass(frozen=True)
class SubAgentRunReport:
    """子代理运行结果:成功填 report,失败填 error。"""

    agent: str
    report: str | None = None
    error: str | None = None


def _dumps(obj: Any) -> str:
    """把 dataclass 结果序列化为给 LLM 的 JSON 字符串。"""
    return json.dumps(asdict(obj), ensure_ascii=False)


_AGENT_TYPES = ", ".join(item.value for item in AgentType)


class AgentToolInput(BaseModel):
    agent_type: AgentType = Field(
        default=AgentType.GENERAL_PURPOSE,
        description=f"要启动的子代理类型,可选: {_AGENT_TYPES}",
    )
    prompt: str = Field(description="分派给子代理的完整任务描述")
    description: str | None = Field(
        default=None, description="3-5 个词的任务简述,便于展示与追踪"
    )


class AgentTool(BaseTool):
    name = "Agent"
    description = (
        "启动一个子代理(SubAgent)处理聚焦的多步任务。"
        f"按 agent_type 选择子代理,可选类型: {_AGENT_TYPES}。"
        "子代理在隔离上下文中运行,完成后返回一份简洁报告;中间过程不污染主会话。"
    )
    input_model = AgentToolInput

    async def run(self, args: AgentToolInput, ctx: ToolContext) -> str:
        spec = get_subagent_spec(args.agent_type)
        report = await self._dispatch(ctx.session_id, args.prompt, spec, ctx.bus)
        return _dumps(
            SubAgentRunReport(agent=args.agent_type.value, report=report)
        )

    async def _dispatch(self, session_id: int, prompt: str, spec, bus) -> str:
        """用独立 AsyncSession 运行子代理,避免与主会话共用连接。"""
        from app.db.session import AsyncSessionLocal
        from app.services.subagent_runner import SubAgentRunner

        async with AsyncSessionLocal() as sub_db:
            return await SubAgentRunner(sub_db, bus=bus).run(session_id, prompt, spec)
