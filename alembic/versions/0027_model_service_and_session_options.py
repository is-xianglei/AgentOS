"""新增工作区模型服务、角色及会话模型策略。

Revision ID: 0027_model_service_options
Revises: 0026_permission_workspace_scope
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "0027_model_service_options"
down_revision = "0026_permission_workspace_scope"
branch_labels = None
depends_on = None


def _audit_columns() -> list[sa.Column]:
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
            comment="软删除标记",
        ),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True, comment="软删除时间"),
    ]


def upgrade() -> None:
    op.create_table(
        "model_providers",
        sa.Column("provider_id", sa.String(36), primary_key=True, comment="供应商ID"),
        sa.Column(
            "workspace_id",
            sa.Integer(),
            sa.ForeignKey("workspaces.id", ondelete="CASCADE"),
            nullable=False,
            comment="所属工作区ID",
        ),
        sa.Column("display_name", sa.String(100), nullable=False, comment="显示名称"),
        sa.Column("provider_type", sa.String(20), nullable=False, comment="用途类型"),
        sa.Column("provider", sa.String(40), nullable=False, comment="上游协议或厂商"),
        sa.Column("base_url", sa.Text(), nullable=False, comment="上游服务地址"),
        sa.Column("api_key", sa.Text(), nullable=False, comment="上游密钥，仅服务端使用"),
        sa.Column("model_name", sa.String(200), nullable=False, comment="上游模型ID"),
        sa.Column("extra_config", postgresql.JSONB(), nullable=False, comment="能力与参数映射配置"),
        sa.Column("is_active", sa.Boolean(), nullable=False, comment="是否启用"),
        sa.Column("last_tested_at", sa.DateTime(timezone=True), nullable=True, comment="最近测试时间"),
        sa.Column("last_test_status", sa.String(20), nullable=True, comment="最近测试结果"),
        *_audit_columns(),
        comment="工作区模型服务供应商",
    )
    op.create_table(
        "model_roles",
        sa.Column("id", sa.Integer(), primary_key=True, comment="指派ID"),
        sa.Column(
            "workspace_id",
            sa.Integer(),
            sa.ForeignKey("workspaces.id", ondelete="CASCADE"),
            nullable=False,
            comment="所属工作区ID",
        ),
        sa.Column(
            "role_key", sa.String(40), nullable=False, comment="场景角色"
        ),
        sa.Column(
            "provider_id",
            sa.String(36),
            sa.ForeignKey("model_providers.provider_id", ondelete="RESTRICT"),
            nullable=False,
            comment="供应商ID",
        ),
        *_audit_columns(),
        sa.UniqueConstraint("workspace_id", "role_key", name="uq_model_roles_workspace_role"),
        comment="模型场景角色指派",
    )
    for table in ("model_providers", "model_roles"):
        for column in ("workspace_id", "is_deleted"):
            op.create_index(f"ix_{table}_{column}", table, [column])
    op.create_index("ix_model_roles_provider_id", "model_roles", ["provider_id"])
    op.add_column(
        "sessions",
        sa.Column(
            "model_provider_id",
            sa.String(36),
            nullable=True,
            comment="会话选择的模型供应商",
        ),
    )
    op.create_foreign_key(
        "fk_sessions_model_provider_id",
        "sessions",
        "model_providers",
        ["model_provider_id"],
        ["provider_id"],
        ondelete="SET NULL",
    )
    op.add_column(
        "sessions",
        sa.Column(
            "mode_slug",
            sa.String(20),
            nullable=False,
            server_default="standard",
            comment="对话模式",
        ),
    )
    op.add_column(
        "sessions",
        sa.Column(
            "thinking_effort",
            sa.String(20),
            nullable=False,
            server_default="fast",
            comment="思考强度",
        ),
    )


def downgrade() -> None:
    op.drop_column("sessions", "thinking_effort")
    op.drop_column("sessions", "mode_slug")
    op.drop_constraint("fk_sessions_model_provider_id", "sessions", type_="foreignkey")
    op.drop_column("sessions", "model_provider_id")
    op.drop_table("model_roles")
    op.drop_table("model_providers")
