from __future__ import annotations

from enum import Enum
from typing import TYPE_CHECKING

from sqlalchemy import Boolean, ForeignKey, Index, Integer, String, Text, UniqueConstraint, text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from database.base import Base

if TYPE_CHECKING:
    from agent.models import AgentRecord
    from user.models import UserRecord
    from workspace.models import WorkspaceRecord


class WorkflowMode(str, Enum):
    """内置编排模式；差异全部落在运行时编排器，关联表不区分成员角色。"""

    SEQUENTIAL = "sequential"
    PARALLEL = "parallel"
    LLM_ROUTING = "llm_routing"


class WorkflowRecord(Base):
    """工作区内可复用的 Workflow 定义。"""

    __tablename__ = "workflows"
    __table_args__ = (
        Index(
            "uq_workflows_active_workspace_name",
            "workspace_id",
            "name",
            unique=True,
            postgresql_where=text("is_deleted = false"),
        ),
        Index("ix_workflows_workspace_enabled", "workspace_id", "is_enabled"),
        {"comment": "Workflow编排定义表"},
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, comment="Workflow ID")
    workspace_id: Mapped[int] = mapped_column(
        ForeignKey("workspaces.id", ondelete="CASCADE"),
        index=True,
        comment="所属工作区ID",
    )
    name: Mapped[str] = mapped_column(String(128), comment="Workflow名称")
    description: Mapped[str | None] = mapped_column(
        Text,
        nullable=True,
        comment="Workflow用途说明",
    )
    mode: Mapped[str] = mapped_column(
        String(32),
        comment="编排模式: sequential/parallel/llm_routing",
    )
    is_enabled: Mapped[bool] = mapped_column(
        Boolean,
        default=True,
        index=True,
        comment="是否启用，停用后拒绝运行",
    )
    created_by_user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="RESTRICT"),
        index=True,
        comment="创建者用户ID",
    )

    workspace: Mapped[WorkspaceRecord] = relationship()
    created_by_user: Mapped[UserRecord] = relationship(foreign_keys=[created_by_user_id])
    # 成员按 (sort_order, agent_id) 复合升序，sort_order 相同时次序仍稳定。
    agents: Mapped[list[WorkflowAgentRecord]] = relationship(
        back_populates="workflow",
        cascade="all, delete-orphan",
        order_by=lambda: (WorkflowAgentRecord.sort_order, WorkflowAgentRecord.agent_id),
    )


class WorkflowAgentRecord(Base):
    """Workflow 与成员 Agent 的绑定，仅携带执行/展示序。"""

    __tablename__ = "workflow_agents"
    __table_args__ = (
        UniqueConstraint("workflow_id", "agent_id", name="uq_workflow_agents_workflow_agent"),
        {"comment": "Workflow成员Agent绑定表"},
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, comment="绑定ID")
    workflow_id: Mapped[int] = mapped_column(
        ForeignKey("workflows.id", ondelete="CASCADE"),
        index=True,
        comment="Workflow ID",
    )
    agent_id: Mapped[int] = mapped_column(
        ForeignKey("agents.id", ondelete="RESTRICT"),
        index=True,
        comment="成员Agent ID",
    )
    sort_order: Mapped[int] = mapped_column(
        Integer,
        default=0,
        comment="执行/展示顺序，越小越靠前",
    )

    workflow: Mapped[WorkflowRecord] = relationship(back_populates="agents")
    agent: Mapped[AgentRecord] = relationship()
