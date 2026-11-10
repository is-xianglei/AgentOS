"""组装根：唯一实例化适配器、把具体类接到运行时上的地方。

运行时（runtime/）、应用层（harness/app/）、领域层（harness/domain/）只认端口与构造参数，
换模型、换存储、换记忆、换审核员都只改这里：``Harness`` 的每个字段是一项可替换的实现，
``dataclasses.replace(default_harness(), memory=...)`` 即可只换一项。

进程级单例用模块级缓存函数持有（目前是内置工具集），并在本模块导入时登记到应用层的
``platform_tools``，供只有工具名的 feature 域按名查策略。导入本模块不连接任何外部服务：
数据库引擎惰性建连，模型客户端按调用现建。
"""

from collections.abc import Callable
from dataclasses import dataclass
from functools import cache
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from core.event_bus import StreamBus
from database.engine import AsyncSessionLocal
from harness.adapters.memory.builtin_provider import BuiltinMemoryProvider
from harness.adapters.models.factory import create_model_client, resolve_subagent_model
from harness.adapters.models.resolver import DatabaseModelResolver
from harness.adapters.reviewers.policy_reviewer import PolicyReviewer
from harness.adapters.tools.builtin_provider import builtin_tool_set
from harness.app.agent_service import AgentService
from harness.app.memory_port import MemoryProvider
from harness.app.ports import (
    ModelClient,
    ModelResolver,
    ResolvedModel,
    SubAgentLauncher,
    ToolCallReviewer,
)
from harness.app.session_port import SessionStore, TurnTermination
from harness.app.tool_set import ToolSet, register_platform_tools
from harness.domain.roles.subagent import SubAgentSpec
from permission.service import PermissionService
from runtime.agent import AgentRuntime
from runtime.execution import cancel_turn
from runtime.subagent import SessionFactory, SubAgentRunner
from runtime.workflow import WorkflowRuntime
from session.service import SessionService


@cache
def platform_tool_set() -> ToolSet:
    """进程级工具集：目前只有内置工具；接入 MCP 时在这里追加工具来源。"""
    return builtin_tool_set()


def _env_compact_model() -> ResolvedModel:
    """会话没有解析出压缩模型时，按 .env 的 compact 场景现建客户端。"""
    return ResolvedModel.from_client(create_model_client(scenario="compact"))


@dataclass(frozen=True)
class Harness:
    """一套完整装配：每个字段是一项可替换的实现，按数据库会话现建的依赖以工厂函数给出。

    - tools：完整工具集，按名查策略、按档位裁剪都以它为准；
    - session_factory：打开独立数据库会话，后台执行、队友唤醒、并发成员与子代理启动器用它；
    - sessions、memory、models、permissions：按数据库会话构造会话存储、记忆、模型解析器与权限服务；
    - main_reviewer、subagent_reviewer：基于同一份权限服务构造主 Agent 与子代理的工具调用审核员；
    - compact_model：会话没有解析出压缩模型时现建的模型；
    - subagent_model：.env 端点上按子代理规格覆盖的模型名，None 表示沿用场景模型；
    - subagent_launcher：按本装配构造 Agent 工具用的子代理启动器。
    """

    tools: ToolSet
    session_factory: SessionFactory
    sessions: Callable[[AsyncSession], SessionStore]
    memory: Callable[[AsyncSession], MemoryProvider]
    models: Callable[[AsyncSession], ModelResolver]
    permissions: Callable[[AsyncSession], PermissionService]
    main_reviewer: Callable[[PermissionService], ToolCallReviewer]
    subagent_reviewer: Callable[[PermissionService], ToolCallReviewer]
    compact_model: Callable[[], ResolvedModel]
    subagent_model: Callable[[SubAgentSpec], str | None]
    subagent_launcher: Callable[[Harness], SubAgentLauncher]

    def agent_runtime(
        self,
        db: AsyncSession,
        *,
        user_id: int | None,
        workspace_id: int | None,
        llm: ModelClient | None = None,
        reviewer: ToolCallReviewer | None = None,
        memory: MemoryProvider | None = None,
        sessions: SessionStore | None = None,
    ) -> AgentRuntime:
        """装配主 Agent 运行时；llm、reviewer、memory、sessions 是本次调用的显式覆盖项。

        后台执行在独立数据库会话里重建运行时，只沿用这里显式传入的 llm、memory、sessions；
        没传的按新会话重新构造，与请求作用域的数据库会话脱钩。审核员不随后台执行传递。
        """
        permission_service = self.permissions(db)
        resolved_reviewer = reviewer
        if resolved_reviewer is None:
            resolved_reviewer = self.main_reviewer(permission_service)
        resolved_memory = memory
        if resolved_memory is None:
            resolved_memory = self.memory(db)
        resolved_sessions = sessions
        if resolved_sessions is None:
            resolved_sessions = self.sessions(db)

        def rebuild(background_db: AsyncSession) -> AgentRuntime:
            return self.agent_runtime(
                background_db,
                user_id=user_id,
                workspace_id=workspace_id,
                llm=llm,
                memory=memory,
                sessions=sessions,
            )

        return AgentRuntime(
            db,
            user_id=user_id,
            workspace_id=workspace_id,
            llm=llm,
            reviewer=resolved_reviewer,
            memory=resolved_memory,
            sessions=resolved_sessions,
            permission_service=permission_service,
            models=self.models(db),
            tools=self.tools,
            compact_model=self.compact_model,
            subagents=self.subagent_launcher(self),
            teammates=self.subagent_runner,
            session_factory=self.session_factory,
            rebuild=rebuild,
        )

    def subagent_runner(
        self,
        db: AsyncSession,
        *,
        bus: StreamBus | None,
        llm: ModelClient | None = None,
        reviewer: ToolCallReviewer | None = None,
        memory: MemoryProvider | None = None,
        sessions: SessionStore | None = None,
    ) -> SubAgentRunner:
        """装配子代理运行器（一次性子代理、队友、Workflow 成员共用）；后四项是显式覆盖项。"""
        resolved_reviewer = reviewer
        if resolved_reviewer is None:
            resolved_reviewer = self.subagent_reviewer(self.permissions(db))
        resolved_memory = memory
        if resolved_memory is None:
            resolved_memory = self.memory(db)
        resolved_sessions = sessions
        if resolved_sessions is None:
            resolved_sessions = self.sessions(db)
        return SubAgentRunner(
            db,
            bus=bus,
            llm=llm,
            reviewer=resolved_reviewer,
            memory=resolved_memory,
            sessions=resolved_sessions,
            models=self.models(db),
            tools=self.tools,
            subagent_model=self.subagent_model,
        )

    def workflow_runtime(
        self,
        db: AsyncSession,
        *,
        user_id: int | None,
        workspace_id: int | None,
        llm: ModelClient | None = None,
        sessions: SessionStore | None = None,
    ) -> WorkflowRuntime:
        """装配 Workflow 运行时；llm、sessions 是显式覆盖项，只有它们随后台执行传递。"""
        resolved_sessions = sessions
        if resolved_sessions is None:
            resolved_sessions = self.sessions(db)

        def rebuild(background_db: AsyncSession) -> WorkflowRuntime:
            return self.workflow_runtime(
                background_db,
                user_id=user_id,
                workspace_id=workspace_id,
                llm=llm,
                sessions=sessions,
            )

        return WorkflowRuntime(
            db,
            user_id=user_id,
            workspace_id=workspace_id,
            llm=llm,
            sessions=resolved_sessions,
            models=self.models(db),
            runners=self.subagent_runner,
            session_factory=self.session_factory,
            rebuild=rebuild,
        )

    def agent_service(
        self,
        db: AsyncSession,
        *,
        user_id: int,
        workspace_id: int,
    ) -> AgentService:
        """按请求装配应用层入口：运行时绑定本次请求的数据库会话与可信用户、工作区。"""

        async def _cancel_turn(session_id: int, turn_id: UUID) -> TurnTermination:
            return await cancel_turn(db, session_id, turn_id)

        return AgentService(
            agent_runner=lambda: self.agent_runtime(
                db,
                user_id=user_id,
                workspace_id=workspace_id,
            ),
            workflow_runner=lambda: self.workflow_runtime(
                db,
                user_id=user_id,
                workspace_id=workspace_id,
            ),
            cancel_turn=_cancel_turn,
        )


class IsolatedSubAgentLauncher:
    """SubAgentLauncher 的实现：开独立数据库会话，经组装根构造子代理运行器执行。

    独立会话避免与主会话共用连接；事件经调用方的 bus 发到父回合的事件流。
    子代理运行器在自己的会话里按运行记录与报告提交，事务边界与启动器无关。
    """

    def __init__(self, harness: Harness) -> None:
        self._harness = harness

    async def run(
        self,
        session_id: int,
        prompt: str,
        spec: SubAgentSpec,
        *,
        bus: StreamBus | None,
        parent_turn_id: UUID | None,
        user_id: int | None,
        workspace_id: int | None,
        parent_allowed_tool_names: frozenset[str] | None,
        allowed_skill_names: frozenset[str] | None,
    ) -> str:
        async with self._harness.session_factory() as sub_db:
            return await self._harness.subagent_runner(sub_db, bus=bus).run(
                session_id,
                prompt,
                spec,
                parent_turn_id=parent_turn_id,
                user_id=user_id,
                workspace_id=workspace_id,
                parent_allowed_tool_names=parent_allowed_tool_names,
                allowed_skill_names=allowed_skill_names,
            )


def builtin_harness() -> Harness:
    """内置实现的完整装配：数据库会话存储、内置记忆、数据库 + .env 模型解析、规则链审核员。"""
    tools = platform_tool_set()
    return Harness(
        tools=tools,
        session_factory=AsyncSessionLocal,
        sessions=SessionService,
        memory=BuiltinMemoryProvider,
        models=DatabaseModelResolver,
        permissions=PermissionService,
        main_reviewer=lambda permissions: PolicyReviewer.for_main_agent(
            permissions, policy_source=tools
        ),
        subagent_reviewer=lambda permissions: PolicyReviewer.for_subagent(
            permissions, policy_source=tools
        ),
        compact_model=_env_compact_model,
        subagent_model=resolve_subagent_model,
        subagent_launcher=IsolatedSubAgentLauncher,
    )


@cache
def default_harness() -> Harness:
    """进程默认装配，首次取用时构造并缓存。"""
    return builtin_harness()


# 导入即登记进程级工具集：拿得到组装根，就一定能按名查到工具策略（同 database.engine 导入 registry）。
register_platform_tools(platform_tool_set())
