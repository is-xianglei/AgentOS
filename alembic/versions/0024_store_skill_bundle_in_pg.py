"""Skill 正文与附属文件改存 PostgreSQL,新增启用开关与创建者。

存量行不回填:MinIO 中的历史内容不迁移,content 以空串占位、extra_files 为空对象,
需重新上传后才能使用。

Revision ID: 0024_store_skill_bundle_in_pg
Revises: 0023_subagent_run_agent_id
Create Date: 2026-09-05
"""

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "0024_store_skill_bundle_in_pg"
down_revision = "0023_subagent_run_agent_id"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # 临时 server_default 仅为让存量行通过 NOT NULL,加列后立即去掉,新写入由应用层赋值。
    op.add_column(
        "skills",
        sa.Column(
            "content",
            sa.Text(),
            nullable=False,
            server_default="",
            comment="SKILL.md原文(含frontmatter)",
        ),
    )
    op.alter_column("skills", "content", server_default=None)
    op.add_column(
        "skills",
        sa.Column(
            "extra_files",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
            comment="附属文件,键为相对skill根的路径,值含encoding与content",
        ),
    )
    op.alter_column("skills", "extra_files", server_default=None)
    op.add_column(
        "skills",
        sa.Column(
            "is_enabled",
            sa.Boolean(),
            nullable=False,
            server_default=sa.true(),
            comment="是否启用,禁用后不进catalog且Skill工具不可访问",
        ),
    )
    op.create_index("ix_skills_is_enabled", "skills", ["is_enabled"])
    op.add_column(
        "skills",
        sa.Column(
            "created_by_user_id",
            sa.Integer(),
            nullable=True,
            comment="创建者用户ID,改PG存储前的历史记录为空",
        ),
    )
    op.create_foreign_key(
        "fk_skills_created_by_user_id_users",
        "skills",
        "users",
        ["created_by_user_id"],
        ["id"],
        ondelete="RESTRICT",
    )
    op.create_index("ix_skills_created_by_user_id", "skills", ["created_by_user_id"])
    op.alter_column(
        "skills",
        "skill_hash",
        existing_type=sa.String(length=64),
        existing_nullable=True,
        comment="规范化bundle(SKILL.md+附属文件)的sha256,幂等写入判重",
        existing_comment="上传zip包的哈希,幂等上传判重",
    )


def downgrade() -> None:
    op.alter_column(
        "skills",
        "skill_hash",
        existing_type=sa.String(length=64),
        existing_nullable=True,
        comment="上传zip包的哈希,幂等上传判重",
        existing_comment="规范化bundle(SKILL.md+附属文件)的sha256,幂等写入判重",
    )
    op.drop_index("ix_skills_created_by_user_id", table_name="skills")
    op.drop_constraint("fk_skills_created_by_user_id_users", "skills", type_="foreignkey")
    op.drop_column("skills", "created_by_user_id")
    op.drop_index("ix_skills_is_enabled", table_name="skills")
    op.drop_column("skills", "is_enabled")
    op.drop_column("skills", "extra_files")
    op.drop_column("skills", "content")
