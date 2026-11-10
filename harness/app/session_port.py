"""应用层端口：会话存储（SessionStore）。

运行时经它存取会话：加载本轮上下文、追加消息、写压缩快照，会话与轮次的状态读写和加锁，
执行租约、offset、收尾与回收。端口按运行时实际用到的操作设计，不做整存整取；按职责拆成几个协议，
每个运行时协作者只依赖自己用到的那一部分，SessionStore 把它们合在一起。

数据库实现就是 session 域的 SessionService：端口方法与它的公开方法同名同参，靠结构化类型直接满足，
不另写转发适配器；测试用的内存实现在 tests/fixtures/in_memory_session_store.py。

事务约定：存储只写不提交，提交时点由调用方决定（请求阶段的准入、执行体的挂起点与回合终态、
执行包装层的独立短会话）。存储交出的会话、轮次与消息只承诺运行时读取的字段（StoredSession 等视图）；
传回存储的实体必须是同一个存储交出的实例，实现可以直接改写它们。
"""

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any, Protocol
from uuid import UUID

from harness.app.ports import SessionModelOptions


@dataclass(frozen=True)
class TurnExecutionState:
    """轮次的业务状态与执行状态;executing 表示租约有效、有执行中的任务。"""

    turn_id: UUID
    session_id: int
    status: str
    executing: bool
    last_event_offset: int


@dataclass(frozen=True)
class TurnTermination:
    """清除租约后的收尾结果;offset 是为 run_end 分配的 offset。"""

    turn_id: UUID
    session_id: int
    status: str
    session_status: str
    offset: int


@dataclass(frozen=True)
class MessageDraft:
    """取消收尾时补写的一条中立形状消息(assistant 中立块数组或 tool 结果 dict)。"""

    role: str
    content: Any


# 取消方在锁内按需读取未落库内容:只有轮次仍是 running 时才调用。
type PartialLoader = Callable[[], Awaitable[list[MessageDraft]]]


class StoredSession(SessionModelOptions, Protocol):
    """存储交出的会话：运行时读取的字段，模型选择相关的字段继承自 SessionModelOptions。"""

    @property
    def id(self) -> int: ...

    @property
    def title(self) -> str: ...

    @property
    def status(self) -> str: ...

    @property
    def mode_slug(self) -> str: ...

    @property
    def agent_id(self) -> int | None: ...

    @property
    def model_name(self) -> str | None: ...

    @property
    def system_prompt(self) -> str | None: ...


class StoredTurn(Protocol):
    """存储交出的轮次；started_message_id 是本轮起始用户消息，加载上下文时据此切分历史与本轮。"""

    @property
    def id(self) -> UUID: ...

    @property
    def session_id(self) -> int: ...

    @property
    def user_id(self) -> int: ...

    @property
    def workspace_id(self) -> int: ...

    @property
    def status(self) -> str: ...

    @property
    def started_message_id(self) -> int: ...


class StoredMessage(Protocol):
    """存储交出的消息：运行时只读取 ID（工具调用关联的助手消息、回合完成消息）。"""

    @property
    def id(self) -> int: ...


class MessageWriter(Protocol):
    """追加会话消息。"""

    async def add_message(
        self,
        session_id: int,
        role: str,
        content: Any,
        turn_id: UUID | None = None,
        provider: str | None = None,
        model: str | None = None,
    ) -> StoredMessage:
        """追加一条中立形状的消息；provider、model 只有模型产出的 assistant 消息才传，用于成本归因。"""
        ...


class CompactSnapshotWriter(Protocol):
    """写压缩快照。"""

    async def save_compact_snapshot(
        self,
        session_id: int,
        messages: list[dict[str, Any]],
        summary: str,
        through_message_id: int,
    ) -> object:
        """写入一份快照：此后加载上下文时用 messages 替换 through_message_id 及之前的消息。"""
        ...


class TurnContextStore(MessageWriter, CompactSnapshotWriter, Protocol):
    """本轮上下文：加载、追加消息、写压缩快照。"""

    async def load_context_for_turn(
        self,
        turn: StoredTurn,
        additional_context: str | None = None,
        rendered_memories: str | None = None,
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]], int | None]:
        """返回 (可压缩的历史, 本轮消息, 历史水位)。

        历史是最新快照加上水位之后、本轮之前的消息；记忆正文拼在本轮起始用户消息之前，附加上下文
        拼在其后，两者只进返回值、不落库。水位是历史覆盖到的最后一条消息 ID，没有历史时为 None。
        """
        ...


class SessionOptionsReader(Protocol):
    """读取会话冻结的模型选项；子代理据此解析自己的模型。"""

    async def runtime_model_options(self, session_id: int) -> StoredSession:
        """会话不存在时抛「会话不存在」。"""
        ...


class TurnStateStore(SessionOptionsReader, Protocol):
    """会话与轮次的状态：读取、加行锁、发消息准入与状态流转。"""

    async def get(self, session_id: int) -> StoredSession | None: ...

    async def get_required(self, session_id: int) -> StoredSession:
        """会话不存在时抛「会话不存在」。"""
        ...

    async def lock_required(self, session_id: int) -> StoredSession:
        """加行锁读取会话；跨事务的状态机统一先锁会话、再锁轮次。"""
        ...

    async def ensure_interaction_resumable(self, session_id: int) -> StoredSession:
        """加锁并确认会话正在等待人工交互。"""
        ...

    async def prepare_for_message(
        self,
        session_id: int | None,
        content: str,
        user_id: int,
        workspace_id: int,
        agent_id: int | None = None,
        workflow_id: int | None = None,
        model_options: dict[str, Any] | None = None,
    ) -> StoredSession:
        """发消息的准入：取或建会话，校验可执行（冲突为 409），标记运行中并写入模型选项。"""
        ...

    async def set_status(self, session: StoredSession, status: str) -> StoredSession: ...

    async def start_turn(
        self,
        session: StoredSession,
        user_id: int,
        workspace_id: int,
        content: str,
    ) -> tuple[StoredTurn, StoredMessage]:
        """原样保存用户输入并创建轮次，返回轮次与起始用户消息。"""
        ...

    async def get_turn(self, turn_id: UUID) -> StoredTurn | None: ...

    async def get_turn_required(self, turn_id: UUID) -> StoredTurn:
        """轮次不存在时抛「交互轮次不存在」。"""
        ...

    async def mark_turn_status(
        self,
        turn: StoredTurn,
        status: str,
        completed_message_id: int | None = None,
    ) -> StoredTurn:
        """改轮次状态；改为 completed 时必须给出最终助手消息。"""
        ...

    # 以下三个租约操作发生在运行时自己的事务里，与状态写入同一事务、按同一加锁顺序，所以归这里；
    # 执行包装层在独立短会话里的租约操作见 TurnLeaseStore。

    async def claim_turn_lease(self, turn_id: UUID, owner: str, lease_seconds: float) -> int:
        """准入时认领执行租约，返回认领时的 last_event_offset；已有执行中的任务时抛 409。"""
        ...

    async def lock_owned_turn(
        self,
        session_id: int,
        turn_id: UUID,
        owner: str,
    ) -> tuple[StoredSession, StoredTurn]:
        """提交终态前按「会话 → 轮次」加锁并确认租约仍属于 owner，否则抛 LeaseLostError。"""
        ...

    async def release_turn_for_detached_end(
        self,
        session: StoredSession,
        turn: StoredTurn,
    ) -> TurnTermination:
        """执行之外写了轮次终态（如交互取消）后清除残留租约，并为补写的 run_end 分配 offset。"""
        ...


class TurnLeaseStore(Protocol):
    """执行包装层的租约与 offset：心跳续约、offset 预留与校验、持有者收尾、用户取消、孤儿回收。

    这些操作各在一个独立的短数据库会话里执行，store 的生命周期跟随那个会话，由调用方决定是否提交；
    租约在 run_end 写入事件流之后才清除，保证下一次执行的事件排在本次 run_end 之后。
    """

    async def renew_turn_lease(self, turn_id: UUID, owner: str, lease_seconds: float) -> bool:
        """心跳续约；返回 False 表示租约已不属于 owner。"""
        ...

    async def reserve_turn_offsets(self, turn_id: UUID, owner: str, count: int) -> int:
        """在租约内预留 count 个连续 offset，返回首个；租约丢失时抛 LeaseLostError。"""
        ...

    async def verify_turn_lease(self, turn_id: UUID, owner: str) -> None:
        """只读校验租约归属，不属于 owner 时抛 LeaseLostError。"""
        ...

    async def finish_turn_execution(
        self,
        session_id: int,
        turn_id: UUID,
        owner: str,
        *,
        partial: list[MessageDraft] | None = None,
        interrupted_session_status: str = "failed",
    ) -> TurnTermination:
        """持有者收尾：为 run_end 分配 offset；轮次仍是 running 时先补写 partial 再按中断收尾。"""
        ...

    async def release_turn_lease(self, turn_id: UUID, owner: str) -> None:
        """清除 owner 自己的租约；已被他人清除时为空操作。"""
        ...

    async def cancel_turn_execution(
        self,
        session_id: int,
        turn_id: UUID,
        *,
        partial_loader: PartialLoader | None = None,
    ) -> TurnTermination:
        """用户取消执行中的轮次：在锁内补写 partial_loader 给出的半截回答，清除租约并分配 offset。"""
        ...

    async def list_reapable_turn_ids(self, *, include_unleased_running: bool) -> list[UUID]:
        """列出待回收的孤儿轮次；只做初筛，reap_turn 加锁后再复核。"""
        ...

    async def reap_turn(
        self,
        turn_id: UUID,
        *,
        include_unleased_running: bool,
    ) -> TurnTermination | None:
        """回收一个孤儿轮次；加锁复核时已被续约或认领则返回 None。"""
        ...

    async def get_turn_execution(self, turn_id: UUID) -> TurnExecutionState | None: ...

    async def get_session_turn_execution(
        self,
        session_id: int,
        turn_id: UUID,
    ) -> TurnExecutionState:
        """轮次不存在或不属于该会话时抛 404「交互轮次不存在」。"""
        ...


class SessionStore(TurnContextStore, TurnStateStore, Protocol):
    """运行时持有的会话存储：本轮上下文加上会话与轮次状态，再按协作者拆成窄端口分发出去。

    不含 TurnLeaseStore：执行包装层按短会话另行取用，它的生命周期不跟随运行时的数据库会话。
    """
