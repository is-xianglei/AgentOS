"""会话存储端口的内存实现：不连数据库，供测试替换 SessionService。

实现运行时主路径用到的 SessionStore 方法：加载本轮上下文、追加消息、写压缩快照，会话与轮次的
读写与状态流转，以及执行内认领、隔离租约的最小语义。租约的 offset、心跳与回收（TurnLeaseStore）
属执行包装层，不在这里实现。语义对齐数据库实现：上下文是「最新快照 + 水位之后的消息」，
记忆正文与附加上下文只拼进返回值，返回的消息内容都是副本，改写它们不影响已存的消息。
传回的会话与轮次按 ID 换成本存储自己的记录再改写，所以入参只需满足端口视图。
"""

from copy import deepcopy
from dataclasses import dataclass, field
from typing import Any
from uuid import UUID, uuid4

from core.errors import AgentException, LeaseLostError
from harness.app.session_port import StoredSession, StoredTurn, TurnTermination


@dataclass
class MemorySession:
    """内存会话，字段覆盖 StoredSession 视图。"""

    id: int
    workspace_id: int | None
    user_id: int | None
    title: str = "新会话"
    status: str = "idle"
    mode_slug: str = "standard"
    thinking_effort: str = "fast"
    model_provider_id: str | None = None
    model_name: str | None = None
    system_prompt: str | None = None
    agent_id: int | None = None
    extra: dict[str, Any] = field(default_factory=dict)


@dataclass
class MemoryTurn:
    """内存轮次，字段覆盖 StoredTurn 视图；lease_owner 只供执行内隔离使用。"""

    id: UUID
    session_id: int
    user_id: int
    workspace_id: int
    started_message_id: int
    status: str = "running"
    completed_message_id: int | None = None
    lease_owner: str | None = None


@dataclass
class MemoryMessage:
    """内存消息；provider、model 与数据库实现一样只记成本归因。"""

    id: int
    session_id: int
    role: str
    content: Any
    turn_id: UUID | None = None
    provider: str | None = None
    model: str | None = None


@dataclass(frozen=True)
class MemorySnapshot:
    """内存压缩快照：加载上下文时以 messages 替换 through_message_id 及之前的消息。"""

    session_id: int
    messages: list[dict[str, Any]]
    summary: str
    through_message_id: int


class InMemorySessionStore:
    """SessionStore 的内存实现；sessions、turns、messages、snapshots 公开，供测试布置与断言。"""

    def __init__(self) -> None:
        self.sessions: dict[int, MemorySession] = {}
        self.turns: dict[UUID, MemoryTurn] = {}
        self.messages: list[MemoryMessage] = []
        self.snapshots: list[MemorySnapshot] = []

    # ---- 布置 ----

    def create_session(self, session_id: int, *, workspace_id: int, user_id: int) -> MemorySession:
        session = MemorySession(id=session_id, workspace_id=workspace_id, user_id=user_id)
        self.sessions[session_id] = session
        return session

    def session_messages(self, session_id: int) -> list[tuple[str, Any]]:
        """按写入顺序返回会话的 (role, content)，供断言消息序列。"""
        return [(m.role, m.content) for m in self.messages if m.session_id == session_id]

    # ---- TurnContextStore ----

    async def add_message(
        self,
        session_id: int,
        role: str,
        content: Any,
        turn_id: UUID | None = None,
        provider: str | None = None,
        model: str | None = None,
    ) -> MemoryMessage:
        message = MemoryMessage(
            id=len(self.messages) + 1,
            session_id=session_id,
            role=role,
            content=deepcopy(content),
            turn_id=turn_id,
            provider=provider,
            model=model,
        )
        self.messages.append(message)
        return message

    async def save_compact_snapshot(
        self,
        session_id: int,
        messages: list[dict[str, Any]],
        summary: str,
        through_message_id: int,
    ) -> MemorySnapshot:
        snapshot = MemorySnapshot(session_id, deepcopy(messages), summary, through_message_id)
        self.snapshots.append(snapshot)
        return snapshot

    async def load_context_for_turn(
        self,
        turn: StoredTurn,
        additional_context: str | None = None,
        rendered_memories: str | None = None,
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]], int | None]:
        snapshot = self._latest_snapshot(turn.session_id)
        history: list[dict[str, Any]] = []
        snapshot_through_id: int | None = None
        if snapshot is not None:
            history = deepcopy(snapshot.messages)
            snapshot_through_id = snapshot.through_message_id
        history_messages = [
            message
            for message in self.messages
            if message.session_id == turn.session_id
            and (snapshot_through_id is None or message.id > snapshot_through_id)
            and message.id < turn.started_message_id
        ]
        history.extend(_rows(history_messages))
        current: list[dict[str, Any]] = []
        for message in self.messages:
            if message.session_id != turn.session_id or message.turn_id != turn.id:
                continue
            content = deepcopy(message.content)
            if message.id == turn.started_message_id and isinstance(content, str):
                if rendered_memories:
                    content = f"{rendered_memories}\n\n{content}"
                if additional_context:
                    content = f"{content}\n\n{additional_context}"
            current.append({"role": message.role, "content": content})
        if history_messages:
            through_message_id = history_messages[-1].id
        else:
            through_message_id = snapshot_through_id
        return history, current, through_message_id

    # ---- TurnStateStore ----

    async def get(self, session_id: int) -> MemorySession | None:
        return self.sessions.get(session_id)

    async def get_required(self, session_id: int) -> MemorySession:
        session = self.sessions.get(session_id)
        if session is None:
            raise AgentException.message("会话不存在")
        return session

    async def lock_required(self, session_id: int) -> MemorySession:
        # 单任务内存实现没有并发，加锁即读取。
        return await self.get_required(session_id)

    async def runtime_model_options(self, session_id: int) -> MemorySession:
        return await self.get_required(session_id)

    async def ensure_interaction_resumable(self, session_id: int) -> MemorySession:
        session = await self.lock_required(session_id)
        if session.status != "awaiting_interaction":
            raise AgentException.message("会话当前没有待处理的人工交互")
        return session

    async def prepare_for_message(
        self,
        session_id: int | None,
        content: str,
        user_id: int,
        workspace_id: int,
        agent_id: int | None = None,
        workflow_id: int | None = None,
        model_options: dict[str, Any] | None = None,
    ) -> MemorySession:
        """只支持已有会话的最小准入：校验可执行后标记运行中，不建会话、不校验 Agent 与模型选项。"""
        if session_id is None:
            raise AgentException.message("内存会话存储不支持按首条消息建会话")
        session = await self.lock_required(session_id)
        if session.status not in {"idle", "created", "failed"}:
            raise AgentException.message(
                "会话当前不可执行", {"status": session.status}, status_code=409
            )
        session.status = "running"
        return session

    async def set_status(self, session: StoredSession, status: str) -> MemorySession:
        record = self.sessions[session.id]
        record.status = status
        return record

    async def start_turn(
        self,
        session: StoredSession,
        user_id: int,
        workspace_id: int,
        content: str,
    ) -> tuple[MemoryTurn, MemoryMessage]:
        if session.user_id != user_id or session.workspace_id != workspace_id:
            raise AgentException.message("会话不属于当前用户或工作区")
        turn_id = uuid4()
        message = await self.add_message(session.id, "user", content, turn_id=turn_id)
        turn = MemoryTurn(
            id=turn_id,
            session_id=session.id,
            user_id=user_id,
            workspace_id=workspace_id,
            started_message_id=message.id,
        )
        self.turns[turn_id] = turn
        return turn, message

    async def get_turn(self, turn_id: UUID) -> MemoryTurn | None:
        return self.turns.get(turn_id)

    async def get_turn_required(self, turn_id: UUID) -> MemoryTurn:
        turn = self.turns.get(turn_id)
        if turn is None:
            raise AgentException.message("交互轮次不存在")
        return turn

    async def mark_turn_status(
        self,
        turn: StoredTurn,
        status: str,
        completed_message_id: int | None = None,
    ) -> MemoryTurn:
        if status == "completed" and completed_message_id is None:
            raise AgentException.message("完成交互轮次时缺少最终助手消息")
        record = self.turns[turn.id]
        record.status = status
        if status == "completed":
            record.completed_message_id = completed_message_id
        else:
            record.completed_message_id = None
        return record

    async def claim_turn_lease(self, turn_id: UUID, owner: str, lease_seconds: float) -> int:
        """最小租约：没有过期与 offset，只记持有者；已有持有者时按 409 拒绝。"""
        turn = await self.get_turn_required(turn_id)
        if turn.lease_owner is not None:
            raise AgentException.message(
                "该轮次已有执行中的任务", {"turn_id": str(turn_id)}, status_code=409
            )
        turn.lease_owner = owner
        return 0

    async def lock_owned_turn(
        self,
        session_id: int,
        turn_id: UUID,
        owner: str,
    ) -> tuple[MemorySession, MemoryTurn]:
        session = await self.lock_required(session_id)
        turn = self.turns.get(turn_id)
        if turn is None or turn.lease_owner != owner:
            raise LeaseLostError("执行租约已失效", {"turn_id": str(turn_id)})
        return session, turn

    async def release_turn_for_detached_end(
        self,
        session: StoredSession,
        turn: StoredTurn,
    ) -> TurnTermination:
        self.turns[turn.id].lease_owner = None
        return TurnTermination(turn.id, session.id, turn.status, session.status, 0)

    def _latest_snapshot(self, session_id: int) -> MemorySnapshot | None:
        for snapshot in reversed(self.snapshots):
            if snapshot.session_id == session_id:
                return snapshot
        return None


def _rows(messages: list[MemoryMessage]) -> list[dict[str, Any]]:
    """持久化消息转中立上下文；与数据库实现一样只透出三种角色，内容取副本。"""
    return [
        {"role": message.role, "content": deepcopy(message.content)}
        for message in messages
        if message.role in {"user", "assistant", "tool"}
    ]
