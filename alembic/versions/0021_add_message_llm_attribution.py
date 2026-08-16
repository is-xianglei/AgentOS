"""session_messages 增加 provider/model 成本归因列。

两列均可空：仅 LLM 产出的 assistant 消息填写，user/tool 消息与存量历史
消息保持 NULL，迁移不做回填。

Revision ID: 0021_message_llm_attribution
Revises: 0020_agent_mvp_comments
Create Date: 2026-08-16
"""

import sqlalchemy as sa

from alembic import op

revision = "0021_message_llm_attribution"
down_revision = "0020_agent_mvp_comments"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "session_messages",
        sa.Column(
            "provider",
            sa.String(length=32),
            nullable=True,
            comment="产出该消息的LLM提供商标识（anthropic/openai），非模型产出的消息为空",
        ),
    )
    op.add_column(
        "session_messages",
        sa.Column(
            "model",
            sa.String(length=120),
            nullable=True,
            comment="本次调用实际生效的模型ID，非模型产出的消息为空",
        ),
    )


def downgrade() -> None:
    op.drop_column("session_messages", "model")
    op.drop_column("session_messages", "provider")
