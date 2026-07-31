from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.ext.mutable import MutableDict, MutableList
from sqlalchemy.orm import Mapped, mapped_column, relationship

from database.base import Base


class MemorySpaceRecord(Base):
    __tablename__ = "memory_spaces"
    __table_args__ = (
        CheckConstraint("catalog_version >= 0", name="ck_memory_spaces_catalog_version"),
        CheckConstraint(
            "sessions_since_dream >= 0",
            name="ck_memory_spaces_sessions_since_dream",
        ),
        Index(
            "uq_memory_spaces_active_user_workspace",
            "workspace_id",
            "user_id",
            unique=True,
            postgresql_where=text("is_deleted = false"),
        ),
        {"comment": "用户在工作区内的私有Memory空间"},
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, comment="Memory空间ID")
    workspace_id: Mapped[int] = mapped_column(
        ForeignKey("workspaces.id", ondelete="CASCADE"),
        index=True,
        comment="工作区ID",
    )
    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"),
        index=True,
        comment="私有所有者用户ID",
    )
    catalog_version: Mapped[int] = mapped_column(
        BigInteger,
        default=0,
        comment="有效Memory目录变更计数器",
    )
    last_dream_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
        comment="最近一次Dream完成时间",
    )
    last_scan_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
        comment="最近一次Dream扫描时间",
    )
    sessions_since_dream: Mapped[int] = mapped_column(
        Integer,
        default=0,
        comment="上次Dream后完成的不同会话数",
    )
    settings: Mapped[dict[str, Any]] = mapped_column(
        MutableDict.as_mutable(JSONB()),
        default=dict,
        comment="Memory空间设置和预算",
    )

    items: Mapped[list[MemoryItemRecord]] = relationship(
        back_populates="space",
        cascade="all, delete-orphan",
    )


class MemoryItemRecord(Base):
    __tablename__ = "memory_items"
    __table_args__ = (
        CheckConstraint(
            "memory_type IN ('user', 'feedback', 'project', 'reference')",
            name="ck_memory_items_type",
        ),
        CheckConstraint(
            "status IN ('active', 'superseded', 'archived')",
            name="ck_memory_items_status",
        ),
        CheckConstraint(
            "source_kind IN ('explicit', 'extracted', 'dream', 'manual')",
            name="ck_memory_items_source_kind",
        ),
        CheckConstraint("version >= 1", name="ck_memory_items_version"),
        CheckConstraint("use_count >= 0", name="ck_memory_items_use_count"),
        CheckConstraint(
            "octet_length(body) <= 16384",
            name="ck_memory_items_body_bytes",
        ),
        Index(
            "uq_memory_items_active_memory_key",
            "space_id",
            "memory_key",
            unique=True,
            postgresql_where=text("status = 'active' AND is_deleted = false"),
        ),
        Index(
            "ix_memory_catalog",
            "space_id",
            "status",
            text("updated_at DESC"),
            "id",
        ),
        {"comment": "Memory稳定逻辑记录"},
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, comment="Memory ID")
    space_id: Mapped[int] = mapped_column(
        ForeignKey("memory_spaces.id", ondelete="CASCADE"),
        index=True,
        comment="所属Memory空间ID",
    )
    memory_key: Mapped[str] = mapped_column(
        String(160),
        index=True,
        comment="稳定业务标识",
    )
    memory_type: Mapped[str] = mapped_column(String(16), index=True, comment="Memory类型")
    name: Mapped[str] = mapped_column(String(200), comment="人类可读标题")
    description: Mapped[str] = mapped_column(
        String(500),
        comment="供选择器使用的摘要",
    )
    body: Mapped[str] = mapped_column(Text, comment="Markdown正文")
    status: Mapped[str] = mapped_column(
        String(16),
        default="active",
        index=True,
        comment="状态: active/superseded/archived",
    )
    version: Mapped[int] = mapped_column(Integer, default=1, comment="乐观锁版本")
    superseded_by_id: Mapped[int | None] = mapped_column(
        ForeignKey("memory_items.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
        comment="替代当前旧Memory的新Memory ID",
    )
    source_kind: Mapped[str] = mapped_column(
        String(16),
        default="explicit",
        comment="来源: explicit/extracted/dream/manual",
    )
    last_used_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
        comment="最近一次被Recall使用时间",
    )
    use_count: Mapped[int] = mapped_column(Integer, default=0, comment="Recall使用次数")

    space: Mapped[MemorySpaceRecord] = relationship(back_populates="items")
    superseded_by: Mapped[MemoryItemRecord | None] = relationship(
        remote_side=[id],
        foreign_keys=[superseded_by_id],
        post_update=True,
    )
    revisions: Mapped[list[MemoryRevisionRecord]] = relationship(
        back_populates="memory",
        cascade="all, delete-orphan",
    )
    sources: Mapped[list[MemorySourceRecord]] = relationship(
        back_populates="memory",
        cascade="all, delete-orphan",
    )


class MemoryRevisionRecord(Base):
    __tablename__ = "memory_revisions"
    __table_args__ = (
        CheckConstraint("revision >= 1", name="ck_memory_revisions_revision"),
        CheckConstraint(
            "memory_type IN ('user', 'feedback', 'project', 'reference')",
            name="ck_memory_revisions_type",
        ),
        CheckConstraint(
            "octet_length(body) <= 16384",
            name="ck_memory_revisions_body_bytes",
        ),
        UniqueConstraint(
            "memory_id",
            "revision",
            name="uq_memory_revisions_memory_revision",
        ),
        {"comment": "Memory不可变修订快照"},
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, comment="修订ID")
    memory_id: Mapped[int] = mapped_column(
        ForeignKey("memory_items.id", ondelete="CASCADE"),
        index=True,
        comment="Memory ID",
    )
    revision: Mapped[int] = mapped_column(Integer, comment="修订序号")
    memory_type: Mapped[str] = mapped_column(String(16), comment="该修订的Memory类型")
    name: Mapped[str] = mapped_column(String(200), comment="该修订的标题")
    description: Mapped[str] = mapped_column(String(500), comment="该修订的摘要")
    body: Mapped[str] = mapped_column(Text, comment="该修订的Markdown正文")
    actor_type: Mapped[str] = mapped_column(
        String(16),
        comment="写入者类型: user/extractor/dream/system",
    )
    actor_id: Mapped[str | None] = mapped_column(
        String(120),
        nullable=True,
        comment="写入者标识",
    )
    run_id: Mapped[UUID | None] = mapped_column(
        PGUUID(as_uuid=True),
        nullable=True,
        index=True,
        comment="关联后台运行ID",
    )

    memory: Mapped[MemoryItemRecord] = relationship(back_populates="revisions")
    sources: Mapped[list[MemorySourceRecord]] = relationship(
        back_populates="revision_record",
        cascade="all, delete-orphan",
    )


class MemorySourceRecord(Base):
    __tablename__ = "memory_sources"
    __table_args__ = (
        CheckConstraint(
            "source_kind IN ('explicit', 'extracted', 'dream', 'manual')",
            name="ck_memory_sources_kind",
        ),
        CheckConstraint(
            "source_excerpt IS NULL OR char_length(source_excerpt) <= 1000",
            name="ck_memory_sources_excerpt_length",
        ),
        CheckConstraint(
            "from_message_id IS NULL OR to_message_id IS NULL OR from_message_id <= to_message_id",
            name="ck_memory_sources_message_range",
        ),
        UniqueConstraint(
            "memory_id",
            "turn_id",
            "from_message_id",
            "to_message_id",
            "source_kind",
            name="uq_memory_sources_provenance",
        ),
        {"comment": "Memory来源证据"},
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, comment="来源ID")
    memory_id: Mapped[int] = mapped_column(
        ForeignKey("memory_items.id", ondelete="CASCADE"),
        index=True,
        comment="Memory ID",
    )
    revision_id: Mapped[int] = mapped_column(
        ForeignKey("memory_revisions.id", ondelete="CASCADE"),
        index=True,
        comment="修订ID",
    )
    session_id: Mapped[int | None] = mapped_column(
        ForeignKey("sessions.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
        comment="来源会话ID",
    )
    turn_id: Mapped[UUID | None] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("session_turns.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
        comment="来源交互轮次ID",
    )
    from_message_id: Mapped[int | None] = mapped_column(
        ForeignKey("session_messages.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
        comment="来源消息起点ID",
    )
    to_message_id: Mapped[int | None] = mapped_column(
        ForeignKey("session_messages.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
        comment="来源消息终点ID",
    )
    source_kind: Mapped[str] = mapped_column(String(16), comment="来源类型")
    source_excerpt: Mapped[str | None] = mapped_column(
        Text,
        nullable=True,
        comment="短来源证据",
    )

    memory: Mapped[MemoryItemRecord] = relationship(back_populates="sources")
    revision_record: Mapped[MemoryRevisionRecord] = relationship(back_populates="sources")


class TurnMemoryContextRecord(Base):
    __tablename__ = "turn_memory_contexts"
    __table_args__ = (
        UniqueConstraint(
            "turn_id",
            name="uq_turn_memory_contexts_turn_id",
        ),
        CheckConstraint(
            "catalog_version >= 0",
            name="ck_turn_memory_contexts_catalog_version",
        ),
        CheckConstraint(
            "selector_status IN ('selected', 'empty', 'degraded')",
            name="ck_turn_memory_contexts_selector_status",
        ),
        CheckConstraint("byte_count >= 0", name="ck_turn_memory_contexts_byte_count"),
        {"comment": "交互轮次冻结的Memory请求上下文"},
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, comment="Memory上下文ID")
    turn_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("session_turns.id", ondelete="CASCADE"),
        comment="所属交互轮次ID",
    )
    space_id: Mapped[int | None] = mapped_column(
        ForeignKey("memory_spaces.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
        comment="读取时的Memory空间ID，无空间时为空",
    )
    catalog_version: Mapped[int] = mapped_column(
        BigInteger,
        default=0,
        comment="冻结时的Catalog版本",
    )
    selected_revision_ids: Mapped[list[int]] = mapped_column(
        MutableList.as_mutable(JSONB()),
        default=list,
        comment="按注入顺序保存的Revision ID",
    )
    rendered_catalog: Mapped[str] = mapped_column(
        Text,
        default="",
        comment="发给主模型的精确Catalog文本",
    )
    rendered_memories: Mapped[str] = mapped_column(
        Text,
        default="",
        comment="发给模型的精确相关Memory文本",
    )
    selector_status: Mapped[str] = mapped_column(
        String(16),
        comment="选择状态: selected/empty/degraded",
    )
    degraded_reason: Mapped[str | None] = mapped_column(
        String(200),
        nullable=True,
        comment="降级原因代码",
    )
    byte_count: Mapped[int] = mapped_column(
        Integer,
        default=0,
        comment="Catalog与相关Memory的UTF-8总字节数",
    )


class MemoryJobRecord(Base):
    __tablename__ = "memory_jobs"
    __table_args__ = (
        CheckConstraint(
            "job_type IN ('extract', 'dream')",
            name="ck_memory_jobs_job_type",
        ),
        CheckConstraint(
            "status IN ('pending', 'running', 'retry', 'succeeded', 'dead')",
            name="ck_memory_jobs_status",
        ),
        CheckConstraint("attempts >= 0", name="ck_memory_jobs_attempts"),
        CheckConstraint("max_attempts >= 1", name="ck_memory_jobs_max_attempts"),
        CheckConstraint(
            "attempts <= max_attempts",
            name="ck_memory_jobs_attempt_limit",
        ),
        CheckConstraint(
            "base_catalog_version >= 0",
            name="ck_memory_jobs_base_catalog_version",
        ),
        UniqueConstraint(
            "idempotency_key",
            name="uq_memory_jobs_idempotency_key",
        ),
        Index(
            "ix_memory_jobs_claim",
            "status",
            "available_at",
            "lease_until",
            "id",
            # 与迁移 0015 保持一致：认领 SQL 还有接管 lease 过期 running Job 的
            # OR 分支，部分索引必须覆盖三种状态，否则退化为全表扫描。
            postgresql_where=text(
                "is_deleted = false AND status IN ('pending', 'retry', 'running')"
            ),
        ),
        Index(
            "ix_memory_jobs_space_type_status",
            "space_id",
            "job_type",
            "status",
        ),
        {"comment": "Memory持久后台任务"},
    )

    id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True),
        primary_key=True,
        default=uuid4,
        comment="任务ID",
    )
    job_type: Mapped[str] = mapped_column(
        String(16),
        comment="任务类型: extract/dream",
    )
    space_id: Mapped[int] = mapped_column(
        ForeignKey("memory_spaces.id", ondelete="CASCADE"),
        index=True,
        comment="目标Memory空间ID",
    )
    session_id: Mapped[int | None] = mapped_column(
        ForeignKey("sessions.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
        comment="来源会话ID",
    )
    turn_id: Mapped[UUID | None] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("session_turns.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
        comment="来源交互轮次ID",
    )
    idempotency_key: Mapped[str] = mapped_column(
        String(255),
        comment="全局唯一幂等键",
    )
    status: Mapped[str] = mapped_column(
        String(16),
        default="pending",
        index=True,
        comment="状态: pending/running/retry/succeeded/dead",
    )
    attempts: Mapped[int] = mapped_column(
        Integer,
        default=0,
        comment="累计尝试次数",
    )
    max_attempts: Mapped[int] = mapped_column(
        Integer,
        default=5,
        comment="最大尝试次数",
    )
    available_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        comment="允许认领时间",
    )
    lease_until: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
        comment="租约到期时间",
    )
    worker_id: Mapped[str | None] = mapped_column(
        String(120),
        nullable=True,
        comment="当前租约持有者ID",
    )
    model: Mapped[str | None] = mapped_column(
        String(120),
        nullable=True,
        comment="执行任务使用的模型",
    )
    prompt_version: Mapped[str | None] = mapped_column(
        String(64),
        nullable=True,
        comment="任务提示词版本",
    )
    base_catalog_version: Mapped[int] = mapped_column(
        BigInteger,
        default=0,
        comment="任务基于的Catalog版本",
    )
    started_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
        comment="首次开始执行时间",
    )
    finished_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
        comment="最终完成时间",
    )
    last_error_code: Mapped[str | None] = mapped_column(
        String(64),
        nullable=True,
        comment="最近一次失败代码",
    )
    last_error_message: Mapped[str | None] = mapped_column(
        String(1000),
        nullable=True,
        comment="最近一次失败摘要",
    )
    payload: Mapped[dict[str, Any]] = mapped_column(
        MutableDict.as_mutable(JSONB()),
        default=dict,
        comment="不含正文和原始对话的任务扩展参数",
    )
