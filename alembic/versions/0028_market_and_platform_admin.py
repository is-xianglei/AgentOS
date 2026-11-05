"""新增平台管理员标记，以及市场的条目、版本、安装记录三张表。

Revision ID: 0028_market_platform_admin
Revises: 0027_model_service_options
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "0028_market_platform_admin"
down_revision = "0027_model_service_options"
branch_labels = None
depends_on = None


def _audit_columns() -> list[sa.Column]:
    # 注释与 database/base.py 中 Base 的审计字段保持一致，避免 autogenerate 报出注释差异。
    return [
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
            comment="创建时间",
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
            comment="更新时间",
        ),
        sa.Column(
            "is_deleted",
            sa.Boolean(),
            nullable=False,
            server_default=sa.false(),
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
    op.add_column(
        "users",
        sa.Column(
            "is_platform_admin",
            sa.Boolean(),
            nullable=False,
            server_default=sa.false(),
            comment="是否平台管理员（平台首个注册用户）",
        ),
    )
    # 回填：id 最小且未软删除的用户成为平台管理员；没有这样的用户时子查询为空，不更新任何行。
    op.execute(
        """
        UPDATE users
        SET is_platform_admin = true
        WHERE id = (SELECT min(id) FROM users WHERE is_deleted = false)
        """
    )

    op.create_table(
        "market_listings",
        sa.Column("id", sa.Integer(), primary_key=True, comment="市场条目ID"),
        sa.Column("kind", sa.String(16), nullable=False, comment="条目类型: agent/skill"),
        sa.Column("is_official", sa.Boolean(), nullable=False, comment="是否官方预置"),
        sa.Column(
            "official_key",
            sa.String(128),
            nullable=True,
            comment="官方预置的目录名，社区条目为空",
        ),
        sa.Column(
            "publisher_workspace_id",
            sa.Integer(),
            sa.ForeignKey("workspaces.id", ondelete="CASCADE"),
            nullable=True,
            comment="发布条目的工作区ID，官方条目为空",
        ),
        sa.Column(
            "source_agent_id",
            sa.Integer(),
            sa.ForeignKey("agents.id", ondelete="SET NULL"),
            nullable=True,
            comment="源Agent ID，仅agent条目有值",
        ),
        sa.Column(
            "source_skill_id",
            sa.Integer(),
            sa.ForeignKey("skills.id", ondelete="SET NULL"),
            nullable=True,
            comment="源Skill ID，仅skill条目有值",
        ),
        sa.Column(
            "status",
            sa.String(16),
            nullable=False,
            comment="条目状态: unlisted未有通过版本/listed已上架/delisted作者下架/removed平台下架",
        ),
        sa.Column("status_reason", sa.Text(), nullable=True, comment="下架原因"),
        sa.Column(
            "current_version_number",
            sa.Integer(),
            nullable=True,
            comment="当前版本号，即最新审核通过的版本，没有时为空",
        ),
        sa.Column("name", sa.String(128), nullable=True, comment="当前版本名称"),
        sa.Column("summary", sa.Text(), nullable=True, comment="当前版本简介"),
        sa.Column("category", sa.String(32), nullable=True, comment="当前版本分类"),
        sa.Column(
            "created_by_user_id",
            sa.Integer(),
            sa.ForeignKey("users.id", ondelete="RESTRICT"),
            nullable=True,
            comment="创建条目的用户ID，官方条目为空",
        ),
        *_audit_columns(),
        sa.CheckConstraint("kind IN ('agent', 'skill')", name="ck_market_listings_kind"),
        sa.CheckConstraint(
            "status IN ('unlisted', 'listed', 'delisted', 'removed')",
            name="ck_market_listings_status",
        ),
        comment="市场条目",
    )
    for column in ("publisher_workspace_id", "status", "created_by_user_id", "is_deleted"):
        op.create_index(f"ix_market_listings_{column}", "market_listings", [column])
    op.create_index(
        "uq_market_listings_kind_official_key",
        "market_listings",
        ["kind", "official_key"],
        unique=True,
        postgresql_where=sa.text("official_key IS NOT NULL"),
    )
    op.create_index(
        "uq_market_listings_source_agent_id",
        "market_listings",
        ["source_agent_id"],
        unique=True,
        postgresql_where=sa.text("source_agent_id IS NOT NULL"),
    )
    op.create_index(
        "uq_market_listings_source_skill_id",
        "market_listings",
        ["source_skill_id"],
        unique=True,
        postgresql_where=sa.text("source_skill_id IS NOT NULL"),
    )

    op.create_table(
        "market_versions",
        sa.Column("id", sa.Integer(), primary_key=True, comment="版本ID"),
        sa.Column(
            "listing_id",
            sa.Integer(),
            sa.ForeignKey("market_listings.id", ondelete="CASCADE"),
            nullable=False,
            comment="所属市场条目ID",
        ),
        sa.Column("version_number", sa.Integer(), nullable=False, comment="版本号，从1开始递增"),
        sa.Column(
            "status",
            sa.String(16),
            nullable=False,
            comment="审核状态: pending待审/approved通过/rejected驳回/withdrawn撤回",
        ),
        sa.Column("category", sa.String(32), nullable=False, comment="提交时选择的分类"),
        sa.Column("release_note", sa.Text(), nullable=True, comment="版本说明"),
        sa.Column(
            "snapshot",
            postgresql.JSONB(),
            nullable=False,
            comment="提交时生成的内容快照",
        ),
        sa.Column(
            "snapshot_hash",
            sa.String(64),
            nullable=False,
            comment="规范化快照JSON的sha256",
        ),
        sa.Column(
            "submitted_by_user_id",
            sa.Integer(),
            sa.ForeignKey("users.id", ondelete="RESTRICT"),
            nullable=True,
            comment="提交人用户ID，官方条目为空",
        ),
        sa.Column(
            "reviewed_by_user_id",
            sa.Integer(),
            sa.ForeignKey("users.id", ondelete="SET NULL"),
            nullable=True,
            comment="审核人用户ID，官方条目为空",
        ),
        sa.Column("reviewed_at", sa.DateTime(timezone=True), nullable=True, comment="审核时间"),
        sa.Column("review_comment", sa.Text(), nullable=True, comment="审核意见，驳回时必填"),
        *_audit_columns(),
        sa.CheckConstraint(
            "status IN ('pending', 'approved', 'rejected', 'withdrawn')",
            name="ck_market_versions_status",
        ),
        sa.UniqueConstraint(
            "listing_id",
            "version_number",
            name="uq_market_versions_listing_version",
        ),
        comment="市场条目版本",
    )
    for column in (
        "listing_id",
        "status",
        "submitted_by_user_id",
        "reviewed_by_user_id",
        "is_deleted",
    ):
        op.create_index(f"ix_market_versions_{column}", "market_versions", [column])
    # 每个条目最多只有一个待审版本。
    op.create_index(
        "uq_market_versions_listing_pending",
        "market_versions",
        ["listing_id"],
        unique=True,
        postgresql_where=sa.text("status = 'pending'"),
    )

    op.create_table(
        "market_installs",
        sa.Column("id", sa.Integer(), primary_key=True, comment="安装记录ID"),
        sa.Column(
            "listing_id",
            sa.Integer(),
            sa.ForeignKey("market_listings.id", ondelete="CASCADE"),
            nullable=False,
            comment="市场条目ID",
        ),
        sa.Column(
            "workspace_id",
            sa.Integer(),
            sa.ForeignKey("workspaces.id", ondelete="CASCADE"),
            nullable=False,
            comment="安装到的工作区ID",
        ),
        sa.Column(
            "installed_version_number",
            sa.Integer(),
            nullable=False,
            comment="已安装的版本号",
        ),
        sa.Column(
            "target_agent_id",
            sa.Integer(),
            sa.ForeignKey("agents.id", ondelete="SET NULL"),
            nullable=True,
            comment="安装出来的Agent ID",
        ),
        sa.Column(
            "target_skill_id",
            sa.Integer(),
            sa.ForeignKey("skills.id", ondelete="SET NULL"),
            nullable=True,
            comment="安装出来的Skill ID",
        ),
        sa.Column(
            "managed_skill_ids",
            postgresql.JSONB(),
            nullable=False,
            comment="安装Agent时新建或恢复的Skill ID，升级时只覆盖这些Skill",
        ),
        sa.Column(
            "installed_by_user_id",
            sa.Integer(),
            sa.ForeignKey("users.id", ondelete="RESTRICT"),
            nullable=False,
            comment="安装人用户ID",
        ),
        *_audit_columns(),
        comment="市场安装记录",
    )
    for column in (
        "listing_id",
        "workspace_id",
        "target_agent_id",
        "target_skill_id",
        "installed_by_user_id",
        "is_deleted",
    ):
        op.create_index(f"ix_market_installs_{column}", "market_installs", [column])
    # 同一工作区对同一条目只有一条有效安装记录。
    op.create_index(
        "uq_market_installs_active_listing_workspace",
        "market_installs",
        ["listing_id", "workspace_id"],
        unique=True,
        postgresql_where=sa.text("is_deleted = false"),
    )


def downgrade() -> None:
    # 按外键依赖倒序删表，表上的索引和约束随表一起删除。
    op.drop_table("market_installs")
    op.drop_table("market_versions")
    op.drop_table("market_listings")
    op.drop_column("users", "is_platform_admin")
