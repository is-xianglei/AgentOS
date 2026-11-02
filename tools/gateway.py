"""统一工具网关。

``ToolRegistry`` 只表示某个运行时可见的工具集合，``ToolService`` 负责持久化、
Hook 和实际执行。本模块把 profile、可信身份和这两个对象绑定成一个不可绕过的
运行时入口，避免主 Agent、SubAgent 和 Workflow 节点各自拼接执行参数。
"""

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from core.event_bus import StreamBus
from core.events import Actor
from tools.models import ToolCallRecord
from tools.context import ToolExecutionContext
from tools.policy import ToolCapabilityResolver, ToolProfile
from tools.registry import ToolRegistry, build_tool_registry
from tools.service import ToolService
from tools.shell_policy import ShellAccess


@dataclass
class ToolGateway:
    """一个执行身份对应的工具网关。

    ``registry`` 是已经按 profile 和父级白名单裁剪后的集合；``run`` 仍然把
    profile 再传给 ``ToolService``，因此即使调用方持有旧的工具名，也会在执行层
    再次经过策略检查。
    """

    db: AsyncSession
    registry: ToolRegistry
    profile: ToolProfile
    principal_name: str
    agent_id: int | None = None
    allowed_skill_names: frozenset[str] | None = None
    shell_access: ShellAccess = ShellAccess.FULL
    shell_tmp_root: Path | None = None
    context: ToolExecutionContext = field(init=False)
    service: ToolService = field(init=False)

    @staticmethod
    def _ordered_registry_names(registry: ToolRegistry) -> tuple[str, ...] | None:
        """读取注册表工具名并尽量保留注册顺序。

        ``ToolRegistry.names`` 是 ``frozenset``，适合做集合判断却不适合直接
        生成发给模型的工具 schema：集合迭代顺序可能变化，进而破坏请求缓存、
        测试快照和不同运行时之间的可重复性。正式注册表优先使用 ``tools``
        的插入顺序；旧扩展点只有 ``names`` 时再按名称排序，保证安全降级也
        具有确定顺序。
        """
        tools = getattr(registry, "tools", None)
        if tools is not None:
            return tuple(tool.name for tool in tools)
        names = getattr(registry, "names", None)
        if names is None:
            return None
        return tuple(sorted(names))

    def __post_init__(self) -> None:
        self.context = ToolExecutionContext(
            profile=self.profile,
            principal_name=self.principal_name,
            agent_id=self.agent_id,
            allowed_skill_names=self.allowed_skill_names,
            shell_access=self.shell_access,
            shell_tmp_root=self.shell_tmp_root,
        )
        # 即使调用方绕过 ``create`` 直接构造网关，也不能把完整注册表暴露给
        # 受限 profile。显式白名单的严格拒绝仍由 ``create`` 负责；这里负责
        # 保证模型可见集合与最终执行策略至少保持一致。
        registry_names = self._ordered_registry_names(self.registry)
        if registry_names is None:
            # 旧扩展点可能传入只实现了部分接口的替身注册表。无法读取工具名时，
            # 安全降级为空注册表，避免把未知对象当成完整能力集合而绕过 profile。
            self.registry = ToolRegistry([])
        else:
            allowed_names = ToolCapabilityResolver().resolve_names(
                registry_names,
                self.profile,
            )
            self.registry = self.registry.only(*allowed_names)
        self.service = ToolService(self.db, self.registry)

    @classmethod
    def create(
        cls,
        db: AsyncSession,
        profile: ToolProfile,
        *,
        principal_name: str,
        requested_names: list[str] | tuple[str, ...] | frozenset[str] | None = None,
        strict: bool = True,
        agent_id: int | None = None,
        allowed_skill_names: frozenset[str] | None = None,
        shell_access: ShellAccess = ShellAccess.FULL,
        shell_tmp_root: Path | None = None,
    ) -> "ToolGateway":
        """创建网关并在构造期裁剪工具集合。

        ``requested_names`` 表示父 Agent 或 Agent 配置给出的硬白名单。显式白名单
        中出现当前 profile 不允许调用的工具时直接报错，避免静默丢失配置导致模型
        以为自己拥有某个工具却在运行时才发现不存在。
        """
        registry = build_tool_registry()
        resolver = ToolCapabilityResolver()
        if requested_names is not None:
            # 显式配置属于“绑定”动作，必须使用 bindable 策略；Skill 工具只能由
            # 运行时根据已绑定 Skill 自动注入，不能把 SkillRun 直接写进用户 Agent 配置。
            names = resolver.resolve_bindable_names(
                requested_names,
                profile,
                strict=strict,
            )
            unknown = tuple(name for name in dict.fromkeys(requested_names) if name not in registry.names)
            if strict and unknown:
                raise ValueError(f"工具不存在或未注册: {', '.join(unknown)}")
        else:
            names = resolver.resolve_names(registry.names, profile)
        return cls(
            db,
            registry.only(*names),
            profile,
            principal_name,
            agent_id=agent_id,
            allowed_skill_names=allowed_skill_names,
            shell_access=shell_access,
            shell_tmp_root=shell_tmp_root,
        )

    @classmethod
    def from_registry(
        cls,
        db: AsyncSession,
        registry: ToolRegistry,
        profile: ToolProfile,
        *,
        principal_name: str,
        strict: bool = True,
        agent_id: int | None = None,
        allowed_skill_names: frozenset[str] | None = None,
        shell_access: ShellAccess = ShellAccess.FULL,
        shell_tmp_root: Path | None = None,
    ) -> "ToolGateway":
        """从已有注册表构造网关，并严格校验其能力边界。

        运行时通常已经根据 Plan Mode、父 Agent 白名单或 Workflow 节点得到一个
        裁剪后的 ``ToolRegistry``，此方法避免重新创建注册表，同时保证正式入口
        不会静默吞掉 profile 不允许的工具。
        """
        registry_names = cls._ordered_registry_names(registry)
        if registry_names is None:
            # 兼容旧的 _resolve_tools 替身；没有可审计的 names 时不暴露任何工具。
            safe_registry = ToolRegistry([])
        else:
            names = ToolCapabilityResolver().resolve_names(
                registry_names,
                profile,
                strict=strict,
            )
            safe_registry = registry.only(*names)
        return cls(
            db,
            safe_registry,
            profile,
            principal_name,
            agent_id=agent_id,
            allowed_skill_names=allowed_skill_names,
            shell_access=shell_access,
            shell_tmp_root=shell_tmp_root,
        )

    @property
    def tool_repo(self):
        """保留交互恢复代码对调用记录仓储的访问，迁移期间不复制状态。"""
        return self.service.tool_repo

    @property
    def catalog_repo(self):
        return self.service.catalog_repo

    def validate_input(self, name: str, data: dict[str, Any]) -> dict[str, Any]:
        return self.registry.validate_input(name, data)

    async def start_call(
        self,
        session_id: int,
        tool_name: str,
        input_args: dict[str, Any],
        *,
        message_id: int | None = None,
        agent_id: int | None = None,
    ) -> ToolCallRecord:
        return await self.service.start_call(
            session_id,
            tool_name,
            input_args,
            message_id=message_id,
            # agent_id 是网关绑定的归属；参数保留仅为兼容旧调用点，不能覆盖。
            agent_id=self.agent_id,
        )

    async def get_interaction_call_required(
        self,
        tool_call_id: int,
        session_id: int,
    ) -> ToolCallRecord:
        return await self.service.get_interaction_call_required(tool_call_id, session_id)

    async def run(
        self,
        session_id: int,
        tool_name: str,
        input_args: dict[str, Any],
        *,
        message_id: int | None = None,
        bus: StreamBus | None = None,
        record: ToolCallRecord | None = None,
        actor: Actor | None = None,
        turn_id: UUID | None = None,
        user_id: int | None = None,
        workspace_id: int | None = None,
        shell_access: ShellAccess | None = None,
        shell_tmp_root: Path | None = None,
        agent_id: int | None = None,
        allowed_skill_names: frozenset[str] | None = None,
    ) -> str:
        """执行工具，profile、身份和资源边界只能来自网关自身。

        参数中的 ``allowed_skill_names``、``shell_access``、``shell_tmp_root``
        和 ``agent_id`` 仅为迁移期兼容保留，不能用来扩大网关创建时的能力。
        否则持有一个受限网关的内部调用方可以在执行时临时换成更宽权限。
        """
        return await self.service.run(
            session_id,
            tool_name,
            input_args,
            message_id=message_id,
            bus=bus,
            record=record,
            actor=actor,
            turn_id=turn_id,
            user_id=user_id,
            workspace_id=workspace_id,
            # 权限敏感字段永远采用网关快照；调用方只能通过构造新的网关
            # 获得不同边界，不能在单次调用中提权。
            profile=self.context.profile,
            principal_name=self.context.principal_name,
            agent_id=self.context.agent_id,
            allowed_skill_names=self.context.allowed_skill_names,
            shell_access=self.context.shell_access,
            shell_tmp_root=self.context.shell_tmp_root,
            execution_context=self.context,
        )
