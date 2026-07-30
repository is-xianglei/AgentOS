"""新增PostgreSQL Memory存储表。

Revision ID: 0013_add_memory_storage
Revises: 0012_add_session_turns
Create Date: 2026-07-25
"""

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "0013_add_memory_storage"
down_revision = "0012_add_session_turns"
branch_labels = None
depends_on = None


def _audit_columns() -> list[sa.Column]:
    """为新业务表生成与 Base 一致的审计字段。"""
    return [
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
            comment="创建时间",
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
            comment="更新时间",
        ),
        sa.Column(
            "is_deleted",
            sa.Boolean(),
            server_default=sa.false(),
            nullable=False,
            comment="软删除标记: false 有效 / true 已删除",
        ),
        sa.Column(
            "deleted_at",
            sa.DateTime(timezone=True),
            nullable=True,
            comment="软删除时间, 空表示未删除",
        ),
    ]


def upgrade() -> None:
    op.create_table(
        "memory_spaces",
        sa.Column("id", sa.BigInteger(), nullable=False, comment="Memory空间ID"),
        sa.Column("workspace_id", sa.Integer(), nullable=False, comment="工作区ID"),
        sa.Column("user_id", sa.Integer(), nullable=False, comment="私有所有者用户ID"),
        sa.Column(
            "catalog_version",
            sa.BigInteger(),
            server_default=sa.text("0"),
            nullable=False,
            comment="有效Memory目录变更计数器",
        ),
        sa.Column(
            "last_dream_at",
            sa.DateTime(timezone=True),
            nullable=True,
            comment="最近一次Dream完成时间",
        ),
        sa.Column(
            "last_scan_at",
            sa.DateTime(timezone=True),
            nullable=True,
            comment="最近一次Dream扫描时间",
        ),
        sa.Column(
            "sessions_since_dream",
            sa.Integer(),
            server_default=sa.text("0"),
            nullable=False,
            comment="上次Dream后完成的不同会话数",
        ),
        sa.Column(
            "settings",
            postgresql.JSONB(),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
            comment="Memory空间设置和预算",
        ),
        *_audit_columns(),
        sa.CheckConstraint(
            "catalog_version >= 0",
            name="ck_memory_spaces_catalog_version",
        ),
        sa.CheckConstraint(
            "sessions_since_dream >= 0",
            name="ck_memory_spaces_sessions_since_dream",
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id"],
            ["workspaces.id"],
            name="fk_memory_spaces_workspace_id",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["users.id"],
            name="fk_memory_spaces_user_id",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_memory_spaces"),
        comment="用户在工作区内的私有Memory空间",
    )
    op.create_index("ix_memory_spaces_workspace_id", "memory_spaces", ["workspace_id"])
    op.create_index("ix_memory_spaces_user_id", "memory_spaces", ["user_id"])
    op.create_index("ix_memory_spaces_is_deleted", "memory_spaces", ["is_deleted"])
    op.create_index(
        "uq_memory_spaces_active_user_workspace",
        "memory_spaces",
        ["workspace_id", "user_id"],
        unique=True,
        postgresql_where=sa.text("is_deleted = false"),
    )

    op.create_table(
        "memory_items",
        sa.Column("id", sa.BigInteger(), nullable=False, comment="Memory ID"),
        sa.Column("space_id", sa.BigInteger(), nullable=False, comment="所属Memory空间ID"),
        sa.Column("memory_key", sa.String(length=160), nullable=False, comment="稳定业务标识"),
        sa.Column("memory_type", sa.String(length=16), nullable=False, comment="Memory类型"),
        sa.Column("name", sa.String(length=200), nullable=False, comment="人类可读标题"),
        sa.Column(
            "description",
            sa.String(length=500),
            nullable=False,
            comment="供选择器使用的摘要",
        ),
        sa.Column("body", sa.Text(), nullable=False, comment="Markdown正文"),
        sa.Column(
            "status",
            sa.String(length=16),
            server_default="active",
            nullable=False,
            comment="状态: active/superseded/archived",
        ),
        sa.Column(
            "version",
            sa.Integer(),
            server_default=sa.text("1"),
            nullable=False,
            comment="乐观锁版本",
        ),
        sa.Column(
            "superseded_by_id",
            sa.BigInteger(),
            nullable=True,
            comment="替代当前旧Memory的新Memory ID",
        ),
        sa.Column(
            "source_kind",
            sa.String(length=16),
            server_default="explicit",
            nullable=False,
            comment="来源: explicit/extracted/dream/manual",
        ),
        sa.Column(
            "last_used_at",
            sa.DateTime(timezone=True),
            nullable=True,
            comment="最近一次被Recall使用时间",
        ),
        sa.Column(
            "use_count",
            sa.Integer(),
            server_default=sa.text("0"),
            nullable=False,
            comment="Recall使用次数",
        ),
        *_audit_columns(),
        sa.CheckConstraint(
            "memory_type IN ('user', 'feedback', 'project', 'reference')",
            name="ck_memory_items_type",
        ),
        sa.CheckConstraint(
            "status IN ('active', 'superseded', 'archived')",
            name="ck_memory_items_status",
        ),
        sa.CheckConstraint(
            "source_kind IN ('explicit', 'extracted', 'dream', 'manual')",
            name="ck_memory_items_source_kind",
        ),
        sa.CheckConstraint("version >= 1", name="ck_memory_items_version"),
        sa.CheckConstraint("use_count >= 0", name="ck_memory_items_use_count"),
        sa.CheckConstraint(
            "octet_length(body) <= 16384",
            name="ck_memory_items_body_bytes",
        ),
        sa.ForeignKeyConstraint(
            ["space_id"],
            ["memory_spaces.id"],
            name="fk_memory_items_space_id",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["superseded_by_id"],
            ["memory_items.id"],
            name="fk_memory_items_superseded_by_id",
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_memory_items"),
        comment="Memory稳定逻辑记录",
    )
    op.create_index("ix_memory_items_space_id", "memory_items", ["space_id"])
    op.create_index("ix_memory_items_memory_key", "memory_items", ["memory_key"])
    op.create_index("ix_memory_items_memory_type", "memory_items", ["memory_type"])
    op.create_index("ix_memory_items_status", "memory_items", ["status"])
    op.create_index(
        "ix_memory_items_superseded_by_id",
        "memory_items",
        ["superseded_by_id"],
    )
    op.create_index("ix_memory_items_is_deleted", "memory_items", ["is_deleted"])
    op.create_index(
        "uq_memory_items_active_memory_key",
        "memory_items",
        ["space_id", "memory_key"],
        unique=True,
        postgresql_where=sa.text("status = 'active' AND is_deleted = false"),
    )
    op.create_index(
        "ix_memory_catalog",
        "memory_items",
        ["space_id", "status", sa.text("updated_at DESC"), "id"],
    )

    op.create_table(
        "memory_revisions",
        sa.Column("id", sa.BigInteger(), nullable=False, comment="修订ID"),
        sa.Column("memory_id", sa.BigInteger(), nullable=False, comment="Memory ID"),
        sa.Column("revision", sa.Integer(), nullable=False, comment="修订序号"),
        sa.Column(
            "memory_type",
            sa.String(length=16),
            nullable=False,
            comment="该修订的Memory类型",
        ),
        sa.Column("name", sa.String(length=200), nullable=False, comment="该修订的标题"),
        sa.Column(
            "description",
            sa.String(length=500),
            nullable=False,
            comment="该修订的摘要",
        ),
        sa.Column("body", sa.Text(), nullable=False, comment="该修订的Markdown正文"),
        sa.Column(
            "actor_type",
            sa.String(length=16),
            nullable=False,
            comment="写入者类型: user/extractor/dream/system",
        ),
        sa.Column("actor_id", sa.String(length=120), nullable=True, comment="写入者标识"),
        sa.Column(
            "run_id",
            postgresql.UUID(as_uuid=True),
            nullable=True,
            comment="关联后台运行ID",
        ),
        *_audit_columns(),
        sa.CheckConstraint("revision >= 1", name="ck_memory_revisions_revision"),
        sa.CheckConstraint(
            "memory_type IN ('user', 'feedback', 'project', 'reference')",
            name="ck_memory_revisions_type",
        ),
        sa.CheckConstraint(
            "octet_length(body) <= 16384",
            name="ck_memory_revisions_body_bytes",
        ),
        sa.ForeignKeyConstraint(
            ["memory_id"],
            ["memory_items.id"],
            name="fk_memory_revisions_memory_id",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_memory_revisions"),
        sa.UniqueConstraint(
            "memory_id",
            "revision",
            name="uq_memory_revisions_memory_revision",
        ),
        comment="Memory不可变修订快照",
    )
    op.create_index("ix_memory_revisions_memory_id", "memory_revisions", ["memory_id"])
    op.create_index("ix_memory_revisions_run_id", "memory_revisions", ["run_id"])
    op.create_index("ix_memory_revisions_is_deleted", "memory_revisions", ["is_deleted"])

    op.create_table(
        "memory_sources",
        sa.Column("id", sa.BigInteger(), nullable=False, comment="来源ID"),
        sa.Column("memory_id", sa.BigInteger(), nullable=False, comment="Memory ID"),
        sa.Column("revision_id", sa.BigInteger(), nullable=False, comment="修订ID"),
        sa.Column("session_id", sa.Integer(), nullable=True, comment="来源会话ID"),
        sa.Column(
            "turn_id",
            postgresql.UUID(as_uuid=True),
            nullable=True,
            comment="来源交互轮次ID",
        ),
        sa.Column("from_message_id", sa.Integer(), nullable=True, comment="来源消息起点ID"),
        sa.Column("to_message_id", sa.Integer(), nullable=True, comment="来源消息终点ID"),
        sa.Column("source_kind", sa.String(length=16), nullable=False, comment="来源类型"),
        sa.Column("source_excerpt", sa.Text(), nullable=True, comment="短来源证据"),
        *_audit_columns(),
        sa.CheckConstraint(
            "source_kind IN ('explicit', 'extracted', 'dream', 'manual')",
            name="ck_memory_sources_kind",
        ),
        sa.CheckConstraint(
            "source_excerpt IS NULL OR char_length(source_excerpt) <= 1000",
            name="ck_memory_sources_excerpt_length",
        ),
        sa.CheckConstraint(
            "from_message_id IS NULL OR to_message_id IS NULL OR from_message_id <= to_message_id",
            name="ck_memory_sources_message_range",
        ),
        sa.ForeignKeyConstraint(
            ["memory_id"],
            ["memory_items.id"],
            name="fk_memory_sources_memory_id",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["revision_id"],
            ["memory_revisions.id"],
            name="fk_memory_sources_revision_id",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["session_id"],
            ["sessions.id"],
            name="fk_memory_sources_session_id",
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["turn_id"],
            ["session_turns.id"],
            name="fk_memory_sources_turn_id",
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["from_message_id"],
            ["session_messages.id"],
            name="fk_memory_sources_from_message_id",
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["to_message_id"],
            ["session_messages.id"],
            name="fk_memory_sources_to_message_id",
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_memory_sources"),
        sa.UniqueConstraint(
            "memory_id",
            "turn_id",
            "from_message_id",
            "to_message_id",
            "source_kind",
            name="uq_memory_sources_provenance",
        ),
        comment="Memory来源证据",
    )
    op.create_index("ix_memory_sources_memory_id", "memory_sources", ["memory_id"])
    op.create_index("ix_memory_sources_revision_id", "memory_sources", ["revision_id"])
    op.create_index("ix_memory_sources_session_id", "memory_sources", ["session_id"])
    op.create_index("ix_memory_sources_turn_id", "memory_sources", ["turn_id"])
    op.create_index(
        "ix_memory_sources_from_message_id",
        "memory_sources",
        ["from_message_id"],
    )
    op.create_index(
        "ix_memory_sources_to_message_id",
        "memory_sources",
        ["to_message_id"],
    )
    op.create_index("ix_memory_sources_is_deleted", "memory_sources", ["is_deleted"])


def downgrade() -> None:
    op.drop_table("memory_sources")
    op.drop_table("memory_revisions")
    op.drop_table("memory_items")
    op.drop_table("memory_spaces")
