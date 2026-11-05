from datetime import datetime
from typing import Any

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.ext.mutable import MutableList
from sqlalchemy.orm import Mapped, mapped_column

from database.base import Base, json_type


class MarketListingRecord(Base):
    """市场条目：源 Agent 或 Skill 在市场里的唯一身份，内容放在各个版本里。"""

    __tablename__ = "market_listings"
    __table_args__ = (
        CheckConstraint("kind IN ('agent', 'skill')", name="ck_market_listings_kind"),
        CheckConstraint(
            "status IN ('unlisted', 'listed', 'delisted', 'removed')",
            name="ck_market_listings_status",
        ),
        # 官方预置按 (kind, 目录名) 定位，启动同步时据此判断库里是否已有。
        Index(
            "uq_market_listings_kind_official_key",
            "kind",
            "official_key",
            unique=True,
            postgresql_where=text("official_key IS NOT NULL"),
        ),
        # 同一个源对象只对应一个条目，之后的提交都作为这个条目的新版本。
        Index(
            "uq_market_listings_source_agent_id",
            "source_agent_id",
            unique=True,
            postgresql_where=text("source_agent_id IS NOT NULL"),
        ),
        Index(
            "uq_market_listings_source_skill_id",
            "source_skill_id",
            unique=True,
            postgresql_where=text("source_skill_id IS NOT NULL"),
        ),
        {"comment": "市场条目"},
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, comment="市场条目ID")
    kind: Mapped[str] = mapped_column(String(16), comment="条目类型: agent/skill")
    is_official: Mapped[bool] = mapped_column(Boolean, default=False, comment="是否官方预置")
    official_key: Mapped[str | None] = mapped_column(
        String(128),
        nullable=True,
        comment="官方预置的目录名，社区条目为空",
    )
    publisher_workspace_id: Mapped[int | None] = mapped_column(
        ForeignKey("workspaces.id", ondelete="CASCADE"),
        nullable=True,
        index=True,
        comment="发布条目的工作区ID，官方条目为空",
    )
    source_agent_id: Mapped[int | None] = mapped_column(
        ForeignKey("agents.id", ondelete="SET NULL"),
        nullable=True,
        comment="源Agent ID，仅agent条目有值",
    )
    source_skill_id: Mapped[int | None] = mapped_column(
        ForeignKey("skills.id", ondelete="SET NULL"),
        nullable=True,
        comment="源Skill ID，仅skill条目有值",
    )
    status: Mapped[str] = mapped_column(
        String(16),
        default="unlisted",
        index=True,
        comment="条目状态: unlisted未有通过版本/listed已上架/delisted作者下架/removed平台下架",
    )
    status_reason: Mapped[str | None] = mapped_column(Text, nullable=True, comment="下架原因")
    current_version_number: Mapped[int | None] = mapped_column(
        Integer,
        nullable=True,
        comment="当前版本号，即最新审核通过的版本，没有时为空",
    )
    # name、summary、category 冗余自当前版本，审核通过时复制过来，方便列表查询和搜索。
    name: Mapped[str | None] = mapped_column(String(128), nullable=True, comment="当前版本名称")
    summary: Mapped[str | None] = mapped_column(Text, nullable=True, comment="当前版本简介")
    category: Mapped[str | None] = mapped_column(
        String(32),
        nullable=True,
        comment="当前版本分类",
    )
    created_by_user_id: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="RESTRICT"),
        nullable=True,
        index=True,
        comment="创建条目的用户ID，官方条目为空",
    )


class MarketVersionRecord(Base):
    """市场条目的一个版本：提交那一刻生成的快照和它的审核结果。"""

    __tablename__ = "market_versions"
    __table_args__ = (
        CheckConstraint(
            "status IN ('pending', 'approved', 'rejected', 'withdrawn')",
            name="ck_market_versions_status",
        ),
        UniqueConstraint(
            "listing_id",
            "version_number",
            name="uq_market_versions_listing_version",
        ),
        # 每个条目最多只有一个待审版本，并发提交时由这个索引兜底。
        Index(
            "uq_market_versions_listing_pending",
            "listing_id",
            unique=True,
            postgresql_where=text("status = 'pending'"),
        ),
        {"comment": "市场条目版本"},
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, comment="版本ID")
    listing_id: Mapped[int] = mapped_column(
        ForeignKey("market_listings.id", ondelete="CASCADE"),
        index=True,
        comment="所属市场条目ID",
    )
    version_number: Mapped[int] = mapped_column(Integer, comment="版本号，从1开始递增")
    status: Mapped[str] = mapped_column(
        String(16),
        default="pending",
        index=True,
        comment="审核状态: pending待审/approved通过/rejected驳回/withdrawn撤回",
    )
    category: Mapped[str] = mapped_column(String(32), comment="提交时选择的分类")
    release_note: Mapped[str | None] = mapped_column(Text, nullable=True, comment="版本说明")
    # 快照创建后不可变，所以不用 MutableDict 跟踪原地修改。
    snapshot: Mapped[dict[str, Any]] = mapped_column(json_type(), comment="提交时生成的内容快照")
    snapshot_hash: Mapped[str] = mapped_column(String(64), comment="规范化快照JSON的sha256")
    submitted_by_user_id: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="RESTRICT"),
        nullable=True,
        index=True,
        comment="提交人用户ID，官方条目为空",
    )
    reviewed_by_user_id: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
        comment="审核人用户ID，官方条目为空",
    )
    reviewed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
        comment="审核时间",
    )
    review_comment: Mapped[str | None] = mapped_column(
        Text,
        nullable=True,
        comment="审核意见，驳回时必填",
    )


class MarketInstallRecord(Base):
    """工作区对市场条目的安装记录。

    Agent 和 Skill 都是软删除，不会触发外键的 SET NULL；安装是否仍有效，
    要请 agent 域或 skill 域确认目标对象还在。目标已删除视为未安装，重新安装时复用这一行。
    """

    __tablename__ = "market_installs"
    __table_args__ = (
        Index(
            "uq_market_installs_active_listing_workspace",
            "listing_id",
            "workspace_id",
            unique=True,
            postgresql_where=text("is_deleted = false"),
        ),
        {"comment": "市场安装记录"},
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, comment="安装记录ID")
    listing_id: Mapped[int] = mapped_column(
        ForeignKey("market_listings.id", ondelete="CASCADE"),
        index=True,
        comment="市场条目ID",
    )
    workspace_id: Mapped[int] = mapped_column(
        ForeignKey("workspaces.id", ondelete="CASCADE"),
        index=True,
        comment="安装到的工作区ID",
    )
    installed_version_number: Mapped[int] = mapped_column(Integer, comment="已安装的版本号")
    target_agent_id: Mapped[int | None] = mapped_column(
        ForeignKey("agents.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
        comment="安装出来的Agent ID",
    )
    target_skill_id: Mapped[int | None] = mapped_column(
        ForeignKey("skills.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
        comment="安装出来的Skill ID",
    )
    managed_skill_ids: Mapped[list[int]] = mapped_column(
        MutableList.as_mutable(json_type()),
        default=list,
        comment="安装Agent时新建或恢复的Skill ID，升级时只覆盖这些Skill",
    )
    installed_by_user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="RESTRICT"),
        index=True,
        comment="安装人用户ID",
    )
