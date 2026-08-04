import json
from dataclasses import asdict, dataclass, replace
from typing import Any
from uuid import UUID

from pydantic import BaseModel, Field

from core.event_bus import StreamBus
from tools.base import BaseTool, ToolContext
from tools.subagents.definition import AgentType, SubAgentSpec
from tools.subagents.registry import (
    get_subagent_spec,
    routing_catalog,
    validate_verification_report,
)


@dataclass(frozen=True)
class SubAgentRunReport:
    """子代理运行结果:成功填 report,失败填 error。

    verdict / verdict_note 仅 verification 类型有值:把机器可读的验收判定与降级原因
    从正文里提出来,主代理不必自行解析报告文本,也无法把 FAIL 读成 PASS。
    """

    agent: str
    report: str | None = None
    error: str | None = None
    verdict: str | None = None
    verdict_note: str | None = None


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
    description: str | None = Field(default=None, description="3-5 个词的任务简述,便于展示与追踪")
    # 以下为 verification 的输入契约:它必须独立复核而不能复用主代理的结论,
    # 所以原始需求、变更清单等要显式传入,不能靠继承上下文。
    original_request: str | None = Field(
        default=None, description="verification 专用:用户的原始需求原文,不要改写"
    )
    changed_files: list[str] | None = Field(
        default=None, description="verification 专用:本次改动涉及的全部文件路径"
    )
    implementation_notes: str | None = Field(
        default=None, description="verification 专用:实现方法与关键取舍"
    )
    known_risks: str | None = Field(
        default=None, description="verification 专用:已知风险、未覆盖点或不确定处"
    )
    plan_path: str | None = Field(
        default=None, description="verification 专用:可选的方案文件路径,供比对是否偏离"
    )


class AgentTool(BaseTool):
    name = "Agent"
    description = (
        "启动一个子代理(SubAgent)处理聚焦的多步任务。"
        "子代理在隔离上下文中运行,完成后返回一份报告;中间过程不污染主会话。"
        "子代理无法再派生子代理,也无审批通道,需要用户确认的操作应留在主代理。\n"
        "可选类型及适用场景:\n" + routing_catalog()
    )
    input_model = AgentToolInput

    async def run(self, args: AgentToolInput, ctx: ToolContext) -> str:
        spec = get_subagent_spec(args.agent_type)
        report = await self._dispatch(
            ctx.session_id,
            self._compose_prompt(args),
            spec,
            ctx.bus,
            ctx.turn_id,
            ctx.user_id,
            ctx.workspace_id,
            ctx.allowed_tool_names,
            ctx.allowed_skill_names,
        )
        result = SubAgentRunReport(agent=args.agent_type.value, report=report)
        if args.agent_type is AgentType.VERIFICATION:
            # 在返回边界做校验:无证据的 PASS 或缺失 VERDICT 一律降级,
            # 不让"看起来通过"的报告直接进主代理的判断链。
            verdict, note = validate_verification_report(report)
            result = replace(result, verdict=verdict, verdict_note=note)
        return _dumps(result)

    @staticmethod
    def _compose_prompt(args: AgentToolInput) -> str:
        """把 verification 的结构化输入附在任务描述之后;其余类型原样透传。"""
        if args.agent_type is not AgentType.VERIFICATION:
            return args.prompt
        sections = [
            ("原始用户需求", args.original_request),
            ("变更文件清单", "\n".join(args.changed_files) if args.changed_files else None),
            ("实现方法", args.implementation_notes),
            ("已知风险", args.known_risks),
            ("方案文件路径", args.plan_path),
        ]
        parts = [args.prompt]
        for title, value in sections:
            parts.append(f"## {title}\n{value.strip() if value else '(调用方未提供)'}")
        return "\n\n".join(parts)

    async def _dispatch(
        self,
        session_id: int,
        prompt: str,
        spec: SubAgentSpec,
        bus: StreamBus | None,
        parent_turn_id: UUID | None,
        user_id: int | None,
        workspace_id: int | None,
        parent_allowed_tool_names: frozenset[str] | None,
        allowed_skill_names: frozenset[str] | None,
    ) -> str:
        """用独立 AsyncSession 运行子代理,避免与主会话共用连接。"""
        from database.engine import AsyncSessionLocal
        from runtime.subagent import SubAgentRunner

        async with AsyncSessionLocal() as sub_db:
            return await SubAgentRunner(sub_db, bus=bus).run(
                session_id,
                prompt,
                spec,
                parent_turn_id=parent_turn_id,
                user_id=user_id,
                workspace_id=workspace_id,
                parent_allowed_tool_names=parent_allowed_tool_names,
                allowed_skill_names=allowed_skill_names,
            )
