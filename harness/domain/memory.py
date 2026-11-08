"""记忆的领域值：回合身份 TurnRef、召回结果 MemoryRecall 与回合写入回执 MemoryWriteTicket。

主循环只认识「召回」和「回合结束写入」两个动作（应用层端口 MemoryProvider），记忆怎么存、
怎么检索由实现决定；这里的类型只携带领域值，不含 ORM 或任何存储细节。
"""

from dataclasses import dataclass
from uuid import UUID


@dataclass(frozen=True)
class TurnRef:
    """召回与写入定位一个回合所需的身份：租户（工作区、用户）、会话与回合。

    主 Agent 召回与回合结束写入时指本轮；子代理继承时指父回合。
    """

    workspace_id: int
    user_id: int
    session_id: int
    turn_id: UUID


@dataclass(frozen=True)
class MemoryRecall:
    """一次召回的结果，按放进 Context 的位置分成两部分；同一回合重复召回结果相同。

    instruction 拼进系统指令末段（Context.instruction 的最后一块，按回合冻结、回合内不变），
    内置实现是记忆目录（rendered_catalog）。message_prefix 拼在本轮起始的用户消息前面
    （Context.messages），只进请求副本、不改写已落库的消息，内置实现是选中的记忆正文
    （rendered_memories）。None 或空串都表示这部分没有内容，实现原样返回，调用方不做归一。
    """

    instruction: str | None = None
    message_prefix: str | None = None


@dataclass(frozen=True)
class MemoryWriteTicket:
    """回合结束写入的回执：调用方提交事务之后，原样交回同一个 Provider 的 after_commit。

    turn 是登记写入时的回合；ref 是实现自定的写入标识（内置实现为提取任务 ID），调用方不解读。
    """

    turn: TurnRef
    ref: str
