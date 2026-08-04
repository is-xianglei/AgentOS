"""对齐Agent MVP相关数据库注释。

Revision ID: 0020_agent_mvp_comments
Revises: 0019_agent_tool_catalog
Create Date: 2026-08-04
"""

import sqlalchemy as sa

from alembic import op

revision = "0020_agent_mvp_comments"
down_revision = "0019_agent_tool_catalog"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.alter_column(
        "skills",
        "name",
        existing_type=sa.String(length=64),
        existing_nullable=False,
        comment="Skill名称，工作区内覆盖更新的键",
        existing_comment="skill名称;覆盖更新与工具路由的键",
    )
    op.create_table_comment(
        "skills",
        "工作区上传的Skill",
        existing_comment="web端上传的skill",
    )
    op.alter_column(
        "agent_tools",
        "tool_id",
        existing_type=sa.Integer(),
        existing_nullable=False,
        comment="工具定义ID",
        existing_comment="工具ID",
    )


def downgrade() -> None:
    op.alter_column(
        "agent_tools",
        "tool_id",
        existing_type=sa.Integer(),
        existing_nullable=False,
        comment="工具ID",
        existing_comment="工具定义ID",
    )
    op.create_table_comment(
        "skills",
        "web端上传的skill",
        existing_comment="工作区上传的Skill",
    )
    op.alter_column(
        "skills",
        "name",
        existing_type=sa.String(length=64),
        existing_nullable=False,
        comment="skill名称;覆盖更新与工具路由的键",
        existing_comment="Skill名称，工作区内覆盖更新的键",
    )
