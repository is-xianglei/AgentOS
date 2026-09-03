"""新增Workflow编排定义表、成员绑定表，并为会话增加workflow_id锁定列。

Revision ID: 0022_create_workflows
Revises: 0021_message_llm_attribution
Create Date: 2026-09-02
"""

import sqlalchemy as sa

from alembic import op

revision = "0022_create_workflows"
down_revision = "0021_message_llm_attribution"
branch_labels = None
depends_on = None


def _audit_columns() -> list[sa.Column]:
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
        "workflows",
        sa.Column("id", sa.Integer(), primary_key=True, comment="Workflow ID"),
        sa.Column("workspace_id", sa.Integer(), nullable=False, comment="所属工作区ID"),
        sa.Column("name", sa.String(length=128), nullable=False, comment="Workflow名称"),
        sa.Column("description", sa.Text(), nullable=True, comment="Workflow用途说明"),
        sa.Column(
            "mode",
            sa.String(length=32),
            nullable=False,
            comment="编排模式: sequential/parallel/llm_routing",
        ),
        sa.Column(
            "is_enabled",
            sa.Boolean(),
            server_default=sa.true(),
            nullable=False,
            comment="是否启用，停用后拒绝运行",
        ),
        sa.Column("created_by_user_id", sa.Integer(), nullable=False, comment="创建者用户ID"),
        *_audit_columns(),
        sa.ForeignKeyConstraint(
            ["workspace_id"],
            ["workspaces.id"],
            name="fk_workflows_workspace_id_workspaces",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["created_by_user_id"],
            ["users.id"],
            name="fk_workflows_created_by_user_id_users",
            ondelete="RESTRICT",
        ),
        comment="Workflow编排定义表",
    )
    op.create_index("ix_workflows_workspace_id", "workflows", ["workspace_id"])
    op.create_index("ix_workflows_is_enabled", "workflows", ["is_enabled"])
    op.create_index("ix_workflows_created_by_user_id", "workflows", ["created_by_user_id"])
    op.create_index("ix_workflows_is_deleted", "workflows", ["is_deleted"])
    op.create_index(
        "ix_workflows_workspace_enabled",
        "workflows",
        ["workspace_id", "is_enabled"],
    )
    op.create_index(
        "uq_workflows_active_workspace_name",
        "workflows",
        ["workspace_id", "name"],
        unique=True,
        postgresql_where=sa.text("is_deleted = false"),
    )

    op.create_table(
        "workflow_agents",
        sa.Column("id", sa.Integer(), primary_key=True, comment="绑定ID"),
        sa.Column("workflow_id", sa.Integer(), nullable=False, comment="Workflow ID"),
        sa.Column("agent_id", sa.Integer(), nullable=False, comment="成员Agent ID"),
        sa.Column(
            "sort_order",
            sa.Integer(),
            server_default="0",
            nullable=False,
            comment="执行/展示顺序，越小越靠前",
        ),
        *_audit_columns(),
        sa.ForeignKeyConstraint(
            ["workflow_id"],
            ["workflows.id"],
            name="fk_workflow_agents_workflow_id_workflows",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["agent_id"],
            ["agents.id"],
            name="fk_workflow_agents_agent_id_agents",
            ondelete="RESTRICT",
        ),
        sa.UniqueConstraint("workflow_id", "agent_id", name="uq_workflow_agents_workflow_agent"),
        comment="Workflow成员Agent绑定表",
    )
    op.create_index("ix_workflow_agents_workflow_id", "workflow_agents", ["workflow_id"])
    op.create_index("ix_workflow_agents_agent_id", "workflow_agents", ["agent_id"])
    op.create_index("ix_workflow_agents_is_deleted", "workflow_agents", ["is_deleted"])

    op.add_column(
        "sessions",
        sa.Column(
            "workflow_id",
            sa.Integer(),
            nullable=True,
            comment="会话锁定的Workflow ID，空表示普通Agent会话",
        ),
    )
    op.create_foreign_key(
        "fk_sessions_workflow_id_workflows",
        "sessions",
        "workflows",
        ["workflow_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.create_index("ix_sessions_workflow_id", "sessions", ["workflow_id"])


def downgrade() -> None:
    op.drop_index("ix_sessions_workflow_id", table_name="sessions")
    op.drop_constraint("fk_sessions_workflow_id_workflows", "sessions", type_="foreignkey")
    op.drop_column("sessions", "workflow_id")

    op.drop_index("ix_workflow_agents_is_deleted", table_name="workflow_agents")
    op.drop_index("ix_workflow_agents_agent_id", table_name="workflow_agents")
    op.drop_index("ix_workflow_agents_workflow_id", table_name="workflow_agents")
    op.drop_table("workflow_agents")

    op.drop_index("uq_workflows_active_workspace_name", table_name="workflows")
    op.drop_index("ix_workflows_workspace_enabled", table_name="workflows")
    op.drop_index("ix_workflows_is_deleted", table_name="workflows")
    op.drop_index("ix_workflows_created_by_user_id", table_name="workflows")
    op.drop_index("ix_workflows_is_enabled", table_name="workflows")
    op.drop_index("ix_workflows_workspace_id", table_name="workflows")
    op.drop_table("workflows")
