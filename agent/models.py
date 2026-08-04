from __future__ import annotations

from typing import TYPE_CHECKING

from sqlalchemy import Boolean, ForeignKey, Index, Integer, String, Text, UniqueConstraint, text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from database.base import Base

if TYPE_CHECKING:
    from skill.models import SkillRecord
    from tools.models import ToolRecord
    from user.models import UserRecord
    from workspace.models import WorkspaceRecord


class AgentRecord(Base):
    """工作区内可配置的 Agent 定义。"""

    __tablename__ = "agents"
    __table_args__ = (
        Index(
            "uq_agents_active_workspace_name",
            "workspace_id",
            "name",
            unique=True,
            postgresql_where=text("is_deleted = false"),
        ),
        Index("ix_agents_workspace_enabled", "workspace_id", "is_enabled"),
        {"comment": "自定义Agent定义表"},
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, comment="Agent ID")
    workspace_id: Mapped[int] = mapped_column(
        ForeignKey("workspaces.id", ondelete="CASCADE"),
        index=True,
        comment="所属工作区ID",
    )
    name: Mapped[str] = mapped_column(String(128), comment="Agent名称")
    description: Mapped[str | None] = mapped_column(
        Text,
        nullable=True,
        comment="Agent描述",
    )
    system_prompt: Mapped[str] = mapped_column(Text, comment="系统提示词")
    model_name: Mapped[str | None] = mapped_column(
        String(128),
        nullable=True,
        comment="模型名称，空表示使用系统默认模型",
    )
    is_enabled: Mapped[bool] = mapped_column(
        Boolean,
        default=True,
        index=True,
        comment="是否启用",
    )
    created_by_user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="RESTRICT"),
        index=True,
        comment="创建者用户ID",
    )

    workspace: Mapped[WorkspaceRecord] = relationship()
    created_by_user: Mapped[UserRecord] = relationship(foreign_keys=[created_by_user_id])
    tool_bindings: Mapped[list[AgentToolRecord]] = relationship(
        back_populates="agent",
        cascade="all, delete-orphan",
    )
    skill_bindings: Mapped[list[AgentSkillRecord]] = relationship(
        back_populates="agent",
        cascade="all, delete-orphan",
    )


class AgentToolRecord(Base):
    """Agent 与工具目录条目的绑定。"""

    __tablename__ = "agent_tools"
    __table_args__ = (
        UniqueConstraint("agent_id", "tool_id", name="uq_agent_tools_agent_tool"),
        {"comment": "Agent工具绑定表"},
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, comment="绑定ID")
    agent_id: Mapped[int] = mapped_column(
        ForeignKey("agents.id", ondelete="CASCADE"),
        index=True,
        comment="Agent ID",
    )
    tool_id: Mapped[int] = mapped_column(
        ForeignKey("tools.id", ondelete="RESTRICT"),
        index=True,
        comment="工具定义ID",
    )

    agent: Mapped[AgentRecord] = relationship(back_populates="tool_bindings")
    tool: Mapped[ToolRecord] = relationship()


class AgentSkillRecord(Base):
    """Agent 与 Skill 的绑定。"""

    __tablename__ = "agent_skills"
    __table_args__ = (
        UniqueConstraint("agent_id", "skill_id", name="uq_agent_skills_agent_skill"),
        {"comment": "Agent Skill绑定表"},
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, comment="绑定ID")
    agent_id: Mapped[int] = mapped_column(
        ForeignKey("agents.id", ondelete="CASCADE"),
        index=True,
        comment="Agent ID",
    )
    skill_id: Mapped[int] = mapped_column(
        ForeignKey("skills.id", ondelete="RESTRICT"),
        index=True,
        comment="Skill ID",
    )

    agent: Mapped[AgentRecord] = relationship(back_populates="skill_bindings")
    skill: Mapped[SkillRecord] = relationship()
