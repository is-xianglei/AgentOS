"""为交互轮次增加执行租约与事件offset列，run 即 turn，不新建 run 表。

存量行 last_event_offset 回填 0；迁移前遗留的 running 轮次 lease_owner 为空，
由启动时的孤儿回收按 interrupted 收尾。

Revision ID: 0025_turn_execution_lease
Revises: 0024_store_skill_bundle_in_pg
Create Date: 2026-09-05
"""

import sqlalchemy as sa

from alembic import op

revision = "0025_turn_execution_lease"
down_revision = "0024_store_skill_bundle_in_pg"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "session_turns",
        sa.Column(
            "lease_owner",
            sa.String(160),
            nullable=True,
            comment="执行租约持有者(进程实例ID加执行ID),为空表示没有执行中的任务",
        ),
    )
    op.add_column(
        "session_turns",
        sa.Column(
            "lease_expires_at",
            sa.DateTime(timezone=True),
            nullable=True,
            comment="执行租约到期时间,心跳续约,过期后可被回收或重新认领",
        ),
    )
    op.add_column(
        "session_turns",
        sa.Column(
            "last_event_offset",
            sa.BigInteger(),
            nullable=False,
            server_default=sa.text("0"),
            comment="已分配的最大事件offset,跨多次执行连续递增、不复用",
        ),
    )
    op.create_index(
        "ix_session_turns_lease_expires_at",
        "session_turns",
        ["lease_expires_at"],
        postgresql_where=sa.text("lease_owner IS NOT NULL"),
    )


def downgrade() -> None:
    op.drop_index("ix_session_turns_lease_expires_at", table_name="session_turns")
    op.drop_column("session_turns", "last_event_offset")
    op.drop_column("session_turns", "lease_expires_at")
    op.drop_column("session_turns", "lease_owner")
