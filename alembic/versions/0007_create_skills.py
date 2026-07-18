"""创建 skills 表(web端上传的skill索引与元数据)

Revision ID: 0007_create_skills
Revises: 0006_add_audit_fields
Create Date: 2026-07-18

skill 的正文与资源文件存对象存储(MinIO),此表只存索引与管理元数据。
审计四列(created_at/updated_at/is_deleted/deleted_at)由 db.base.Base 下发,
但建表迁移中仍须逐列手写(项目手写迁移,不依赖 autogenerate)。
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "0007_create_skills"
down_revision = "0006_add_audit_fields"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "skills",
        sa.Column("id", sa.Integer(), primary_key=True, comment="skillID"),
        sa.Column("name", sa.String(length=64), nullable=False, comment="skill名称(kebab-case)"),
        sa.Column("description", sa.Text(), nullable=False, comment="skill描述"),
        sa.Column("frontmatter", postgresql.JSONB(), nullable=False, comment="解析后的完整frontmatter"),
        sa.Column("version", sa.String(length=32), nullable=True, comment="版本号"),
        sa.Column("skill_hash", sa.String(length=64), nullable=True, comment="上传zip包的哈希"),
        sa.Column("scope", sa.String(length=16), nullable=False, server_default="global", comment="作用域"),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), comment="创建时间"),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), comment="更新时间"),
        sa.Column("is_deleted", sa.Boolean(), server_default=sa.false(), nullable=False, comment="软删除标记"),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True, comment="软删除时间"),
        comment="web端上传的skill(正文与资源存对象存储,此表存索引与元数据)",
    )
    op.create_index("ix_skills_name", "skills", ["name"], unique=True)
    op.create_index("ix_skills_is_deleted", "skills", ["is_deleted"])
    op.create_index("ix_skills_skill_hash", "skills", ["skill_hash"])


def downgrade() -> None:
    op.drop_index("ix_skills_skill_hash", table_name="skills")
    op.drop_index("ix_skills_is_deleted", table_name="skills")
    op.drop_index("ix_skills_name", table_name="skills")
    op.drop_table("skills")
