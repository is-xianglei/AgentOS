"""将权限规则绑定到工作区，隔离不同租户的 global 规则。

Revision ID: 0026_permission_workspace_scope
Revises: 0025_turn_execution_lease
"""

import sqlalchemy as sa

from alembic import op

revision = "0026_permission_workspace_scope"
down_revision = "0025_turn_execution_lease"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "permission_rules",
        sa.Column(
            "workspace_id",
            sa.Integer(),
            sa.ForeignKey("workspaces.id", ondelete="CASCADE"),
            nullable=True,
            comment="所属工作区; global 规则必填",
        ),
    )
    op.create_index(
        "ix_permission_rules_workspace_id",
        "permission_rules",
        ["workspace_id"],
    )
    op.create_index(
        "ix_permission_rules_workspace_scope_session_tool",
        "permission_rules",
        ["workspace_id", "scope", "session_id", "tool_name"],
    )
    # session 规则可以从所属会话安全回填。历史 global 规则无法可靠判断
    # 属于哪个工作区，保留 NULL；新运行时会忽略这类遗留规则，避免跨租户生效。
    op.execute(
        """
        UPDATE permission_rules AS pr
        SET workspace_id = s.workspace_id
        FROM sessions AS s
        WHERE pr.scope = 'session'
          AND pr.session_id = s.id
          AND pr.workspace_id IS NULL
        """
    )


def downgrade() -> None:
    op.drop_index(
        "ix_permission_rules_workspace_scope_session_tool",
        table_name="permission_rules",
    )
    op.drop_index("ix_permission_rules_workspace_id", table_name="permission_rules")
    op.drop_column("permission_rules", "workspace_id")
