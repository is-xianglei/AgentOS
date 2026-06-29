"""流式事件的类型化定义(对齐 docs/streaming-protocol.md)。

统一 orchestrator(主循环)、subagent(一次性子代理)、teammate(持久成员)三类
产出者的事件构造。所有事件封装为对象,不裸传 dict,通过 to_dict() 序列化为 SSE 帧;
sequence 由 StreamBus 在入队时注入,不在对象构造期填充。

两类信封:
1. StreamEvent —— 协议定义的 9 类事件(turn_start/turn_end/block_*/thinking_delta/
   text_delta/tool_use/tool_result/error),带 actor。
2. RuntimeEvent —— 会话级控制事件(如 session_ready),不属于某个 actor 的产出,
   仅用于前端建立连接 / 拿 session 元信息。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any


class ActorRole(str, Enum):
    """事件产出者的绝对种类(不随团队层级变化)。"""

    ORCHESTRATOR = "orchestrator"  # 会话主循环
    SUBAGENT = "subagent"  # Agent 工具派的一次性子代理
    TEAMMATE = "teammate"  # TeamSpawn 派的持久成员


class EventType(str, Enum):
    """协议事件类型(9 种,所有 actor 通用)。"""

    TURN_START = "turn_start"
    TURN_END = "turn_end"
    BLOCK_START = "block_start"
    BLOCK_STOP = "block_stop"
    THINKING_DELTA = "thinking_delta"
    TEXT_DELTA = "text_delta"
    TOOL_USE = "tool_use"
    TOOL_RESULT = "tool_result"
    ERROR = "error"


class BlockType(str, Enum):
    """内容块类型。"""

    THINKING = "thinking"
    TEXT = "text"
    TOOL_USE = "tool_use"


@dataclass(frozen=True)
class TeamRef:
    """团队引用(仅 teammate 的 actor 携带)。is_lead 表达团队内相对角色。"""

    id: int
    is_lead: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {"id": self.id, "is_lead": self.is_lead}


@dataclass(frozen=True)
class Actor:
    """事件产出者。

    role 是绝对种类;name 是显示名(orchestrator 固定 'orchestrator',
    subagent 用 agent_type,teammate 用成员名)。run_id 用于区分同名并发运行
    (subagent 的唯一键);task/team 是只在开场事件携带的元信息。
    """

    role: ActorRole
    name: str
    run_id: str | None = None
    task: str | None = None
    team: TeamRef | None = None

    def to_dict_full(self) -> dict[str, Any]:
        """完整形态:供开场事件(turn_start)使用,带 task / team。"""
        data: dict[str, Any] = {"role": self.role.value, "name": self.name}
        if self.run_id is not None:
            data["run_id"] = self.run_id
        if self.task is not None:
            data["task"] = self.task
        if self.team is not None:
            data["team"] = self.team.to_dict()
        return data

    def to_dict_compact(self) -> dict[str, Any]:
        """精简形态:供高频增量事件使用,只带定位键。"""
        data: dict[str, Any] = {"role": self.role.value, "name": self.name}
        if self.run_id is not None:
            data["run_id"] = self.run_id
        return data


# 主循环来源常量,避免到处手写。
ORCHESTRATOR_ACTOR = Actor(role=ActorRole.ORCHESTRATOR, name="orchestrator")


def subagent_actor(agent_type: str, run_id: str, task: str | None = None) -> Actor:
    """构造一次性子代理来源(唯一键是 run_id)。"""
    return Actor(role=ActorRole.SUBAGENT, name=agent_type, run_id=run_id, task=task)


def teammate_actor(
    name: str, run_id: str, task: str | None = None, team: TeamRef | None = None
) -> Actor:
    """构造持久成员来源(唯一键是 name)。"""
    return Actor(role=ActorRole.TEAMMATE, name=name, run_id=run_id, task=task, team=team)


@dataclass(frozen=True)
class BlockInfo:
    """内容块信息(block_start 带 block_type,block_stop 只需 index)。"""

    index: int
    block_type: BlockType | None = None

    def to_dict(self) -> dict[str, Any]:
        data: dict[str, Any] = {"index": self.index}
        if self.block_type is not None:
            data["block_type"] = self.block_type.value
        return data


@dataclass(frozen=True)
class ToolInfo:
    """工具调用 / 结果信息。

    tool_use 用 id/name/input;tool_result 用 id/name/output/is_error。
    用同一个对象承载,序列化时按非 None 字段输出。
    """

    id: str
    name: str
    input: dict[str, Any] | None = None
    output: str | None = None
    is_error: bool | None = None

    def to_dict(self) -> dict[str, Any]:
        data: dict[str, Any] = {"id": self.id, "name": self.name}
        if self.input is not None:
            data["input"] = self.input
        if self.output is not None:
            data["output"] = self.output
        if self.is_error is not None:
            data["is_error"] = self.is_error
        return data


@dataclass(frozen=True)
class Usage:
    """token 用量(turn_end 携带)。

    cache_creation/cache_read 为缓存写入/命中的 input token 数,用于成本核算
    (缓存读取计费远低于新鲜 input)。缺省 0 兼容不返回缓存字段的场景。
    """

    input_tokens: int = 0
    output_tokens: int = 0
    cache_creation_input_tokens: int = 0
    cache_read_input_tokens: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "cache_creation_input_tokens": self.cache_creation_input_tokens,
            "cache_read_input_tokens": self.cache_read_input_tokens,
        }


@dataclass(frozen=True)
class ErrorInfo:
    """归一化错误信息。retriable=可重试,fatal=该条流是否终止。"""

    code: str
    message: str
    retriable: bool = False
    fatal: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "message": self.message,
            "retriable": self.retriable,
            "fatal": self.fatal,
        }


@dataclass
class StreamEvent:
    """协议事件(9 类的统一载体)。

    type 决定携带哪些 payload 子对象;sequence 由 StreamBus 注入。
    用工厂方法构造,不直接填字段,保证各类型字段组合正确。
    """

    type: EventType
    actor: Actor
    session_id: int
    block: BlockInfo | None = None
    tool: ToolInfo | None = None
    usage: Usage | None = None
    error: ErrorInfo | None = None
    text: str | None = None
    stop_reason: str | None = None
    sequence: int | None = None

    # ---- 工厂方法:生命周期 ----

    @classmethod
    def turn_start(cls, actor: Actor, session_id: int) -> "StreamEvent":
        return cls(type=EventType.TURN_START, actor=actor, session_id=session_id)

    @classmethod
    def turn_end(
        cls, actor: Actor, session_id: int, stop_reason: str | None, usage: Usage | None
    ) -> "StreamEvent":
        return cls(
            type=EventType.TURN_END,
            actor=actor,
            session_id=session_id,
            stop_reason=stop_reason,
            usage=usage,
        )

    # ---- 工厂方法:内容块 ----

    @classmethod
    def block_start(
        cls, actor: Actor, session_id: int, index: int, block_type: BlockType
    ) -> "StreamEvent":
        return cls(
            type=EventType.BLOCK_START,
            actor=actor,
            session_id=session_id,
            block=BlockInfo(index=index, block_type=block_type),
        )

    @classmethod
    def block_stop(cls, actor: Actor, session_id: int, index: int) -> "StreamEvent":
        return cls(
            type=EventType.BLOCK_STOP,
            actor=actor,
            session_id=session_id,
            block=BlockInfo(index=index),
        )

    # ---- 工厂方法:内容增量 ----

    @classmethod
    def thinking_delta(
        cls, actor: Actor, session_id: int, index: int, text: str
    ) -> "StreamEvent":
        return cls(
            type=EventType.THINKING_DELTA,
            actor=actor,
            session_id=session_id,
            block=BlockInfo(index=index),
            text=text,
        )

    @classmethod
    def text_delta(
        cls, actor: Actor, session_id: int, index: int, text: str
    ) -> "StreamEvent":
        return cls(
            type=EventType.TEXT_DELTA,
            actor=actor,
            session_id=session_id,
            block=BlockInfo(index=index),
            text=text,
        )

    # ---- 工厂方法:工具 ----

    @classmethod
    def tool_use(
        cls, actor: Actor, session_id: int, index: int, tool: ToolInfo
    ) -> "StreamEvent":
        return cls(
            type=EventType.TOOL_USE,
            actor=actor,
            session_id=session_id,
            block=BlockInfo(index=index),
            tool=tool,
        )

    @classmethod
    def tool_result(cls, actor: Actor, session_id: int, tool: ToolInfo) -> "StreamEvent":
        return cls(
            type=EventType.TOOL_RESULT, actor=actor, session_id=session_id, tool=tool
        )

    # ---- 工厂方法:错误 ----

    @classmethod
    def error_event(cls, actor: Actor, session_id: int, error: ErrorInfo) -> "StreamEvent":
        return cls(type=EventType.ERROR, actor=actor, session_id=session_id, error=error)

    def to_dict(self) -> dict[str, Any]:
        """序列化为 SSE 帧。turn_start 用完整 actor(带 task/team),其余用精简 actor。"""
        actor_dict = (
            self.actor.to_dict_full()
            if self.type is EventType.TURN_START
            else self.actor.to_dict_compact()
        )
        data: dict[str, Any] = {
            "type": self.type.value,
            "session_id": self.session_id,
            "actor": actor_dict,
        }
        if self.sequence is not None:
            data["sequence"] = self.sequence
        if self.block is not None:
            data["block"] = self.block.to_dict()
        if self.tool is not None:
            data["tool"] = self.tool.to_dict()
        if self.usage is not None:
            data["usage"] = self.usage.to_dict()
        if self.error is not None:
            data["error"] = self.error.to_dict()
        if self.text is not None:
            data["text"] = self.text
        if self.stop_reason is not None:
            data["stop_reason"] = self.stop_reason
        return data


@dataclass
class RuntimeEvent:
    """会话级控制事件(不属于某个 actor 的产出)。

    用于前端建立连接 / 拿 session 元信息,典型为 session_ready。
    与 StreamEvent 并行存在;sequence 同样由 StreamBus 注入。
    """

    type: str
    data: dict[str, Any] = field(default_factory=dict)
    sequence: int | None = None

    def to_dict(self) -> dict[str, Any]:
        result: dict[str, Any] = {"type": self.type, "data": self.data}
        if self.sequence is not None:
            result["sequence"] = self.sequence
        return result




