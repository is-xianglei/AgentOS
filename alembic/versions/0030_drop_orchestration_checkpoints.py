"""删除编排检查点表 orchestration_checkpoints。

这张表对应的 OrchestrationCheckpointRecord 和检查点存储实现从未接入生产代码，已随死代码一并删除。
迁移历史里没有建它的迁移，它是由启动时 create_all 按旧 ORM 建出来的，所以这里用 IF EXISTS，
没建过这张表的库也能正常升级。

Revision ID: 0030_drop_orchestration_ckpts
Revises: 0029_market_stale_skill_links
"""

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "0030_drop_orchestration_ckpts"
down_revision = "0029_market_stale_skill_links"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("DROP TABLE IF EXISTS orchestration_checkpoints")


def downgrade() -> None:
    # 按删除前的 ORM 定义重建空表，列、注释、外键和索引与 create_all 建出的结构一致；数据无法还原。
    op.create_table(
        "orchestration_checkpoints",
        sa.Column(
            "run_id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
            comment="编排运行ID",
        ),
        sa.Column("session_id", sa.Integer(), nullable=False, comment="所属会话ID"),
        sa.Column(
            "turn_id",
            postgresql.UUID(as_uuid=True),
            nullable=False,
            comment="所属交互轮次ID",
        ),
        sa.Column(
            "cursor",
            sa.String(length=160),
            nullable=True,
            comment="最近完成的编排游标",
        ),
        sa.Column(
            "state",
            postgresql.JSONB(),
            nullable=False,
            comment="节点之间传递的编排状态",
        ),
        sa.Column("revision", sa.Integer(), nullable=False, comment="乐观锁版本"),
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
        sa.ForeignKeyConstraint(
            ["session_id"],
            ["sessions.id"],
            name="orchestration_checkpoints_session_id_fkey",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["turn_id"],
            ["session_turns.id"],
            name="orchestration_checkpoints_turn_id_fkey",
            ondelete="CASCADE",
        ),
        comment="编排运行检查点",
    )
    op.create_index(
        "ix_orchestration_checkpoints_session_id",
        "orchestration_checkpoints",
        ["session_id"],
    )
    op.create_index(
        "ix_orchestration_checkpoints_turn_id",
        "orchestration_checkpoints",
        ["turn_id"],
    )
    op.create_index(
        "ix_orchestration_checkpoints_is_deleted",
        "orchestration_checkpoints",
        ["is_deleted"],
    )
