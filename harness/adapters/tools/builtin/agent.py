import json
import logging
from dataclasses import asdict, dataclass
from uuid import UUID

from pydantic import BaseModel, Field, model_validator

from core.errors import AgentException
from core.event_bus import StreamBus
from harness.domain.tool import LEAD_ONLY, PydanticTool, ToolContext, tool_policy
from tools.subagents.definition import AgentType, SubAgentSpec
from tools.subagents.registry import (
    get_subagent_spec,
    routing_catalog,
    validate_verification_report,
)

logger = logging.getLogger(__name__)

# 被委派 Agent 的运行标识前缀:与 Workflow 成员的 wf- 区分;带前缀也避免与内置子代理同名时
# 误命中其定向权限规则(如 verification 的 Bash 放行)。
DELEGATE_DISPLAY_NAME_PREFIX = "delegate-"
# 运行标识会落 subagent_runs.member_name(String(120)),而 Agent 名允许 128 字符,拼前缀后须截断。
DELEGATE_DISPLAY_NAME_MAX_LENGTH = 120


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


_AGENT_TYPES = ", ".join(item.value for item in AgentType)


class AgentToolInput(BaseModel):
    # 默认值必须是 None 而不是 general-purpose:运行时校验入参后会按 exclude_none 重新序列化,
    # 若带默认值,只传 agent_id 的调用在执行时会被误判为两者都传了。
    agent_type: AgentType | None = Field(
        default=None,
        description=(
            f"要启动的内置子代理类型,可选: {_AGENT_TYPES};不传时为 general-purpose。"
            "委派自建 Agent 时不传,改传 agent_id"
        ),
    )
    agent_id: int | None = Field(
        default=None,
        description=(
            "委派目标:用户自建 Agent 的 agent_id,取自系统提示里的「可委派 Agent 目录」;"
            "与 agent_type 二选一"
        ),
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

    @model_validator(mode="after")
    def check_single_target(self) -> AgentToolInput:
        """agent_type 指向内置子代理,agent_id 指向自建 Agent,同时给出时无法判断调用意图。"""
        if self.agent_type is not None and self.agent_id is not None:
            raise ValueError("agent_type 与 agent_id 只能二选一:委派自建 Agent 时只传 agent_id")
        return self


class AgentTool(PydanticTool):
    name = "Agent"
    description = (
        "启动一个子代理(SubAgent)处理聚焦的多步任务。"
        "子代理在隔离上下文中运行,完成后返回一份报告;中间过程不污染主会话。"
        "子代理无法再派生子代理,也无审批通道,需要用户确认的操作应留在主代理。\n"
        "可选类型及适用场景:\n" + routing_catalog() + "\n"
        "委派自建 Agent:若系统提示中有「可委派 Agent 目录」,可只传其中的 agent_id(不传 agent_type),"
        "把任务交给用户自建的 Agent,它按自己的提示词、工具与 Skill 运行;"
        "被拒绝的工具调用会附在报告末尾,确需执行时由你在主会话中重新调用。"
    )
    input_model = AgentToolInput
    # 控制平面能力，只属于主 Agent/Orchestrator。
    policy = tool_policy(
        "Agent",
        "orchestration",
        bindable_profiles=LEAD_ONLY,
        callable_profiles=LEAD_ONLY,
    )

    async def run(self, args: AgentToolInput, ctx: ToolContext) -> str:
        if args.agent_id is not None:
            return await self._delegate(args.agent_id, args.prompt, ctx)
        agent_type = args.agent_type
        if agent_type is None:
            agent_type = AgentType.GENERAL_PURPOSE
        spec = get_subagent_spec(agent_type)
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
        verdict: str | None = None
        verdict_note: str | None = None
        if agent_type is AgentType.VERIFICATION:
            # 在返回边界做校验:无证据的 PASS 或缺失 VERDICT 一律降级,
            # 不让"看起来通过"的报告直接进主代理的判断链。
            verdict, verdict_note = validate_verification_report(report)
        result = SubAgentRunReport(
            agent=agent_type.value,
            report=report,
            verdict=verdict,
            verdict_note=verdict_note,
        )
        return json.dumps(asdict(result), ensure_ascii=False)

    async def _delegate(self, agent_id: int, prompt: str, ctx: ToolContext) -> str:
        """把任务委派给当前工作区的自建 Agent:先走 agent 域的运行配置校验,再转成 SubAgentSpec 执行。

        目标不可用(不存在、跨工作区、已停用、工具绑定失效)时,把中文原因作为结果交回模型,
        让默认 Agent 自行改道,而不是让整轮对话失败;被委派方运行中的失败与内置子代理一致,照常上抛。
        """
        # 局部导入：agent.service -> tools.service -> builtin_provider -> 本模块，顶层导入成环。
        from agent.service import AgentDefinitionService
        from runtime.subagent import build_agent_spec

        # 只有默认 Agent 能委派,深度为 1。user_agent 等档位本就调不到 Agent 工具,
        # 这里再显式拦一次,避免将来工具策略放宽时意外打开多级委派。
        if ctx.profile != "lead":
            return self._delegate_error(agent_id, "只有默认 Agent 可以委派自建 Agent")
        if ctx.workspace_id is None:
            return self._delegate_error(agent_id, "缺少可信工作区上下文")
        try:
            config = await AgentDefinitionService(ctx.db).get_runtime_config(
                ctx.workspace_id, agent_id
            )
        except AgentException as exc:
            logger.warning(
                "委派自建 Agent 失败,目标不可用,会话 ID=%s agent_id=%s 原因=%s",
                ctx.session_id,
                agent_id,
                exc.message,
                extra={"session_id": ctx.session_id, "agent_id": agent_id},
            )
            return self._delegate_error(agent_id, exc.message)

        display_name = f"{DELEGATE_DISPLAY_NAME_PREFIX}{config.name}"
        spec = build_agent_spec(
            config,
            display_name=display_name[:DELEGATE_DISPLAY_NAME_MAX_LENGTH],
            tool_profile="delegate",
        )
        # 父名单取主运行时注入的本轮生效名单(含 Plan Mode 裁剪),与 delegate 档位、
        # Agent 白名单三者取交集;未注入时退回父网关的工具边界。
        parent_tool_names = ctx.round_tool_names
        if parent_tool_names is None:
            parent_tool_names = ctx.allowed_tool_names
        report = await self._dispatch(
            ctx.session_id,
            prompt,
            spec,
            ctx.bus,
            ctx.turn_id,
            ctx.user_id,
            ctx.workspace_id,
            parent_tool_names,
            frozenset(config.skill_names),
        )
        result = SubAgentRunReport(agent=spec.runtime_name, report=report)
        return json.dumps(asdict(result), ensure_ascii=False)

    @staticmethod
    def _delegate_error(agent_id: int, message: str) -> str:
        """委派目标不可用时的结果:沿用 SubAgentRunReport 形状,error 写中文原因。"""
        result = SubAgentRunReport(
            agent=f"agent:{agent_id}",
            error=f"无法委派给 agent_id={agent_id} 的 Agent:{message}",
        )
        return json.dumps(asdict(result), ensure_ascii=False)

    @staticmethod
    def _compose_prompt(args: AgentToolInput) -> str:
        """把 verification 的结构化输入附在任务描述之后;其余类型原样透传。"""
        if args.agent_type is not AgentType.VERIFICATION:
            return args.prompt
        changed_files_text = None
        if args.changed_files:
            changed_files_text = "\n".join(args.changed_files)
        sections = [
            ("原始用户需求", args.original_request),
            ("变更文件清单", changed_files_text),
            ("实现方法", args.implementation_notes),
            ("已知风险", args.known_risks),
            ("方案文件路径", args.plan_path),
        ]
        parts = [args.prompt]
        for title, value in sections:
            # 未提供的项也保留小节标题,并显式标注为未提供。
            if value:
                body = value.strip()
            else:
                body = "(调用方未提供)"
            parts.append(f"## {title}\n{body}")
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
        # 局部导入：工具反向依赖运行时，改顶层仍成环 runtime.subagent -> builtin_provider
        # -> harness.adapters.tools.builtin.agent，待依赖倒置任务处理。
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
