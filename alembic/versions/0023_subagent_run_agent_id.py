"""为子代理运行记录增加agent_id列，供Workflow成员运行记录反查Agent定义。

存量行不回填：历史运行记录无法可靠反推 Agent，保持 NULL。

Revision ID: 0023_subagent_run_agent_id
Revises: 0022_create_workflows
Create Date: 2026-09-04
"""

import sqlalchemy as sa

from alembic import op

revision = "0023_subagent_run_agent_id"
down_revision = "0022_create_workflows"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "subagent_runs",
        sa.Column(
            "agent_id",
            sa.Integer(),
            nullable=True,
            comment="成员对应的Agent ID，仅Workflow成员填写，内置子代理与团队成员为空",
        ),
    )
    op.create_foreign_key(
        "fk_subagent_runs_agent_id_agents",
        "subagent_runs",
        "agents",
        ["agent_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.create_index("ix_subagent_runs_agent_id", "subagent_runs", ["agent_id"])


def downgrade() -> None:
    op.drop_index("ix_subagent_runs_agent_id", table_name="subagent_runs")
    op.drop_constraint("fk_subagent_runs_agent_id_agents", "subagent_runs", type_="foreignkey")
    op.drop_column("subagent_runs", "agent_id")
