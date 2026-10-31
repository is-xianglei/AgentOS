"""Turn 渲染状态的纯函数 reducer:fold(state, event) -> state。

run_snapshot 帧的 state 即 TurnState.to_dict(),是给前端的契约(字段恒定输出):

    turn_id / session_id      本 turn 与所属会话
    status                    null(尚无执行) | running | awaiting_interaction
                              | completed | failed | interrupted,取最近一次执行
    cancelled                 最近一次执行是否被用户取消
    execution_start_offset    最近一次 run_started 的 sequence
    run_end                   最近一次执行的 run_end 事件原文;null 表示执行未结束
    last_offset               已折叠的最大 sequence,续播事件从其后开始
    truncated                 Stream 头部已不可得(过期或裁剪),早期内容需走消息历史接口
    session                   最近一次 session_ready.data
    tasks                     最新 task_snapshot.data.tasks(全量替换语义)
    interaction               待处理的 interaction_request.interaction;已答复、被取消(交互取消
                              接口补写的 run_end(interrupted, cancelled=true))或执行以
                              awaiting_interaction 之外的状态结束后为 null,不保留「已取消」的交互
    workflow                  workflow_progress 节点列表,member 按 agent_id、其余按 phase 去重保序
    errors                    [{actor, error, sequence}],按出现顺序累积
    actors                    [{actor, segment, in_turn, usage, stop_reason, blocks}]
        actor                 最近一次 turn_start 的完整 actor(未见 turn_start 时为精简 actor)
        segment               该 actor 已开始的 turn_start 次数;交互恢复后块 index 从 0 重新编号,
                              故块以 (segment, index) 定位
        in_turn               turn_start 之后、turn_end 之前
        usage / stop_reason   最近一次 turn_end 携带的值
        blocks                [{segment, index, kind, text, closed, tool, result,
                                sequence, result_sequence}]
            kind              text | thinking | tool_use
            text              text / thinking 的累积文本
            tool              tool_use 的 {id, name, input}
            result            对应 tool_result 的 {id, name, output, is_error}
            sequence          建块事件的 sequence;旧 projection 缺失时为 null
            result_sequence   tool_result 事件的 sequence;未见结果时为 null

取消时据 sequence 判断块属于哪次执行、属于哪次 LLM 调用,把尚未落库的部分写回消息历史。

折叠规则:sequence 不大于 last_offset 的事件直接忽略(重复或迟到),
因此「快照 + 其后事件」与「从头折叠全部事件」结果一致。fold 不修改入参。
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, replace
from typing import Any

_DELTA_KINDS = {"text_delta": "text", "thinking_delta": "thinking"}


def actor_key(actor: Mapping[str, Any]) -> tuple[Any, ...]:
    """actor 的稳定定位键:同名并发运行靠 run_id 区分,workflow 成员靠 agent_id。"""
    return (actor.get("role"), actor.get("name"), actor.get("run_id"), actor.get("agent_id"))


@dataclass(frozen=True)
class Block:
    """一个内容块;tool_result 挂在对应 tool_use 块的 result 上。"""

    segment: int
    index: int
    kind: str
    text: str = ""
    closed: bool = False
    tool: dict[str, Any] | None = None
    result: dict[str, Any] | None = None
    sequence: int | None = None
    result_sequence: int | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "segment": self.segment,
            "index": self.index,
            "kind": self.kind,
            "text": self.text,
            "closed": self.closed,
            "tool": self.tool,
            "result": self.result,
            "sequence": self.sequence,
            "result_sequence": self.result_sequence,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> Block:
        return cls(
            segment=data["segment"],
            index=data["index"],
            kind=data["kind"],
            text=data.get("text", ""),
            closed=data.get("closed", False),
            tool=data.get("tool"),
            result=data.get("result"),
            sequence=data.get("sequence"),
            result_sequence=data.get("result_sequence"),
        )


@dataclass(frozen=True)
class ActorState:
    """单个 actor 在本 turn 内的渲染状态。"""

    actor: dict[str, Any]
    segment: int = 0
    in_turn: bool = False
    usage: dict[str, Any] | None = None
    stop_reason: str | None = None
    blocks: tuple[Block, ...] = ()

    @property
    def key(self) -> tuple[Any, ...]:
        return actor_key(self.actor)

    def to_dict(self) -> dict[str, Any]:
        return {
            "actor": self.actor,
            "segment": self.segment,
            "in_turn": self.in_turn,
            "usage": self.usage,
            "stop_reason": self.stop_reason,
            "blocks": [block.to_dict() for block in self.blocks],
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> ActorState:
        return cls(
            actor=dict(data["actor"]),
            segment=data.get("segment", 0),
            in_turn=data.get("in_turn", False),
            usage=data.get("usage"),
            stop_reason=data.get("stop_reason"),
            blocks=tuple(Block.from_dict(item) for item in data.get("blocks", ())),
        )


@dataclass(frozen=True)
class TurnState:
    """客户端渲染一个 turn 所需的全部状态,结构见模块 docstring。"""

    turn_id: str | None = None
    session_id: int | None = None
    status: str | None = None
    cancelled: bool = False
    execution_start_offset: int = 0
    run_end: dict[str, Any] | None = None
    last_offset: int = 0
    truncated: bool = False
    session: dict[str, Any] | None = None
    tasks: list[dict[str, Any]] | None = None
    interaction: dict[str, Any] | None = None
    workflow: tuple[dict[str, Any], ...] = ()
    errors: tuple[dict[str, Any], ...] = ()
    actors: tuple[ActorState, ...] = ()

    @property
    def ended(self) -> bool:
        """最近一次执行是否已写入 run_end。"""
        return self.run_end is not None

    def to_dict(self) -> dict[str, Any]:
        return {
            "turn_id": self.turn_id,
            "session_id": self.session_id,
            "status": self.status,
            "cancelled": self.cancelled,
            "execution_start_offset": self.execution_start_offset,
            "run_end": self.run_end,
            "last_offset": self.last_offset,
            "truncated": self.truncated,
            "session": self.session,
            "tasks": self.tasks,
            "interaction": self.interaction,
            "workflow": list(self.workflow),
            "errors": list(self.errors),
            "actors": [actor.to_dict() for actor in self.actors],
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> TurnState:
        return cls(
            turn_id=data.get("turn_id"),
            session_id=data.get("session_id"),
            status=data.get("status"),
            cancelled=data.get("cancelled", False),
            execution_start_offset=data.get("execution_start_offset", 0),
            run_end=data.get("run_end"),
            last_offset=data.get("last_offset", 0),
            truncated=data.get("truncated", False),
            session=data.get("session"),
            tasks=data.get("tasks"),
            interaction=data.get("interaction"),
            workflow=tuple(dict(item) for item in data.get("workflow", ())),
            errors=tuple(dict(item) for item in data.get("errors", ())),
            actors=tuple(ActorState.from_dict(item) for item in data.get("actors", ())),
        )


def fold(state: TurnState, event: Mapping[str, Any]) -> TurnState:
    """把一个事件折叠进状态,返回新状态;未知类型只推进 last_offset。"""
    sequence = event.get("sequence")
    if isinstance(sequence, int):
        if sequence <= state.last_offset:
            return state
        # 从空状态开始却不是 offset 1,说明 Stream 头部已过期或被裁剪。
        truncated = state.truncated or (state.last_offset == 0 and sequence > 1)
        state = replace(state, last_offset=sequence, truncated=truncated)
    handler = _HANDLERS.get(str(event.get("type")))
    return handler(state, event) if handler is not None else state


def fold_all(state: TurnState, events: Iterable[Mapping[str, Any]]) -> TurnState:
    for event in events:
        state = fold(state, event)
    return state


def restore(
    projection: Mapping[str, Any] | None,
    events: Iterable[Mapping[str, Any]],
) -> TurnState:
    """从 projection(可缺失)与 Stream 现存事件重建状态。"""
    base = TurnState.from_dict(projection) if projection else TurnState()
    return fold_all(base, events)


def snapshot_frame(turn_id: str, state: TurnState) -> dict[str, Any]:
    """run_snapshot 帧;sequence 即快照已覆盖到的 offset。"""
    return {
        "type": "run_snapshot",
        "turn_id": turn_id,
        "state": state.to_dict(),
        "sequence": state.last_offset,
    }


# ---- turn 级事件 ----


def _on_run_started(state: TurnState, event: Mapping[str, Any]) -> TurnState:
    # 新执行开始即说明上一项交互已答复;仍未答复时恢复执行会重新发 interaction_request。
    return replace(
        state,
        turn_id=event.get("turn_id", state.turn_id),
        session_id=event.get("session_id", state.session_id),
        status="running",
        cancelled=False,
        run_end=None,
        interaction=None,
        execution_start_offset=event.get("sequence", state.last_offset),
    )


def _on_run_end(state: TurnState, event: Mapping[str, Any]) -> TurnState:
    status = event.get("status")
    return replace(
        state,
        turn_id=event.get("turn_id", state.turn_id),
        status=status,
        cancelled=bool(event.get("cancelled")),
        run_end=dict(event),
        interaction=state.interaction if status == "awaiting_interaction" else None,
    )


def _on_session_ready(state: TurnState, event: Mapping[str, Any]) -> TurnState:
    data = dict(event.get("data") or {})
    return replace(state, session=data, session_id=data.get("session_id", state.session_id))


def _on_task_snapshot(state: TurnState, event: Mapping[str, Any]) -> TurnState:
    return replace(state, tasks=list((event.get("data") or {}).get("tasks") or []))


def _on_interaction(state: TurnState, event: Mapping[str, Any]) -> TurnState:
    return replace(state, interaction=dict(event.get("interaction") or {}))


def _on_error(state: TurnState, event: Mapping[str, Any]) -> TurnState:
    item = {
        "actor": event.get("actor"),
        "error": event.get("error"),
        "sequence": event.get("sequence"),
    }
    return replace(state, errors=(*state.errors, item))


def _progress_key(progress: Mapping[str, Any]) -> tuple[Any, ...]:
    # routing 的 running 不带 agent_id、done 带被选成员,故非 member 阶段只按 phase 去重。
    if progress.get("phase") == "member":
        return ("member", progress.get("agent_id"))
    return (progress.get("phase"),)


def _on_workflow_progress(state: TurnState, event: Mapping[str, Any]) -> TurnState:
    progress = dict(event.get("progress") or {})
    key = _progress_key(progress)
    nodes = list(state.workflow)
    for position, node in enumerate(nodes):
        if _progress_key(node) == key:
            nodes[position] = {**node, **progress}
            break
    else:
        nodes.append(progress)
    return replace(state, workflow=tuple(nodes))


# ---- actor 级事件 ----


def _update_actor(
    state: TurnState,
    event: Mapping[str, Any],
    update: Callable[[ActorState], ActorState],
) -> TurnState:
    actor = dict(event.get("actor") or {})
    key = actor_key(actor)
    actors = list(state.actors)
    for position, current in enumerate(actors):
        if current.key == key:
            actors[position] = update(current)
            break
    else:
        actors.append(update(ActorState(actor=actor)))
    return replace(state, actors=tuple(actors))


def _find_block(actor: ActorState, index: int) -> int | None:
    for position in range(len(actor.blocks) - 1, -1, -1):
        block = actor.blocks[position]
        if block.segment == actor.segment and block.index == index:
            return position
    return None


def _set_block(actor: ActorState, position: int, block: Block) -> ActorState:
    blocks = list(actor.blocks)
    blocks[position] = block
    return replace(actor, blocks=tuple(blocks))


def _block_index(event: Mapping[str, Any]) -> int:
    return int((event.get("block") or {}).get("index", 0))


def _sequence(event: Mapping[str, Any]) -> int | None:
    sequence = event.get("sequence")
    return sequence if isinstance(sequence, int) else None


def _on_turn_start(state: TurnState, event: Mapping[str, Any]) -> TurnState:
    full_actor = dict(event.get("actor") or {})

    def update(actor: ActorState) -> ActorState:
        return replace(
            actor,
            actor=full_actor,
            segment=actor.segment + 1,
            in_turn=True,
            usage=None,
            stop_reason=None,
        )

    return _update_actor(state, event, update)


def _on_turn_end(state: TurnState, event: Mapping[str, Any]) -> TurnState:
    def update(actor: ActorState) -> ActorState:
        return replace(
            actor,
            in_turn=False,
            usage=event.get("usage"),
            stop_reason=event.get("stop_reason"),
        )

    return _update_actor(state, event, update)


def _on_block_start(state: TurnState, event: Mapping[str, Any]) -> TurnState:
    index = _block_index(event)
    kind = str((event.get("block") or {}).get("block_type") or "text")

    def update(actor: ActorState) -> ActorState:
        block = Block(
            segment=actor.segment,
            index=index,
            kind=kind,
            sequence=_sequence(event),
        )
        return replace(actor, blocks=(*actor.blocks, block))

    return _update_actor(state, event, update)


def _on_delta(state: TurnState, event: Mapping[str, Any]) -> TurnState:
    index = _block_index(event)
    kind = _DELTA_KINDS[str(event.get("type"))]
    text = str(event.get("text") or "")

    def update(actor: ActorState) -> ActorState:
        position = _find_block(actor, index)
        if position is None:
            # 缺 block_start(头部被裁剪)时补建块,不丢正文。
            block = Block(
                segment=actor.segment,
                index=index,
                kind=kind,
                text=text,
                sequence=_sequence(event),
            )
            return replace(actor, blocks=(*actor.blocks, block))
        block = actor.blocks[position]
        return _set_block(actor, position, replace(block, text=block.text + text))

    return _update_actor(state, event, update)


def _on_block_stop(state: TurnState, event: Mapping[str, Any]) -> TurnState:
    index = _block_index(event)

    def update(actor: ActorState) -> ActorState:
        position = _find_block(actor, index)
        if position is None:
            return actor
        return _set_block(actor, position, replace(actor.blocks[position], closed=True))

    return _update_actor(state, event, update)


def _on_tool_use(state: TurnState, event: Mapping[str, Any]) -> TurnState:
    index = _block_index(event)
    tool = dict(event.get("tool") or {})

    def update(actor: ActorState) -> ActorState:
        position = _find_block(actor, index)
        if position is None:
            block = Block(
                segment=actor.segment,
                index=index,
                kind="tool_use",
                tool=tool,
                sequence=_sequence(event),
            )
            return replace(actor, blocks=(*actor.blocks, block))
        block = replace(actor.blocks[position], kind="tool_use", tool=tool)
        return _set_block(actor, position, block)

    return _update_actor(state, event, update)


def _on_tool_result(state: TurnState, event: Mapping[str, Any]) -> TurnState:
    result = dict(event.get("tool") or {})
    sequence = _sequence(event)

    def update(actor: ActorState) -> ActorState:
        # 交互恢复后 tool_result 可能落在后续 segment,故跨 segment 按工具 ID 回溯。
        for position in range(len(actor.blocks) - 1, -1, -1):
            block = actor.blocks[position]
            if block.tool is not None and block.tool.get("id") == result.get("id"):
                block = replace(block, result=result, result_sequence=sequence)
                return _set_block(actor, position, block)
        orphan = Block(
            segment=actor.segment,
            index=-1,
            kind="tool_use",
            closed=True,
            tool={"id": result.get("id"), "name": result.get("name")},
            result=result,
            sequence=sequence,
            result_sequence=sequence,
        )
        return replace(actor, blocks=(*actor.blocks, orphan))

    return _update_actor(state, event, update)


_HANDLERS: dict[str, Callable[[TurnState, Mapping[str, Any]], TurnState]] = {
    "run_started": _on_run_started,
    "run_end": _on_run_end,
    "session_ready": _on_session_ready,
    "task_snapshot": _on_task_snapshot,
    "interaction_request": _on_interaction,
    "error": _on_error,
    "workflow_progress": _on_workflow_progress,
    "turn_start": _on_turn_start,
    "turn_end": _on_turn_end,
    "block_start": _on_block_start,
    "text_delta": _on_delta,
    "thinking_delta": _on_delta,
    "block_stop": _on_block_stop,
    "tool_use": _on_tool_use,
    "tool_result": _on_tool_result,
}
