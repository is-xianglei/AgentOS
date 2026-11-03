"""编排运行的持久化模型。"""

from typing import Any, ClassVar
from uuid import UUID, uuid4

from sqlalchemy import ForeignKey, Integer, String
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.ext.mutable import MutableDict
from sqlalchemy.orm import Mapped, mapped_column

from database.base import Base, json_type


class OrchestrationCheckpointRecord(Base):
    """一次 Orchestrator run 的最新可恢复快照。"""

    __tablename__ = "orchestration_checkpoints"
    __table_args__: ClassVar[dict[str, str]] = {"comment": "编排运行检查点"}

    run_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True),
        primary_key=True,
        default=uuid4,
        comment="编排运行ID",
    )
    session_id: Mapped[int] = mapped_column(
        ForeignKey("sessions.id", ondelete="CASCADE"),
        index=True,
        comment="所属会话ID",
    )
    turn_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("session_turns.id", ondelete="CASCADE"),
        index=True,
        comment="所属交互轮次ID",
    )
    cursor: Mapped[str | None] = mapped_column(
        String(160),
        nullable=True,
        comment="最近完成的编排游标",
    )
    state: Mapped[dict[str, Any]] = mapped_column(
        MutableDict.as_mutable(json_type()),
        default=dict,
        comment="节点之间传递的编排状态",
    )
    revision: Mapped[int] = mapped_column(
        Integer,
        default=0,
        nullable=False,
        comment="乐观锁版本",
    )
