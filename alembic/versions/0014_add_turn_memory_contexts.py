"""新增Turn级Memory冻结上下文。

Revision ID: 0014_add_turn_memory_contexts
Revises: 0013_add_memory_storage
Create Date: 2026-07-25
"""

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "0014_add_turn_memory_contexts"
down_revision = "0013_add_memory_storage"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("CREATE EXTENSION IF NOT EXISTS pg_trgm")
    op.create_table(
        "turn_memory_contexts",
        sa.Column("id", sa.BigInteger(), nullable=False, comment="Memory上下文ID"),
        sa.Column(
            "turn_id",
            postgresql.UUID(as_uuid=True),
            nullable=False,
            comment="所属交互轮次ID",
        ),
        sa.Column(
            "space_id",
            sa.BigInteger(),
            nullable=True,
            comment="读取时的Memory空间ID，无空间时为空",
        ),
        sa.Column(
            "catalog_version",
            sa.BigInteger(),
            server_default=sa.text("0"),
            nullable=False,
            comment="冻结时的Catalog版本",
        ),
        sa.Column(
            "selected_revision_ids",
            postgresql.JSONB(),
            server_default=sa.text("'[]'::jsonb"),
            nullable=False,
            comment="按注入顺序保存的Revision ID",
        ),
        sa.Column(
            "rendered_catalog",
            sa.Text(),
            server_default="",
            nullable=False,
            comment="发给主模型的精确Catalog文本",
        ),
        sa.Column(
            "rendered_memories",
            sa.Text(),
            server_default="",
            nullable=False,
            comment="发给模型的精确相关Memory文本",
        ),
        sa.Column(
            "selector_status",
            sa.String(length=16),
            nullable=False,
            comment="选择状态: selected/empty/degraded",
        ),
        sa.Column(
            "degraded_reason",
            sa.String(length=200),
            nullable=True,
            comment="降级原因代码",
        ),
        sa.Column(
            "byte_count",
            sa.Integer(),
            server_default=sa.text("0"),
            nullable=False,
            comment="Catalog与相关Memory的UTF-8总字节数",
        ),
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
        sa.CheckConstraint(
            "catalog_version >= 0",
            name="ck_turn_memory_contexts_catalog_version",
        ),
        sa.CheckConstraint(
            "selector_status IN ('selected', 'empty', 'degraded')",
            name="ck_turn_memory_contexts_selector_status",
        ),
        sa.CheckConstraint(
            "byte_count >= 0",
            name="ck_turn_memory_contexts_byte_count",
        ),
        sa.ForeignKeyConstraint(
            ["turn_id"],
            ["session_turns.id"],
            name="fk_turn_memory_contexts_turn_id",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["space_id"],
            ["memory_spaces.id"],
            name="fk_turn_memory_contexts_space_id",
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_turn_memory_contexts"),
        sa.UniqueConstraint("turn_id", name="uq_turn_memory_contexts_turn_id"),
        comment="交互轮次冻结的Memory请求上下文",
    )
    op.create_index(
        "ix_turn_memory_contexts_space_id",
        "turn_memory_contexts",
        ["space_id"],
    )
    op.create_index(
        "ix_turn_memory_contexts_is_deleted",
        "turn_memory_contexts",
        ["is_deleted"],
    )

    # 0012 已预留该列，但此前不存在可引用的目标表，历史非空值无法具备可信含义。
    op.execute(
        "UPDATE session_turns SET memory_context_id = NULL WHERE memory_context_id IS NOT NULL"
    )
    op.create_foreign_key(
        "fk_session_turns_memory_context_id",
        "session_turns",
        "turn_memory_contexts",
        ["memory_context_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.create_unique_constraint(
        "uq_session_turns_memory_context_id",
        "session_turns",
        ["memory_context_id"],
    )


def downgrade() -> None:
    op.drop_constraint(
        "uq_session_turns_memory_context_id",
        "session_turns",
        type_="unique",
    )
    op.drop_constraint(
        "fk_session_turns_memory_context_id",
        "session_turns",
        type_="foreignkey",
    )
    op.drop_table("turn_memory_contexts")
    # pg_trgm 扩展可能被库内其他对象共享，此处不做 DROP EXTENSION，
    # 避免影响无关索引；如需彻底移除请人工确认后单独执行。
