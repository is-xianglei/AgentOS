"""流式事件的类型化定义。

统一 AgentRuntime 与 SubAgentRunner 两处的事件构造,消除前后端靠字符串 key
维持的隐式契约。事件最终经 StreamBus 注入 seq 后序列化为 SSE 帧;为保持前端
app.js 兼容,序列化出的字段名与原裸 dict 完全一致。

两类信封:
1. 运行时事件(RuntimeEvent):{type, event, data, seq} —— 由后端构造的语义事件。
2. 透传的 LLM 原始 chunk:原始 chunk 之上附加 {session_id, actor, seq} 旁路字段。
   原始 chunk 本身是第三方动态结构,不强行对象化,仅用 add_runtime_metadata 包裹。
"""

from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class Actor:
    """事件来源(主代理或某个子代理)。"""

    type: str  # "agent"
    name: str  # "lead" 或成员名

    def to_dict(self) -> dict[str, Any]:
        return {"type": self.type, "name": self.name}


# 主代理来源常量,避免到处手写 {"type":"agent","name":"lead"}
LEAD_ACTOR = Actor(type="agent", name="lead")


def agent_actor(name: str) -> Actor:
    """构造某个子代理来源。"""
    return Actor(type="agent", name=name)


@dataclass
class RuntimeEvent:
    """后端构造的语义事件(非 LLM 原始 chunk)。

    序列化为 {type, event, data},type 与 event 同值(兼容前端
    normalizedEventType 读 event||type)。seq 由 StreamBus 注入,不在此处。
    """

    type: str
    data: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {"type": self.type, "event": self.type, "data": self.data}

