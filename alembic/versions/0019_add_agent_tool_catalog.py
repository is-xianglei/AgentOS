"""新增Agent控制面、内置工具目录和工作区Skill隔离。

Revision ID: 0019_agent_tool_catalog
Revises: 0018_alembic_history
Create Date: 2026-08-04
"""

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "0019_agent_tool_catalog"
down_revision = "0018_alembic_history"
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
        "tools",
        sa.Column("id", sa.Integer(), primary_key=True, comment="工具ID"),
        sa.Column("tool_key", sa.String(length=120), nullable=False, comment="工具稳定标识"),
        sa.Column("name", sa.String(length=120), nullable=False, comment="工具名称"),
        sa.Column("description", sa.Text(), nullable=False, comment="工具描述"),
        sa.Column(
            "tool_type",
            sa.String(length=32),
            server_default="builtin",
            nullable=False,
            comment="工具类型，MVP仅支持builtin",
        ),
        sa.Column(
            "runtime_name",
            sa.String(length=120),
            nullable=False,
            comment="代码注册表名称",
        ),
        sa.Column(
            "input_schema",
            postgresql.JSONB(),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
            comment="工具输入JSON Schema",
        ),
        sa.Column(
            "implementation_config",
            postgresql.JSONB(),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
            comment="工具实现配置，内置工具为空",
        ),
        sa.Column(
            "is_system",
            sa.Boolean(),
            server_default=sa.true(),
            nullable=False,
            comment="是否系统内置工具",
        ),
        sa.Column(
            "is_enabled",
            sa.Boolean(),
            server_default=sa.true(),
            nullable=False,
            comment="是否启用",
        ),
        *_audit_columns(),
        comment="内置工具目录",
    )
    op.create_index("ix_tools_tool_key", "tools", ["tool_key"], unique=True)
    op.create_index("ix_tools_runtime_name", "tools", ["runtime_name"], unique=True)
    op.create_index("ix_tools_tool_type", "tools", ["tool_type"])
    op.create_index("ix_tools_is_enabled", "tools", ["is_enabled"])
    op.create_index("ix_tools_is_deleted", "tools", ["is_deleted"])

    op.add_column(
        "skills",
        sa.Column("workspace_id", sa.Integer(), nullable=True, comment="所属工作区ID"),
    )
    op.execute(
        """
        DO $$
        BEGIN
            IF EXISTS (SELECT 1 FROM skills)
               AND NOT EXISTS (
                   SELECT 1 FROM workspaces WHERE is_deleted = false
               ) THEN
                RAISE EXCEPTION '存在历史Skill但没有可用于归属的工作区';
            END IF;
        END $$
        """
    )
    op.drop_index("ix_skills_name", table_name="skills")
    op.execute(
        """
        INSERT INTO skills (
            name,
            description,
            frontmatter,
            version,
            skill_hash,
            scope,
            created_at,
            updated_at,
            is_deleted,
            deleted_at,
            workspace_id
        )
        SELECT
            skill.name,
            skill.description,
            skill.frontmatter,
            skill.version,
            skill.skill_hash,
            'workspace',
            skill.created_at,
            skill.updated_at,
            skill.is_deleted,
            skill.deleted_at,
            workspace.id
        FROM skills AS skill
        CROSS JOIN workspaces AS workspace
        WHERE skill.workspace_id IS NULL
          AND workspace.is_deleted = false
          AND workspace.id <> (
              SELECT id
              FROM workspaces
              WHERE is_deleted = false
              ORDER BY id
              LIMIT 1
          );

        UPDATE skills
        SET workspace_id = (
                SELECT id
                FROM workspaces
                WHERE is_deleted = false
                ORDER BY id
                LIMIT 1
            ),
            scope = 'workspace'
        WHERE workspace_id IS NULL;
        """
    )
    op.alter_column("skills", "workspace_id", nullable=False)
    op.create_foreign_key(
        "fk_skills_workspace_id_workspaces",
        "skills",
        "workspaces",
        ["workspace_id"],
        ["id"],
        ondelete="CASCADE",
    )
    op.create_index("ix_skills_name", "skills", ["name"])
    op.create_index("ix_skills_workspace_id", "skills", ["workspace_id"])
    op.create_unique_constraint(
        "uq_skills_workspace_name",
        "skills",
        ["workspace_id", "name"],
    )
    op.alter_column(
        "skills",
        "scope",
        server_default="workspace",
        comment="作用域，MVP恒为workspace",
    )
    op.create_table(
        "agents",
        sa.Column("id", sa.Integer(), primary_key=True, comment="Agent ID"),
        sa.Column("workspace_id", sa.Integer(), nullable=False, comment="所属工作区ID"),
        sa.Column("name", sa.String(length=128), nullable=False, comment="Agent名称"),
        sa.Column("description", sa.Text(), nullable=True, comment="Agent描述"),
        sa.Column("system_prompt", sa.Text(), nullable=False, comment="系统提示词"),
        sa.Column(
            "model_name",
            sa.String(length=128),
            nullable=True,
            comment="模型名称，空表示使用系统默认模型",
        ),
        sa.Column(
            "is_enabled",
            sa.Boolean(),
            server_default=sa.true(),
            nullable=False,
            comment="是否启用",
        ),
        sa.Column("created_by_user_id", sa.Integer(), nullable=False, comment="创建者用户ID"),
        *_audit_columns(),
        sa.ForeignKeyConstraint(
            ["workspace_id"],
            ["workspaces.id"],
            name="fk_agents_workspace_id_workspaces",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["created_by_user_id"],
            ["users.id"],
            name="fk_agents_created_by_user_id_users",
            ondelete="RESTRICT",
        ),
        comment="自定义Agent定义表",
    )
    op.create_index("ix_agents_workspace_id", "agents", ["workspace_id"])
    op.create_index("ix_agents_is_enabled", "agents", ["is_enabled"])
    op.create_index("ix_agents_created_by_user_id", "agents", ["created_by_user_id"])
    op.create_index("ix_agents_is_deleted", "agents", ["is_deleted"])
    op.create_index(
        "ix_agents_workspace_enabled",
        "agents",
        ["workspace_id", "is_enabled"],
    )
    op.create_index(
        "uq_agents_active_workspace_name",
        "agents",
        ["workspace_id", "name"],
        unique=True,
        postgresql_where=sa.text("is_deleted = false"),
    )

    op.create_table(
        "agent_tools",
        sa.Column("id", sa.Integer(), primary_key=True, comment="绑定ID"),
        sa.Column("agent_id", sa.Integer(), nullable=False, comment="Agent ID"),
        sa.Column("tool_id", sa.Integer(), nullable=False, comment="工具ID"),
        *_audit_columns(),
        sa.ForeignKeyConstraint(
            ["agent_id"],
            ["agents.id"],
            name="fk_agent_tools_agent_id_agents",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["tool_id"],
            ["tools.id"],
            name="fk_agent_tools_tool_id_tools",
            ondelete="RESTRICT",
        ),
        sa.UniqueConstraint("agent_id", "tool_id", name="uq_agent_tools_agent_tool"),
        comment="Agent工具绑定表",
    )
    op.create_index("ix_agent_tools_agent_id", "agent_tools", ["agent_id"])
    op.create_index("ix_agent_tools_tool_id", "agent_tools", ["tool_id"])
    op.create_index("ix_agent_tools_is_deleted", "agent_tools", ["is_deleted"])

    op.create_table(
        "agent_skills",
        sa.Column("id", sa.Integer(), primary_key=True, comment="绑定ID"),
        sa.Column("agent_id", sa.Integer(), nullable=False, comment="Agent ID"),
        sa.Column("skill_id", sa.Integer(), nullable=False, comment="Skill ID"),
        *_audit_columns(),
        sa.ForeignKeyConstraint(
            ["agent_id"],
            ["agents.id"],
            name="fk_agent_skills_agent_id_agents",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["skill_id"],
            ["skills.id"],
            name="fk_agent_skills_skill_id_skills",
            ondelete="RESTRICT",
        ),
        sa.UniqueConstraint("agent_id", "skill_id", name="uq_agent_skills_agent_skill"),
        comment="Agent Skill绑定表",
    )
    op.create_index("ix_agent_skills_agent_id", "agent_skills", ["agent_id"])
    op.create_index("ix_agent_skills_skill_id", "agent_skills", ["skill_id"])
    op.create_index("ix_agent_skills_is_deleted", "agent_skills", ["is_deleted"])

    op.add_column(
        "sessions",
        sa.Column(
            "agent_id",
            sa.Integer(),
            nullable=True,
            comment="会话绑定的Agent ID，空表示使用默认Agent",
        ),
    )
    op.create_foreign_key(
        "fk_sessions_agent_id_agents",
        "sessions",
        "agents",
        ["agent_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.create_index("ix_sessions_agent_id", "sessions", ["agent_id"])

    op.add_column(
        "tool_calls",
        sa.Column("tool_id", sa.Integer(), nullable=True, comment="调用的工具ID"),
    )
    op.add_column(
        "tool_calls",
        sa.Column("agent_id", sa.Integer(), nullable=True, comment="发起调用的Agent ID"),
    )
    op.create_foreign_key(
        "fk_tool_calls_tool_id_tools",
        "tool_calls",
        "tools",
        ["tool_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.create_foreign_key(
        "fk_tool_calls_agent_id_agents",
        "tool_calls",
        "agents",
        ["agent_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.create_index("ix_tool_calls_tool_id", "tool_calls", ["tool_id"])
    op.create_index("ix_tool_calls_agent_id", "tool_calls", ["agent_id"])


def downgrade() -> None:
    op.drop_index("ix_tool_calls_agent_id", table_name="tool_calls")
    op.drop_index("ix_tool_calls_tool_id", table_name="tool_calls")
    op.drop_constraint("fk_tool_calls_agent_id_agents", "tool_calls", type_="foreignkey")
    op.drop_constraint("fk_tool_calls_tool_id_tools", "tool_calls", type_="foreignkey")
    op.drop_column("tool_calls", "agent_id")
    op.drop_column("tool_calls", "tool_id")

    op.drop_index("ix_sessions_agent_id", table_name="sessions")
    op.drop_constraint("fk_sessions_agent_id_agents", "sessions", type_="foreignkey")
    op.drop_column("sessions", "agent_id")

    op.drop_table("agent_skills")
    op.drop_table("agent_tools")
    op.drop_table("agents")

    op.execute(
        """
        DO $$
        BEGIN
            IF EXISTS (
                SELECT 1
                FROM skills
                GROUP BY name
                HAVING count(*) > 1
                   AND count(
                       DISTINCT ROW(
                           description,
                           frontmatter,
                           version,
                           skill_hash,
                           is_deleted,
                           deleted_at
                       )
                   ) > 1
            ) THEN
                RAISE EXCEPTION '跨工作区同名Skill已发生分化，无法安全降级';
            END IF;
        END $$
        """
    )
    op.execute(
        """
        DELETE FROM skills
        WHERE id NOT IN (
            SELECT min(id)
            FROM skills
            GROUP BY name
        )
        """
    )
    op.drop_constraint("uq_skills_workspace_name", "skills", type_="unique")
    op.drop_index("ix_skills_workspace_id", table_name="skills")
    op.drop_index("ix_skills_name", table_name="skills")
    op.create_index("ix_skills_name", "skills", ["name"], unique=True)
    op.drop_constraint("fk_skills_workspace_id_workspaces", "skills", type_="foreignkey")
    op.drop_column("skills", "workspace_id")
    op.execute("UPDATE skills SET scope = 'global'")
    op.alter_column(
        "skills",
        "scope",
        server_default="global",
        comment="作用域:预留隔离维度,本期恒为global",
    )

    op.drop_table("tools")
