"""新增工作区成员角色、部门、群组与会话共享范围。

Revision ID: 0017_workspace_org_units
Revises: 0016_interaction_plan_mode
Create Date: 2026-08-01
"""

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "0017_workspace_org_units"
down_revision = "0016_interaction_plan_mode"
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
        "departments",
        sa.Column("id", sa.Integer(), nullable=False, comment="部门ID"),
        sa.Column("workspace_id", sa.Integer(), nullable=False, comment="所属工作区ID"),
        sa.Column(
            "parent_id",
            sa.Integer(),
            nullable=True,
            comment="父部门ID（NULL表示顶级部门）",
        ),
        sa.Column("name", sa.String(length=128), nullable=False, comment="部门名称"),
        sa.Column(
            "code",
            sa.String(length=32),
            nullable=True,
            comment="部门编码（用于对接HR系统）",
        ),
        sa.Column("description", sa.Text(), nullable=True, comment="部门描述"),
        sa.Column(
            "manager_id",
            sa.Integer(),
            nullable=True,
            comment="部门负责人用户ID",
        ),
        sa.Column("sort_order", sa.Integer(), nullable=False, comment="排序顺序"),
        sa.Column("is_active", sa.Boolean(), nullable=False, comment="是否启用"),
        *_audit_columns(),
        sa.CheckConstraint(
            "parent_id IS NULL OR parent_id <> id",
            name="ck_departments_parent_not_self",
        ),
        sa.ForeignKeyConstraint(
            ["manager_id"],
            ["users.id"],
            name="fk_departments_manager_id",
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["parent_id"],
            ["departments.id"],
            name="fk_departments_parent_id",
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id"],
            ["workspaces.id"],
            name="fk_departments_workspace_id",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_departments"),
        comment="部门表（支持层级结构）",
    )
    op.create_index("ix_departments_workspace_id", "departments", ["workspace_id"])
    op.create_index("ix_departments_parent_id", "departments", ["parent_id"])
    op.create_index("ix_departments_manager_id", "departments", ["manager_id"])
    op.create_index(
        "ix_departments_workspace_parent",
        "departments",
        ["workspace_id", "parent_id"],
    )
    op.create_index("ix_departments_is_deleted", "departments", ["is_deleted"])

    op.add_column(
        "workspace_members",
        sa.Column(
            "role",
            sa.String(length=32),
            server_default=sa.text("'member'"),
            nullable=False,
            comment="工作区角色: owner/admin/member",
        ),
    )
    op.add_column(
        "workspace_members",
        sa.Column(
            "department_id",
            sa.Integer(),
            nullable=True,
            comment="所属部门ID（一个成员只能属于一个部门）",
        ),
    )
    op.add_column(
        "workspace_members",
        sa.Column(
            "job_title",
            sa.String(length=128),
            nullable=True,
            comment="职位/岗位",
        ),
    )
    op.create_check_constraint(
        "ck_workspace_members_role",
        "workspace_members",
        "role IN ('owner', 'admin', 'member')",
    )
    op.create_foreign_key(
        "fk_workspace_members_department_id",
        "workspace_members",
        "departments",
        ["department_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.create_index("ix_workspace_members_role", "workspace_members", ["role"])
    op.create_index(
        "ix_workspace_members_department_id",
        "workspace_members",
        ["department_id"],
    )

    # 历史数据没有创建者字段。每个工作区最早的有效成员作为唯一初始 owner，
    # 其余成员保持 member，保证已有正式成员的工作区至少存在一个管理主体。
    op.execute(
        """
        WITH ranked_members AS (
            SELECT id,
                   row_number() OVER (
                       PARTITION BY workspace_id
                       ORDER BY joined_at NULLS LAST, created_at, id
                   ) AS member_rank
            FROM workspace_members
            WHERE is_deleted = false AND joined_at IS NOT NULL
        )
        UPDATE workspace_members AS member
        SET role = 'owner'
        FROM ranked_members AS ranked
        WHERE member.id = ranked.id AND ranked.member_rank = 1
        """
    )

    op.create_table(
        "groups",
        sa.Column("id", sa.Integer(), nullable=False, comment="群组ID"),
        sa.Column("workspace_id", sa.Integer(), nullable=False, comment="所属工作区ID"),
        sa.Column("name", sa.String(length=128), nullable=False, comment="群组名称"),
        sa.Column("slug", sa.String(length=64), nullable=False, comment="群组标识（URL友好）"),
        sa.Column("description", sa.Text(), nullable=True, comment="群组描述"),
        sa.Column(
            "avatar_url",
            sa.String(length=512),
            nullable=True,
            comment="群组头像URL",
        ),
        sa.Column(
            "group_type",
            sa.String(length=32),
            nullable=False,
            comment="群组类型: team/community/region/custom",
        ),
        sa.Column("created_by", sa.Integer(), nullable=False, comment="创建者用户ID"),
        sa.Column("owner_id", sa.Integer(), nullable=False, comment="负责人用户ID"),
        sa.Column(
            "visibility",
            sa.String(length=32),
            nullable=False,
            comment="可见性: workspace/private/public",
        ),
        sa.Column(
            "join_mode",
            sa.String(length=32),
            nullable=False,
            comment="加入方式: invite/open/approval",
        ),
        sa.Column(
            "settings",
            postgresql.JSONB(),
            nullable=False,
            comment="群组设置",
        ),
        sa.Column("is_active", sa.Boolean(), nullable=False, comment="是否启用"),
        *_audit_columns(),
        sa.CheckConstraint(
            "group_type IN ('team', 'community', 'region', 'custom')",
            name="ck_groups_type",
        ),
        sa.CheckConstraint(
            "visibility IN ('workspace', 'private', 'public')",
            name="ck_groups_visibility",
        ),
        sa.CheckConstraint(
            "join_mode IN ('invite', 'open', 'approval')",
            name="ck_groups_join_mode",
        ),
        sa.ForeignKeyConstraint(
            ["created_by"],
            ["users.id"],
            name="fk_groups_created_by",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["owner_id"],
            ["users.id"],
            name="fk_groups_owner_id",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id"],
            ["workspaces.id"],
            name="fk_groups_workspace_id",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_groups"),
        comment="群组表（扁平结构，用于灵活协作）",
    )
    op.create_index("ix_groups_workspace_id", "groups", ["workspace_id"])
    op.create_index("ix_groups_visibility", "groups", ["visibility"])
    op.create_index(
        "ix_groups_workspace_type",
        "groups",
        ["workspace_id", "group_type"],
    )
    op.create_index(
        "uq_groups_active_workspace_slug",
        "groups",
        ["workspace_id", "slug"],
        unique=True,
        postgresql_where=sa.text("is_deleted = false"),
    )
    op.create_index("ix_groups_is_deleted", "groups", ["is_deleted"])

    op.create_table(
        "group_members",
        sa.Column("id", sa.Integer(), nullable=False, comment="群组成员记录ID"),
        sa.Column("group_id", sa.Integer(), nullable=False, comment="群组ID"),
        sa.Column("user_id", sa.Integer(), nullable=False, comment="用户ID"),
        sa.Column(
            "role",
            sa.String(length=32),
            nullable=False,
            comment="群组内角色: owner/admin/member",
        ),
        sa.Column("invited_by", sa.Integer(), nullable=True, comment="邀请人用户ID"),
        sa.Column(
            "joined_at",
            sa.DateTime(timezone=True),
            nullable=True,
            comment="加入时间（NULL表示等待审批）",
        ),
        *_audit_columns(),
        sa.CheckConstraint(
            "role IN ('owner', 'admin', 'member')",
            name="ck_group_members_role",
        ),
        sa.ForeignKeyConstraint(
            ["group_id"],
            ["groups.id"],
            name="fk_group_members_group_id",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["invited_by"],
            ["users.id"],
            name="fk_group_members_invited_by",
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["users.id"],
            name="fk_group_members_user_id",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_group_members"),
        comment="群组成员关系表",
    )
    op.create_index("ix_group_members_group_id", "group_members", ["group_id"])
    op.create_index("ix_group_members_user_id", "group_members", ["user_id"])
    op.create_index(
        "ix_group_members_group_user",
        "group_members",
        ["group_id", "user_id"],
        unique=True,
        postgresql_where=sa.text("is_deleted = false"),
    )
    op.create_index("ix_group_members_is_deleted", "group_members", ["is_deleted"])

    op.add_column(
        "sessions",
        sa.Column(
            "shared_with_departments",
            postgresql.JSONB(),
            server_default=sa.text("'[]'::jsonb"),
            nullable=False,
            comment="共享部门ID列表（访问时动态包含子部门）",
        ),
    )
    op.add_column(
        "sessions",
        sa.Column(
            "shared_with_groups",
            postgresql.JSONB(),
            server_default=sa.text("'[]'::jsonb"),
            nullable=False,
            comment="共享群组ID列表",
        ),
    )
    op.alter_column(
        "sessions",
        "visibility",
        existing_type=sa.String(length=32),
        comment="可见性: private/workspace/public",
        existing_comment="可见性: private/team/workspace/public",
    )
    op.create_index(
        "ix_sessions_shared_with_gin",
        "sessions",
        ["shared_with"],
        postgresql_using="gin",
    )
    op.create_index(
        "ix_sessions_shared_with_departments_gin",
        "sessions",
        ["shared_with_departments"],
        postgresql_using="gin",
    )
    op.create_index(
        "ix_sessions_shared_with_groups_gin",
        "sessions",
        ["shared_with_groups"],
        postgresql_using="gin",
    )


def downgrade() -> None:
    op.drop_index("ix_sessions_shared_with_groups_gin", table_name="sessions")
    op.drop_index("ix_sessions_shared_with_departments_gin", table_name="sessions")
    op.drop_index("ix_sessions_shared_with_gin", table_name="sessions")
    op.alter_column(
        "sessions",
        "visibility",
        existing_type=sa.String(length=32),
        comment="可见性: private/team/workspace/public",
        existing_comment="可见性: private/workspace/public",
    )
    op.drop_column("sessions", "shared_with_groups")
    op.drop_column("sessions", "shared_with_departments")

    op.drop_table("group_members")
    op.drop_table("groups")

    op.drop_index("ix_workspace_members_department_id", table_name="workspace_members")
    op.drop_index("ix_workspace_members_role", table_name="workspace_members")
    op.drop_constraint(
        "fk_workspace_members_department_id",
        "workspace_members",
        type_="foreignkey",
    )
    op.drop_constraint(
        "ck_workspace_members_role",
        "workspace_members",
        type_="check",
    )
    op.drop_column("workspace_members", "job_title")
    op.drop_column("workspace_members", "department_id")
    op.drop_column("workspace_members", "role")

    op.drop_table("departments")
