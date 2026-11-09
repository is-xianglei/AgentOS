"""回合配置：一次运行按会话绑定解析出的模式、Agent 绑定、工具边界与模型，解析后不再变化。

configure_turn 负责查库与解析模型，TurnConfiguration 只是结果；运行时、循环策略、工具执行与人工交互
都读同一份配置，不再各自持有一组可变的 active_* 属性。
"""

from dataclasses import dataclass, replace
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from agent.service import AgentDefinitionService, AgentRuntimeConfig
from core.errors import AgentException
from harness.adapters.tools.builtin_provider import builtin_tool_set
from harness.app.ports import ModelClient, ModelSettings, ResolvedModel
from harness.app.tool_set import ToolSet
from harness.domain.review import PLAN_MODE_ALLOWED_TOOLS
from harness.domain.tool import ToolProfile
from model_service.policy import TURBO_ALLOWED_TOOLS
from session.models import SessionRecord
from tools.gateway import ToolGateway


@dataclass(frozen=True)
class TurnConfiguration:
    """一次运行的回合配置，只经工厂方法构造。

    - mode_slug：standard 或 turbo；
    - agent_id、agent_prompt、agent_model_name、skill_ids：会话绑定的自建 Agent，默认 Agent 时为 None；
    - profile、principal_name：工具档位与执行身份；allowed_skill_names 为 None 表示不限 Skill；
    - delegate_catalog：可委派 Agent 目录，只有默认 Agent 才有；
    - tools、gateway：本回合可见的工具集，以及冻结了身份与边界的执行网关；
    - model：主模型，注入客户端时取客户端自带的默认参数，按会话配置之前为 None；
    - compact_model、compact_threshold：解析出的压缩模型与阈值，注入客户端时为 None，沿用压缩服务现值；
    - database_model：模型来自工作区模型配置，此时端点自带模型名，不再用 Agent 或会话上的模型名覆盖。
    """

    mode_slug: str
    agent_id: int | None
    profile: ToolProfile
    principal_name: str
    agent_prompt: str | None
    agent_model_name: str | None
    skill_ids: tuple[int, ...] | None
    allowed_skill_names: frozenset[str] | None
    delegate_catalog: str | None
    tools: ToolSet
    gateway: ToolGateway
    model: ResolvedModel | None
    compact_model: ResolvedModel | None
    compact_threshold: int | None
    database_model: bool

    @classmethod
    def for_lead(
        cls,
        db: AsyncSession,
        *,
        mode_slug: str = "standard",
        delegate_catalog: str | None = None,
        model: ResolvedModel | None = None,
        compact_model: ResolvedModel | None = None,
        compact_threshold: int | None = None,
        database_model: bool = False,
    ) -> TurnConfiguration:
        """默认 Agent（会话未绑定自建 Agent）：完整内置工具，可以委派。"""
        return cls._assemble(
            db,
            mode_slug=mode_slug,
            agent=None,
            delegate_catalog=delegate_catalog,
            model=model,
            compact_model=compact_model,
            compact_threshold=compact_threshold,
            database_model=database_model,
        )

    @classmethod
    def for_agent(
        cls,
        db: AsyncSession,
        agent: AgentRuntimeConfig,
        *,
        mode_slug: str = "standard",
        model: ResolvedModel | None = None,
        compact_model: ResolvedModel | None = None,
        compact_threshold: int | None = None,
        database_model: bool = False,
    ) -> TurnConfiguration:
        """绑定自建 Agent 的会话：工具与 Skill 以 Agent 配置为准，不能再委派（委派深度为 1）。"""
        return cls._assemble(
            db,
            mode_slug=mode_slug,
            agent=agent,
            delegate_catalog=None,
            model=model,
            compact_model=compact_model,
            compact_threshold=compact_threshold,
            database_model=database_model,
        )

    @classmethod
    def _assemble(
        cls,
        db: AsyncSession,
        *,
        mode_slug: str,
        agent: AgentRuntimeConfig | None,
        delegate_catalog: str | None,
        model: ResolvedModel | None,
        compact_model: ResolvedModel | None,
        compact_threshold: int | None,
        database_model: bool,
    ) -> TurnConfiguration:
        base_tools = builtin_tool_set()
        if agent is None:
            agent_id = None
            profile: ToolProfile = "lead"
            principal_name = "lead"
            agent_prompt = None
            agent_model_name = None
            skill_ids = None
            allowed_skill_names = None
            tools = base_tools
        else:
            runtime_names = base_tools.callable_names(
                agent.tool_runtime_names,
                "user_agent",
                strict=True,
            )
            agent_id = agent.agent_id
            profile = "user_agent"
            # 稳定主键作为工具身份，避免使用可修改的显示名称或模型输入。
            principal_name = f"agent:{agent.agent_id}"
            agent_prompt = agent.system_prompt
            agent_model_name = agent.model_name
            skill_ids = agent.skill_ids
            allowed_skill_names = frozenset(agent.skill_names)
            tools = base_tools.only(*runtime_names)
        if mode_slug == "turbo":
            tools = tools.only(*TURBO_ALLOWED_TOOLS)
            allowed_skill_names = frozenset()
        gateway = ToolGateway.from_registry(
            db,
            tools,
            profile,
            principal_name=principal_name,
            agent_id=agent_id,
            allowed_skill_names=allowed_skill_names,
        )
        return cls(
            mode_slug=mode_slug,
            agent_id=agent_id,
            profile=profile,
            principal_name=principal_name,
            agent_prompt=agent_prompt,
            agent_model_name=agent_model_name,
            skill_ids=skill_ids,
            allowed_skill_names=allowed_skill_names,
            delegate_catalog=delegate_catalog,
            tools=tools,
            gateway=gateway,
            model=model,
            compact_model=compact_model,
            compact_threshold=compact_threshold,
            database_model=database_model,
        )

    @property
    def turbo(self) -> bool:
        """极速模式跳过 Skill、记忆、Plan Mode、上下文压缩与 Stop hook。"""
        return self.mode_slug == "turbo"

    @property
    def llm(self) -> ModelClient | None:
        """本回合的模型客户端；按会话配置之前且未注入时为 None。"""
        if self.model is None:
            return None
        return self.model.client

    def require_model(self) -> ResolvedModel:
        """取本回合的模型；运行时总在按会话配置或注入客户端之后才调模型，缺失属编码错误。"""
        if self.model is None:
            raise AgentException.message("回合配置缺少模型")
        return self.model

    def tools_for_mode(self, plan_mode: bool) -> ToolSet:
        """本轮实际生效的工具集：极速只留放行名单，Plan Mode 只留只读白名单，否则去掉两个 Plan 工具。"""
        if self.turbo:
            return self.tools.only(*TURBO_ALLOWED_TOOLS)
        if plan_mode:
            return self.tools.only(*PLAN_MODE_ALLOWED_TOOLS)
        return self.tools.without("ExitPlanMode", "WritePlan")

    def model_override(self, session: SessionRecord) -> str | None:
        """本次 LLM 调用要覆盖的模型名；None 表示使用解析器给出的场景模型。

        数据库模型服务端点自带模型名，不再用 Agent 或会话上的模型名覆盖。
        """
        if self.database_model:
            return None
        if self.agent_id is not None:
            return self.agent_model_name
        return session.model_name

    def round_settings(self, session: SessionRecord) -> ModelSettings:
        """本轮调用参数：场景默认参数，叠加 Agent 或会话指定的模型名。"""
        base = self.require_model().settings
        override = self.model_override(session)
        if override:
            return replace(base, model_id=override)
        return base

    def llm_attribution(self, settings: ModelSettings) -> tuple[str | None, str | None]:
        """返回本次 LLM 调用实际生效的 (provider, model)，用于 assistant 消息成本归因。

        provider 由客户端自报（ModelClient.provider 属性），注入的测试替身若未实现
        该属性则留空；model 取本次实际发出的模型名，替身没有模型名时留空。
        """
        provider = getattr(self.llm, "provider", None)
        return provider, settings.model_id or None


async def configure_turn(
    db: AsyncSession,
    session: SessionRecord,
    *,
    user_id: int | None,
    workspace_id: int | None,
    injected: ModelClient | None = None,
) -> TurnConfiguration:
    """按会话绑定解析本次运行的提示词、模型、Tool 与 Skill 边界。

    injected 是构造运行时注入的客户端：此时不解析模型，模型参数取客户端自带的默认值，
    也不改压缩模型与阈值，但 Agent 绑定、委派目录与工具边界照常计算。
    """
    mode_slug = getattr(session, "mode_slug", "standard")
    compact_model: ResolvedModel | None = None
    compact_threshold: int | None = None
    database_model = False
    if injected is not None:
        model = ResolvedModel.from_client(injected)
    else:
        # 局部导入：模型解析器属适配层，阶段 7 由组装根注入，运行时模块加载期不依赖它。
        from harness.adapters.models.resolver import DatabaseModelResolver

        resolver = DatabaseModelResolver(db)
        model = await resolver.for_session(session, "agent")
        database_model = model.provider_record_id is not None
        compact_model = await resolver.resolve(workspace_id, user_id, "compact")
        # 端点声明了上下文长度时，压缩阈值取其 3/4。
        if model.context_length is not None:
            compact_threshold = max(1, model.context_length * 3 // 4)
    model_fields: dict[str, Any] = {
        "mode_slug": mode_slug,
        "model": model,
        "compact_model": compact_model,
        "compact_threshold": compact_threshold,
        "database_model": database_model,
    }
    session_agent_id = getattr(session, "agent_id", None)
    if session_agent_id is None:
        # 只有默认 Agent 能委派,可委派目录只在这个分支加载。
        delegate_catalog = await load_delegate_catalog(db, workspace_id)
        return TurnConfiguration.for_lead(db, delegate_catalog=delegate_catalog, **model_fields)
    if workspace_id is None:
        raise AgentException.message("缺少可信工作区上下文")
    agent = await AgentDefinitionService(db).get_runtime_config(workspace_id, session_agent_id)
    return TurnConfiguration.for_agent(db, agent, **model_fields)


async def load_delegate_catalog(db: AsyncSession, workspace_id: int | None) -> str | None:
    """渲染默认 Agent 的可委派 Agent 目录:当前工作区每个已启用的 Agent 占一行。

    没有可信工作区或工作区里没有已启用的 Agent 时返回 None,系统提示不注入该段。
    """
    if workspace_id is None:
        return None
    entries = await AgentDefinitionService(db).list_delegate_catalog(workspace_id)
    lines: list[str] = []
    for entry in entries:
        # 描述可能含换行,压成一行,保证目录里一个 Agent 只占一行。
        description = "(无描述)"
        if entry.description and entry.description.strip():
            description = " ".join(entry.description.split())
        tools = "(无)"
        if entry.tool_names:
            tools = "、".join(entry.tool_names)
        lines.append(
            f"- agent_id={entry.agent_id} 名称:{entry.name} 描述:{description} 工具:{tools}"
        )
    if not lines:
        return None
    return "\n".join(lines)
