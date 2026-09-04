"""Workflow 编排运行时:把数据库定义的 Workflow 按模式编排成一次流式会话轮次。

与 AgentRuntime 同构(生产者-消费者 + StreamBus),成员执行复用 SubAgentRunner,
编排层负责成员顺序、输入拼装、并发隔离与汇总/路由,并以 workflow_progress 事件
上报节点状态(成员 running/done/failed 与路由、汇总阶段),供前端实时渲染工作流图。

会话语义(D2):会话一旦锁定 workflow,后续每一轮追问都会重跑整个编排,
成本高于普通 Agent 会话,这是既定取舍。
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator, Sequence
from dataclasses import dataclass
from typing import Any
from uuid import UUID

from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from agent.service import AgentRuntimeConfig, AgentService
from core.errors import AgentException
from core.event_bus import StreamBus
from core.events import (
    ORCHESTRATOR_ACTOR,
    BlockType,
    ErrorInfo,
    RuntimeEvent,
    StreamEvent,
    WorkflowPhase,
    WorkflowProgressInfo,
    WorkflowProgressStatus,
)
from database.engine import AsyncSessionLocal
from llm import BaseLLMClient, create_llm_client
from llm.types import build_assistant_blocks
from runtime.stream_assembler import TurnEventAssembler
from runtime.subagent import SubAgentRunner
from session.models import SessionRecord, SessionTurnRecord
from session.service import SessionService
from tools.subagents.definition import AgentType, SubAgentSpec
from workflow.models import WorkflowMode
from workflow.service import WorkflowRunMember, WorkflowService

logger = logging.getLogger(__name__)

# 顺序模式第 N(N>=2)个成员的输入模板(D4):原始提问 + 上一步输出,不累积更早的输出,
# 上下文开销恒定;第 1 个成员只收到原始提问,是同一规则的自然边界。
SEQUENTIAL_HANDOFF_PROMPT_TEMPLATE = (
    "【用户原始提问】\n{user_content}\n\n"
    "【上一步成员「{previous_member}」的输出】\n{previous_output}\n\n"
    "请在上一步输出的基础上继续完成用户的原始提问。"
)

# 并行模式的汇总提示词(D3):由编排层内部调 LLM 合并,而非文本拼接。
PARALLEL_MERGE_SYSTEM_PROMPT = (
    "你是多智能体协作的汇总者。多个成员已针对同一提问各自独立给出结果,"
    "请把它们整合成一份面向用户的最终回答:合并一致的结论,标出彼此矛盾之处并给出判断,"
    "去除重复内容,不要罗列成员名称,不要提及汇总过程。"
)
PARALLEL_MERGE_PROMPT_TEMPLATE = (
    "【用户原始提问】\n{user_content}\n\n【各成员输出】\n{member_outputs}"
)
PARALLEL_MEMBER_OUTPUT_TEMPLATE = "--- 成员「{member_name}」---\n{output}"

# 智能路由的决策提示词(D3):以成员 description 为依据,输出受约束的结构化选择。
LLM_ROUTING_SYSTEM_PROMPT = (
    "你是多智能体系统的路由器。根据用户提问与各候选成员的描述,选出最适合处理该提问的"
    "唯一一个成员。只能从候选列表中选择,并以结构化结果返回其 agent_id。"
)
LLM_ROUTING_PROMPT_TEMPLATE = "【用户提问】\n{user_content}\n\n【候选成员】\n{candidates}"
LLM_ROUTING_CANDIDATE_TEMPLATE = "- agent_id={agent_id} 名称:{name} 描述:{description}"

# 编排者最终文本块的 index:编排者一个回合只发一个文本块,成员事件由各自 actor 独立编号。
_FINAL_TEXT_BLOCK_INDEX = 0

# 进度事件枚举的短别名,只为缩短编排器里的调用行,不另造字符串常量。
_MEMBER = WorkflowPhase.MEMBER
_ROUTING = WorkflowPhase.ROUTING
_MERGING = WorkflowPhase.MERGING
_RUNNING = WorkflowProgressStatus.RUNNING
_DONE = WorkflowProgressStatus.DONE
_FAILED = WorkflowProgressStatus.FAILED

# 成员运行标识前缀与长度上限:display_name 会落 subagent_runs.member_name(String(120)),
# 而 agents.name 允许 128 字符,拼上前缀后必须截断,否则运行记录写入失败。
MEMBER_DISPLAY_NAME_PREFIX = "wf-"
MEMBER_DISPLAY_NAME_MAX_LENGTH = 120


def build_member_display_name(agent_name: str) -> str:
    """构造成员运行标识 wf-<name>,并截断到运行记录表列宽以内。"""
    return f"{MEMBER_DISPLAY_NAME_PREFIX}{agent_name}"[:MEMBER_DISPLAY_NAME_MAX_LENGTH]


class _RoutingChoice(BaseModel):
    """路由决策的受约束输出:只允许返回一个候选成员的 agent_id。"""

    agent_id: int = Field(description="被选中成员的 agent_id，必须来自候选列表")


@dataclass(frozen=True)
class OrchestrationResult:
    """编排产出:最终文本及成本归因。

    只有编排层自己调 LLM 产出的文本(并行汇总)才有 provider/model;
    成员自身产出的文本由 SubAgentRunner 负责其运行记录,这里不重复归因。
    """

    text: str
    provider: str | None = None
    model: str | None = None


class BaseOrchestrator:
    """三种编排器的公共骨架:成员配置加载、成员执行与最终文本推送。

    依赖全部以关键字传入,便于测试注入替身;子类只实现 run()。
    """

    def __init__(
        self,
        *,
        db: AsyncSession,
        bus: StreamBus,
        user_id: int,
        workspace_id: int,
        session_id: int,
        turn_id: UUID,
        workflow_id: int,
        members: Sequence[WorkflowRunMember],
    ) -> None:
        self.db = db
        self.bus = bus
        self.user_id = user_id
        self.workspace_id = workspace_id
        self.session_id = session_id
        self.turn_id = turn_id
        self.workflow_id = workflow_id
        self.members = tuple(members)

    async def run(self, user_content: str) -> OrchestrationResult:
        """执行编排并返回最终产出;最终文本由子类负责推送到 bus。"""
        raise NotImplementedError

    def _require_min_members(self, minimum: int, mode_label: str) -> None:
        """运行期模式最小成员数校验(AC6);保存期的「至少 1 个」由 WorkflowService 负责。"""
        if len(self.members) < minimum:
            raise AgentException.message(
                f"{mode_label}至少需要{minimum}个成员Agent",
                {"workflow_id": self.workflow_id, "member_count": len(self.members)},
            )

    async def _load_member_config(self, db: AsyncSession, agent_id: int) -> AgentRuntimeConfig:
        """经 agent 域取成员运行配置;内部已 require_runnable,停用/软删成员在此抛错(AC7)。

        三个编排器都经此加载配置,故配置期失败的 member/failed 统一在这里发:此时没有
        running 配对、拿不到 Agent 名(软删/不存在根本没有记录),事件只带 agent_id;
        Sequential 的 ordinal/total 也不带——失败节点无需序号,前端按 agent_id 归节点即可。
        取消不是节点失败,不发 failed。
        """
        try:
            return await AgentService(db).get_runtime_config(self.workspace_id, agent_id)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            await self._emit_progress(_MEMBER, _FAILED, agent_id=agent_id)
            if isinstance(exc, AgentException):
                # 补上是哪个成员,agent 域的原始消息本身不带 Workflow 上下文。
                raise AgentException.message(
                    f"Workflow成员不可运行：{exc.message}",
                    {"workflow_id": self.workflow_id, "agent_id": agent_id},
                    status_code=exc.status_code,
                ) from exc
            raise

    async def _execute_member(
        self,
        db: AsyncSession,
        config: AgentRuntimeConfig,
        prompt: str,
    ) -> str:
        """把数据库 Agent 桥接成 SubAgentSpec 交给 SubAgentRunner 执行,返回其最终报告。

        agent_type 只表示能力档位,display_name 才是运行标识(落运行记录、发事件、判权限),
        形如 wf-<name>,不会命中内置子代理的权限规则。失败详情由 SubAgentRunner 与
        上层 logger.exception 记录,这里只抛不含内部细节的中文业务异常。
        """
        spec = SubAgentSpec(
            agent_type=AgentType.GENERAL_PURPOSE,
            display_name=build_member_display_name(config.name),
            agent_id=config.agent_id,
            system_prompt=config.system_prompt,
            allowed_tools=config.tool_runtime_names,
            model=config.model_name,
        )
        try:
            return await SubAgentRunner(db, bus=self.bus).run(
                self.session_id,
                prompt,
                spec,
                parent_turn_id=self.turn_id,
                user_id=self.user_id,
                workspace_id=self.workspace_id,
                allowed_skill_names=frozenset(config.skill_names),
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            raise AgentException.message(
                "Workflow成员执行失败",
                {"workflow_id": self.workflow_id, "member_name": config.name},
            ) from exc

    async def run_member(
        self,
        db: AsyncSession,
        agent_id: int,
        prompt: str,
        *,
        ordinal: int | None = None,
        total: int | None = None,
    ) -> tuple[AgentRuntimeConfig, str]:
        """成员执行的公共步骤:取配置 → 带进度执行,返回 (配置, 输出) 供调用方取成员名。

        配置期失败(成员停用/软删/不存在)由 _load_member_config 发 member/failed,
        整体失败仍由 WorkflowRuntime._produce 的 error 事件承担。
        """
        config = await self._load_member_config(db, agent_id)
        output = await self._run_member_with_progress(
            db, config, prompt, ordinal=ordinal, total=total
        )
        return config, output

    async def _emit_progress(
        self,
        phase: WorkflowPhase,
        status: WorkflowProgressStatus,
        *,
        config: AgentRuntimeConfig | None = None,
        agent_id: int | None = None,
        ordinal: int | None = None,
        total: int | None = None,
    ) -> None:
        """以编排者身份推送进度事件。

        member 阶段优先从 config 取 agent_id 与 Agent 原名;配置期失败没有 config,
        改用显式 agent_id,member_name 留空。
        """
        await self.bus.emit(
            StreamEvent.workflow_progress(
                ORCHESTRATOR_ACTOR,
                self.session_id,
                WorkflowProgressInfo(
                    phase=phase,
                    status=status,
                    agent_id=config.agent_id if config else agent_id,
                    member_name=config.name if config else None,
                    ordinal=ordinal,
                    total=total,
                ),
            )
        )

    async def _run_member_with_progress(
        self,
        db: AsyncSession,
        config: AgentRuntimeConfig,
        prompt: str,
        *,
        ordinal: int | None = None,
        total: int | None = None,
    ) -> str:
        """成员执行外包一层 running/done/failed 进度;取消不发 failed,保持既有取消语义。

        运行期 failed 与已发出的 running 配对;配置期 failed 由 _load_member_config 单独发出。
        """
        await self._emit_progress(_MEMBER, _RUNNING, config=config, ordinal=ordinal, total=total)
        try:
            output = await self._execute_member(db, config, prompt)
        except asyncio.CancelledError:
            raise
        except Exception:
            await self._emit_progress(_MEMBER, _FAILED, config=config, ordinal=ordinal, total=total)
            raise
        await self._emit_progress(_MEMBER, _DONE, config=config, ordinal=ordinal, total=total)
        return output

    async def _emit_final_text(self, text: str) -> None:
        """把最终文本作为编排者的一个文本块推给前端;成员过程事件已由 SubAgentRunner 自发。"""
        await self.bus.emit(
            StreamEvent.block_start(
                ORCHESTRATOR_ACTOR,
                self.session_id,
                _FINAL_TEXT_BLOCK_INDEX,
                BlockType.TEXT,
            )
        )
        await self.bus.emit(
            StreamEvent.text_delta(
                ORCHESTRATOR_ACTOR,
                self.session_id,
                _FINAL_TEXT_BLOCK_INDEX,
                text,
            )
        )
        await self.bus.emit(
            StreamEvent.block_stop(ORCHESTRATOR_ACTOR, self.session_id, _FINAL_TEXT_BLOCK_INDEX)
        )


class SequentialOrchestrator(BaseOrchestrator):
    """顺序编排:成员按 (sort_order, agent_id) 依次执行,任一成员失败即中止后续(D5)。

    串行执行,沿用运行时自身的 Session;不调 LLM,故不创建客户端。
    """

    async def run(self, user_content: str) -> OrchestrationResult:
        previous_member: str | None = None
        previous_output: str | None = None
        total = len(self.members)
        for ordinal, member in enumerate(self.members, start=1):
            prompt = (
                user_content
                if previous_output is None
                else SEQUENTIAL_HANDOFF_PROMPT_TEMPLATE.format(
                    user_content=user_content,
                    previous_member=previous_member,
                    previous_output=previous_output,
                )
            )
            try:
                config, previous_output = await self.run_member(
                    self.db, member.agent_id, prompt, ordinal=ordinal, total=total
                )
            except AgentException as exc:
                # 下游拿不到输入,继续执行无意义;抛错即中止,后续成员不再执行。
                raise AgentException.message(
                    f"顺序编排已中止：{exc.message}",
                    {**exc.details, "workflow_id": self.workflow_id},
                    status_code=exc.status_code,
                ) from exc
            previous_member = config.name
        if previous_output is None:
            # 保存期已强制至少 1 个成员,这里兜住成员被物理清空等异常数据,不返回空结果冒充成功。
            raise AgentException.message(
                "顺序编排没有可执行的成员Agent",
                {"workflow_id": self.workflow_id},
            )
        await self._emit_final_text(previous_output)
        return OrchestrationResult(text=previous_output)


class ParallelOrchestrator(BaseOrchestrator):
    """并行编排:全部成员并发执行同一提问,成功结果由 LLM 汇总。

    失败语义(D5)分两段:配置期(成员停用/软删/不存在)任一失败即整体失败(AC7),
    此时尚未派发任何成员,只有该成员一条 member/failed;运行期(SubAgentRunner 执行失败)
    部分容忍,只对成功结果汇总。
    """

    def __init__(self, *, llm: BaseLLMClient | None = None, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.llm = llm or create_llm_client(scenario="workflow")

    async def run(self, user_content: str) -> OrchestrationResult:
        self._require_min_members(2, "并行编排")
        # 配置期串行加载全部成员(用主 db):不可运行的成员在派发前就整体抛错,
        # 若放进成员协程里静默跳过,bus 上零事件,前端无从得知该成员被略过。
        configs = [
            await self._load_member_config(self.db, member.agent_id) for member in self.members
        ]
        # 派发前对全部成员发 running:前端一次性点亮所有节点,而不是随各协程调度顺序零散出现。
        for config in configs:
            await self._emit_progress(_MEMBER, _RUNNING, config=config)
        results = await asyncio.gather(
            *(self._run_member_isolated(config, user_content) for config in configs),
            return_exceptions=True,
        )
        succeeded: list[tuple[str, str]] = []
        for outcome in results:
            if isinstance(outcome, asyncio.CancelledError):
                raise outcome
            if isinstance(outcome, BaseException):
                # 失败成员已在 _run_member_isolated 记录服务端日志并发 failed 进度,这里只统计不再上抛。
                continue
            succeeded.append(outcome)
        if not succeeded:
            # 全败不进入汇总阶段,也不发 merging 事件,与实际执行路径一致。
            raise AgentException.message(
                "并行编排的全部成员均执行失败",
                {"workflow_id": self.workflow_id, "member_count": len(self.members)},
            )
        await self._emit_progress(_MERGING, _RUNNING)
        try:
            result = await self._merge(user_content, succeeded)
        except asyncio.CancelledError:
            raise
        except Exception:
            await self._emit_progress(_MERGING, _FAILED)
            raise
        await self._emit_progress(_MERGING, _DONE)
        return result

    async def _run_member_isolated(
        self,
        config: AgentRuntimeConfig,
        prompt: str,
    ) -> tuple[str, str]:
        """在独立数据库 Session 中执行单个已加载配置的成员,返回 (成员名, 输出)。

        AsyncSession 非并发安全,而 SubAgentRunner 持有 db,故每个并发成员必须用
        AsyncSessionLocal 开独立 Session;只共享 self.bus,让各成员增量流到同一条 SSE。
        running 进度已由 run() 在派发前统一发出,这里只负责终态 done / failed。
        """
        async with AsyncSessionLocal() as sub_db:
            try:
                output = await self._execute_member(sub_db, config, prompt)
            except asyncio.CancelledError:
                raise
            except Exception:
                # 运行期部分失败不中止其余成员;异常详情只进日志,不拼进汇总提示词、不透出响应。
                logger.exception(
                    "并行编排成员执行失败，已隔离该成员并继续汇总其余结果",
                    extra={
                        "workflow_id": self.workflow_id,
                        "session_id": self.session_id,
                        "turn_id": str(self.turn_id),
                        "agent_id": config.agent_id,
                        "member_name": config.name,
                    },
                )
                await self._emit_progress(_MEMBER, _FAILED, config=config)
                raise
            await self._emit_progress(_MEMBER, _DONE, config=config)
            return config.name, output

    async def _merge(
        self,
        user_content: str,
        succeeded: Sequence[tuple[str, str]],
    ) -> OrchestrationResult:
        """调 LLM 把各成功成员的输出合并成单一回答,并以编排者身份流式推送到前端。"""
        member_outputs = "\n\n".join(
            PARALLEL_MEMBER_OUTPUT_TEMPLATE.format(member_name=name, output=output)
            for name, output in succeeded
        )
        prompt = PARALLEL_MERGE_PROMPT_TEMPLATE.format(
            user_content=user_content,
            member_outputs=member_outputs,
        )
        # 汇总文本就是本轮最终回答,用装配器走既有 block/text_delta 事件让前端逐字渲染。
        assembler = TurnEventAssembler(ORCHESTRATOR_ACTOR, self.session_id, turn_id=self.turn_id)
        stream = self.llm.stream(
            [{"role": "user", "content": prompt}],
            system_prompt=PARALLEL_MERGE_SYSTEM_PROMPT,
        )
        async for event in assembler.consume(stream):
            await self.bus.emit(event)
        text = assembler.last_result.text
        if not text:
            raise AgentException.message(
                "并行编排汇总失败：LLM 未产出汇总文本",
                {"workflow_id": self.workflow_id},
            )
        # 注入的测试替身可能不带 provider/model 属性,归因留空,与 AgentRuntime 的处理一致。
        return OrchestrationResult(
            text=text,
            provider=getattr(self.llm, "provider", None),
            model=getattr(self.llm, "model", None),
        )


class LlmRoutingOrchestrator(BaseOrchestrator):
    """智能路由:LLM 依据成员 description 选出唯一成员执行;决策或执行失败均为整体失败(D5)。"""

    def __init__(self, *, llm: BaseLLMClient | None = None, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.llm = llm or create_llm_client(scenario="workflow")

    async def run(self, user_content: str) -> OrchestrationResult:
        self._require_min_members(2, "智能路由编排")
        # 路由依据是各成员的 description,需先加载全部成员配置;顺带完成 AC7 的可运行校验。
        configs = [
            await self._load_member_config(self.db, member.agent_id) for member in self.members
        ]
        await self._emit_progress(_ROUTING, _RUNNING)
        try:
            chosen = await self._route(user_content, configs)
        except asyncio.CancelledError:
            raise
        except Exception:
            # _route 内部已把 LLM 异常与「不在成员列表」都转成 AgentException,这里只补节点状态。
            await self._emit_progress(_ROUTING, _FAILED)
            raise
        await self._emit_progress(_ROUTING, _DONE, config=chosen)
        output = await self._run_member_with_progress(self.db, chosen, user_content)
        await self._emit_final_text(output)
        return OrchestrationResult(text=output)

    async def _route(
        self,
        user_content: str,
        configs: Sequence[AgentRuntimeConfig],
    ) -> AgentRuntimeConfig:
        """让 LLM 在候选成员中选出恰好一个;定位不到直接抛错,不回落第一个成员。"""
        candidates = "\n".join(
            LLM_ROUTING_CANDIDATE_TEMPLATE.format(
                agent_id=config.agent_id,
                name=config.name,
                description=config.description or "（无描述）",
            )
            for config in configs
        )
        prompt = LLM_ROUTING_PROMPT_TEMPLATE.format(
            user_content=user_content, candidates=candidates
        )
        try:
            # 结构化输出只有一个整数字段,沿用接口默认的 max_tokens 即可。
            # 超时不在此处单独设置:客户端由 create_llm_client 构造时已带 SDK 层超时
            # ({provider}_timeout_seconds,留空则由 SDK 自动计算),此处再叠一层需新增配置项,超出本次范围。
            choice = await self.llm.complete_structured(
                [{"role": "user", "content": prompt}],
                _RoutingChoice,
                system_prompt=LLM_ROUTING_SYSTEM_PROMPT,
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            raise AgentException.message(
                "智能路由决策失败",
                {"workflow_id": self.workflow_id},
            ) from exc
        chosen = next((config for config in configs if config.agent_id == choice.agent_id), None)
        if chosen is None:
            raise AgentException.message(
                "智能路由选出的Agent不在Workflow成员列表中",
                {"workflow_id": self.workflow_id, "chosen_agent_id": choice.agent_id},
            )
        logger.info(
            "智能路由已选定成员",
            extra={
                "workflow_id": self.workflow_id,
                "session_id": self.session_id,
                "turn_id": str(self.turn_id),
                "agent_id": chosen.agent_id,
            },
        )
        return chosen


# 模式 → 编排器;新增模式时在此登记,未登记的模式由运行时抛中文异常而非静默回落。
ORCHESTRATOR_CLASSES: dict[WorkflowMode, type[BaseOrchestrator]] = {
    WorkflowMode.SEQUENTIAL: SequentialOrchestrator,
    WorkflowMode.PARALLEL: ParallelOrchestrator,
    WorkflowMode.LLM_ROUTING: LlmRoutingOrchestrator,
}


class WorkflowRuntime:
    """Workflow 会话运行时:一次发消息 = 一次完整编排,产出与 AgentRuntime 同形的事件流。"""

    def __init__(
        self,
        db: AsyncSession,
        user_id: int | None = None,
        workspace_id: int | None = None,
        llm: BaseLLMClient | None = None,
    ):
        """初始化运行时依赖;llm 仅供需要汇总/路由的编排器使用,顺序模式不创建客户端。"""
        self.db = db
        self.user_id = user_id
        self.workspace_id = workspace_id
        self.llm = llm
        self.session_service = SessionService(db)
        self.bus = StreamBus()

    async def run(
        self,
        session_id: int | None,
        user_content: str,
        *,
        workflow_id: int,
    ) -> AsyncIterator[dict[str, Any]]:
        """执行一次 Workflow 会话消息并产出流式事件。

        与 AgentRuntime.run 同构:后台任务跑编排并把事件 emit 到 bus,本协程抽干后 yield 给 SSE。
        """
        producer = asyncio.create_task(self._produce(session_id, user_content, workflow_id))
        try:
            async for event in self.bus.stream():
                yield event
        finally:
            if not producer.done():
                # 正常完成时 producer 已结束,走到这里说明事件流被消费方提前中断。
                logger.warning(
                    "Workflow事件流提前中断，取消编排任务，客户端可能已断开连接",
                    extra={"session_id": session_id, "workflow_id": workflow_id},
                )
                producer.cancel()
            try:
                await producer
            except asyncio.CancelledError:
                pass

    async def _produce(
        self,
        session_id: int | None,
        user_content: str,
        workflow_id: int,
    ) -> None:
        """后台生产者:取/建会话 → 落用户消息 → 编排 → 落最终回复,所有事件 emit 到 bus。"""
        turn: SessionTurnRecord | None = None
        try:
            if self.user_id is None or self.workspace_id is None:
                raise AgentException.message("缺少可信用户或工作区上下文")
            session: SessionRecord = await self.session_service.prepare_for_message(
                session_id,
                user_content,
                self.user_id,
                self.workspace_id,
                workflow_id=workflow_id,
            )
            session_id = session.id
            turn, _ = await self.session_service.start_turn(
                session,
                self.user_id,
                self.workspace_id,
                user_content,
            )
            # 流式执行跨越请求依赖的 yield 生命周期,原始输入与 Turn 必须先可靠提交。
            await self.db.commit()

            # 会话级控制事件:前端据此拿 session 元信息建立 UI。
            await self.bus.emit(
                RuntimeEvent.session_ready(session.id, session.title, session.status)
            )

            orchestrator = await self._build_orchestrator(session.id, turn.id, workflow_id)
            await self.bus.emit(StreamEvent.turn_start(ORCHESTRATOR_ACTOR, session.id))
            result = await orchestrator.run(user_content)
            await self.bus.emit(
                StreamEvent.turn_end(ORCHESTRATOR_ACTOR, session.id, "end_turn", None)
            )

            # 落库中立块:库中不存任何 provider wire 形状。
            assistant_message = await self.session_service.add_message(
                session.id,
                "assistant",
                build_assistant_blocks(result.text, []),
                turn_id=turn.id,
                provider=result.provider,
                model=result.model,
            )
            await self._complete_turn(session, turn, assistant_message.id)
        except asyncio.CancelledError:
            logger.warning(
                "Workflow会话处理被取消，客户端可能已断开连接",
                extra={
                    "session_id": session_id,
                    "turn_id": str(turn.id if turn else ""),
                    "workflow_id": workflow_id,
                },
            )
            await self._mark_abnormal_end(session_id, turn.id if turn else None, "interrupted")
            raise
        except Exception as exc:
            # 异常详情只进服务端日志;推给客户端的只有业务消息,内部错误一律用统一文案。
            logger.exception(
                "处理Workflow会话消息失败",
                extra={
                    "session_id": session_id,
                    "turn_id": str(turn.id if turn else ""),
                    "workflow_id": workflow_id,
                },
            )
            await self._mark_abnormal_end(session_id, turn.id if turn else None, "failed")
            if session_id is not None:
                message = exc.message if isinstance(exc, AgentException) else "Workflow执行失败。"
                await self.bus.emit(
                    StreamEvent.error_event(
                        ORCHESTRATOR_ACTOR,
                        session_id,
                        ErrorInfo(code="WORKFLOW_RUN_FAILED", message=message, fatal=True),
                    )
                )
        finally:
            await self.bus.close()

    async def _build_orchestrator(
        self,
        session_id: int,
        turn_id: UUID,
        workflow_id: int,
    ) -> BaseOrchestrator:
        """按 Workflow 模式装配编排器;成员序列由 workflow 域给出,已按 (sort_order, agent_id) 排好。"""
        if self.user_id is None or self.workspace_id is None:
            raise AgentException.message("缺少可信用户或工作区上下文")
        workflow_service = WorkflowService(self.db)
        workflow = await workflow_service.require_runnable(workflow_id, self.workspace_id)
        members = await workflow_service.get_run_members(workflow_id, self.workspace_id)
        try:
            mode = WorkflowMode(workflow.mode)
        except ValueError as exc:
            raise AgentException.message(
                "Workflow编排模式无效",
                {"workflow_id": workflow_id, "mode": workflow.mode},
            ) from exc
        orchestrator_cls = ORCHESTRATOR_CLASSES.get(mode)
        if orchestrator_cls is None:
            raise AgentException.message(
                "Workflow编排模式尚未实现",
                {"workflow_id": workflow_id, "mode": mode.value},
            )
        dependencies: dict[str, Any] = {
            "db": self.db,
            "bus": self.bus,
            "user_id": self.user_id,
            "workspace_id": self.workspace_id,
            "session_id": session_id,
            "turn_id": turn_id,
            "workflow_id": workflow_id,
            "members": members,
        }
        if issubclass(orchestrator_cls, ParallelOrchestrator | LlmRoutingOrchestrator):
            return orchestrator_cls(llm=self.llm, **dependencies)
        return orchestrator_cls(**dependencies)

    async def _complete_turn(
        self,
        session: SessionRecord,
        turn: SessionTurnRecord,
        completed_message_id: int,
    ) -> None:
        """原子完成 Turn 并恢复 Session。"""
        await self.session_service.mark_turn_status(
            turn,
            "completed",
            completed_message_id=completed_message_id,
        )
        await self.session_service.repo.update_status(session, "idle")
        # 流式运行需要在生产者生命周期内明确提交最终状态,不能依赖请求收尾。
        await self.db.commit()
        logger.debug(
            "Workflow回合完成状态已提交",
            extra={"session_id": str(session.id), "turn_id": str(turn.id)},
        )

    async def _mark_abnormal_end(
        self,
        session_id: int | None,
        turn_id: UUID | None,
        turn_status: str,
    ) -> None:
        """回滚当前失败事务,再尽力持久化 Turn 和 Session 的异常终态。"""
        await self.db.rollback()
        if session_id is None or turn_id is None:
            return
        try:
            session = await self.session_service.repo.get(session_id)
            turn = await self.session_service.repo.get_turn(turn_id)
            if session is None or turn is None or turn.status == "completed":
                return
            await self.session_service.mark_turn_status(turn, turn_status)
            await self.session_service.repo.update_status(session, "failed")
            await self.db.commit()
        except Exception:
            await self.db.rollback()
            logger.exception(
                "持久化Workflow轮次异常终态失败",
                extra={"session_id": session_id, "turn_id": str(turn_id)},
            )
