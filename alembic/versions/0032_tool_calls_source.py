"""tool_calls 新增 source 列：记录工具来源，NULL 为内置工具，MCP 工具为 mcp:{server_key}。

按 source 加 status 可以统计每个 MCP server 的调用量与失败率。已有记录全是内置工具，
新列可空、不回填。

Revision ID: 0032_tool_calls_source
Revises: 0031_mcp_servers
"""

import sqlalchemy as sa

from alembic import op

revision = "0032_tool_calls_source"
down_revision = "0031_mcp_servers"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "tool_calls",
        sa.Column(
            "source",
            sa.String(length=64),
            nullable=True,
            comment="工具来源，NULL为内置工具，MCP工具为mcp:{server_key}",
        ),
    )
    op.create_index("ix_tool_calls_source", "tool_calls", ["source"])


def downgrade() -> None:
    op.drop_index("ix_tool_calls_source", table_name="tool_calls")
    op.drop_column("tool_calls", "source")
