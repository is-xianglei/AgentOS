"""组装根：唯一实例化适配器、把具体类接到运行时上的地方。

运行时（runtime/）、应用层（harness/app/）、领域层（harness/domain/）只认端口与构造参数，
换模型、换存储、换记忆、换审核员都只改这里：``Harness`` 的每个字段是一项可替换的实现，
``dataclasses.replace(default_harness(), memory=...)`` 即可只换一项。

进程级单例用模块级缓存函数持有（内置工具集、MCP 运行时），内置工具集在本模块导入时登记到应用层的
``platform_tools``，供只有工具名的 feature 域按名查策略。导入本模块不连接任何外部服务：
数据库引擎惰性建连，模型客户端按调用现建，MCP 运行时首次取用时才创建、也不预先建连。
"""

from collections.abc import Callable
from dataclasses import dataclass
from functools import cache
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from core.config import settings
from core.errors import AgentException
from core.event_bus import StreamBus
from database.engine import AsyncSessionLocal
from harness.adapters.mcp.connector import McpConnector
from harness.adapters.mcp.http import McpHttpFactory, parse_allowed_hosts, system_resolver
from harness.adapters.mcp.provider import McpProviderLimits, McpToolProvider
from harness.adapters.mcp.runtime import McpRuntime, McpRuntimeLimits
from harness.adapters.memory.builtin_provider import BuiltinMemoryProvider
from harness.adapters.models.factory import create_model_client, resolve_subagent_model
from harness.adapters.models.resolver import DatabaseModelResolver
from harness.adapters.reviewers.policy_reviewer import PolicyReviewer
from harness.adapters.tools.builtin_provider import builtin_tool_set
from harness.app.agent_service import AgentService
from harness.app.mcp_admin import McpAdminService
from harness.app.mcp_port import McpServerConnector
from harness.app.memory_port import MemoryProvider
from harness.app.ports import (
    ModelClient,
    ModelResolver,
    ResolvedModel,
    ScopedToolProvider,
    SubAgentLauncher,
    ToolCallReviewer,
)
from harness.app.session_port import SessionStore, TurnTermination
from harness.app.tool_resolver import TurnToolResolver
from harness.app.tool_set import ToolSet, register_platform_tools
from harness.domain.roles.subagent import SubAgentSpec
from harness.domain.tool import ScopedGrant
from mcp_server.service import McpServerService
from mcp_server.source import McpServerCatalog
from permission.service import PermissionService
from runtime.agent import AgentRuntime
from runtime.execution import cancel_turn
from runtime.subagent import SessionFactory, SubAgentRunner
from runtime.workflow import WorkflowRuntime
from session.service import SessionService


@cache
def platform_tool_set() -> ToolSet:
    """进程级工具集：只放进程级工具（目前是内置工具）；作用域来源见 Harness.scoped_tools。"""
    return builtin_tool_set()


def _mcp_http() -> McpHttpFactory:
    """出站 HTTP 工厂：SSRF 规则来自 Settings，DNS 走系统解析；读超时取单次调用上限。"""
    return McpHttpFactory(
        allow_insecure_http=settings.mcp_allow_insecure_http,
        allowed_private_hosts=parse_allowed_hosts(settings.mcp_allowed_private_hosts),
        connect_timeout=settings.mcp_connect_timeout_seconds,
        read_timeout=settings.mcp_call_timeout_seconds,
        resolver=system_resolver,
    )


@cache
def mcp_runtime() -> McpRuntime:
    """进程级 MCP 运行时：连接池与共享响应缓存。首次取用时惰性创建，不建任何连接。"""
    return McpRuntime(
        http=_mcp_http(),
        limits=McpRuntimeLimits(
            connect_timeout=settings.mcp_connect_timeout_seconds,
            idle_seconds=settings.mcp_idle_seconds,
            pool_max_connections=settings.mcp_pool_max_connections,
            tools_cache_seconds=settings.mcp_tools_cache_seconds,
        ),
    )


async def close_mcp_runtime() -> None:
    """lifespan 退出时关闭 MCP 运行时；只有已经创建过才关，免得为了关闭反而先创建出来。"""
    if mcp_runtime.cache_info().currsize:
        await mcp_runtime().aclose()


def mcp_connector() -> McpServerConnector:
    """管理入口用的连接器：探测与缓存失效，共用进程级 MCP 运行时。"""
    return McpConnector(
        runtime=mcp_runtime(),
        probe_timeout=settings.mcp_probe_timeout_seconds,
        max_schema_bytes=settings.mcp_max_schema_bytes,
    )


def mcp_tool_provider(session_factory: SessionFactory) -> McpToolProvider:
    """MCP 作用域工具来源：用进程级运行时，按 Settings 组装上限；每回合经 session_factory
    开短会话读 server 配置与凭证（McpServerCatalog），不占用回合的事务。

    mcp_enabled 打开时由 builtin_harness() 接进 Harness.scoped_tools；构造本身不建连接。
    """
    return McpToolProvider(
        runtime=mcp_runtime(),
        servers=McpServerCatalog(session_factory),
        limits=McpProviderLimits(
            list_timeout=settings.mcp_list_timeout_seconds,
            call_timeout=settings.mcp_call_timeout_seconds,
            max_output_chars=settings.mcp_max_output_chars,
            max_schema_bytes=settings.mcp_max_schema_bytes,
        ),
    )


def _env_compact_model() -> ResolvedModel:
    """会话没有解析出压缩模型时，按 .env 的 compact 场景现建客户端。"""
    return ResolvedModel.from_client(create_model_client(scenario="compact"))


@dataclass(frozen=True)
class Harness:
    """一套完整装配：每个字段是一项可替换的实现，按数据库会话现建的依赖以工厂函数给出。

    - tools：平台工具集（进程级），按档位裁剪以它为底；按名查策略查本回合的策略全集；
    - scoped_tools：作用域工具来源（如 MCP），主 Agent 运行时每回合经 TurnToolResolver 解析；
      为空时不做任何 I/O，策略全集就是 tools；
    - session_factory：打开独立数据库会话，后台执行、队友唤醒、并发成员与子代理启动器用它；
    - sessions、memory、models、permissions：按数据库会话构造会话存储、记忆、模型解析器与权限服务；
    - main_reviewer、subagent_reviewer：基于同一份权限服务构造主 Agent 与子代理的工具调用审核员；
    - compact_model：会话没有解析出压缩模型时现建的模型；
    - subagent_model：.env 端点上按子代理规格覆盖的模型名，None 表示沿用场景模型；
    - subagent_launcher：按本装配构造 Agent 工具用的子代理启动器；
    - mcp_connector：MCP 管理入口用的连接器工厂（探测、缓存失效），None 表示本装配不提供 MCP 管理。
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
    scoped_tools: tuple[ScopedToolProvider, ...] = ()
    mcp_connector: Callable[[], McpServerConnector] | None = None

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
            tool_resolver=self.tool_resolver(),
            compact_model=self.compact_model,
            subagents=self.subagent_launcher(self),
            teammates=self.subagent_runner,
            session_factory=self.session_factory,
            rebuild=rebuild,
        )

    def tool_resolver(self) -> TurnToolResolver:
        """回合工具解析器：平台工具加本装配的作用域来源。

        只有主 Agent 与 Workflow 运行时持有它；子代理、队友、委派不重新解析，用父执行交来的 ScopedGrant。
        """
        return TurnToolResolver(
            self.tools,
            self.scoped_tools,
            total_timeout=settings.mcp_resolve_timeout_seconds,
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
            tool_resolver=self.tool_resolver(),
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

    def mcp_admin_service(
        self,
        db: AsyncSession,
        *,
        user_id: int,
        workspace_id: int,
    ) -> McpAdminService:
        """按请求装配 MCP 管理入口：server 配置读写经 McpServerService，网络动作经 mcp_connector。"""
        if self.mcp_connector is None:
            raise AgentException.message("当前装配未提供 MCP 连接器，无法管理 MCP server")
        return McpAdminService(
            registry=McpServerService(db),
            connector=self.mcp_connector(),
            workspace_id=workspace_id,
            user_id=user_id,
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
        scoped: ScopedGrant | None = None,
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
                scoped=scoped,
            )


def _scoped_tool_sources() -> tuple[ScopedToolProvider, ...]:
    """作用域工具来源：MCP 总开关打开时接上 MCP 来源，关闭时为空，回合不做任何 MCP I/O。"""
    if not settings.mcp_enabled:
        return ()
    return (mcp_tool_provider(AsyncSessionLocal),)


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
        scoped_tools=_scoped_tool_sources(),
        mcp_connector=mcp_connector,
    )


@cache
def default_harness() -> Harness:
    """进程默认装配，首次取用时构造并缓存。"""
    return builtin_harness()


# 导入即登记进程级工具集：拿得到组装根，就一定能按名查到工具策略（同 database.engine 导入 registry）。
register_platform_tools(platform_tool_set())
