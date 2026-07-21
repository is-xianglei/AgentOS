"""add_user_workspace_to_sessions

Revision ID: 0010_add_user_workspace
Revises: 0009_workspace_members
Create Date: 2026-07-20 17:00:00.000000
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = '0010_add_user_workspace'
down_revision = '0009_workspace_members'
branch_labels = None
depends_on = None


def upgrade() -> None:
    # 添加 user_id 字段
    op.add_column(
        'sessions',
        sa.Column('user_id', sa.Integer(), nullable=True, comment='创建用户ID')
    )

    # 添加 workspace_id 字段
    op.add_column(
        'sessions',
        sa.Column('workspace_id', sa.Integer(), nullable=True, comment='所属工作区ID')
    )

    # 添加 visibility 字段
    op.add_column(
        'sessions',
        sa.Column(
            'visibility',
            sa.String(length=32),
            nullable=False,
            server_default='private',
            comment='可见性: private/team/workspace/public'
        )
    )

    # 添加 shared_with 字段
    op.add_column(
        'sessions',
        sa.Column(
            'shared_with',
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default='[]',
            comment='共享用户ID列表'
        )
    )

    # 创建外键约束
    op.create_foreign_key(
        'fk_sessions_user_id',
        'sessions',
        'users',
        ['user_id'],
        ['id'],
        ondelete='CASCADE'
    )

    op.create_foreign_key(
        'fk_sessions_workspace_id',
        'sessions',
        'workspaces',
        ['workspace_id'],
        ['id'],
        ondelete='CASCADE'
    )

    # 创建索引
    op.create_index(op.f('ix_sessions_user_id'), 'sessions', ['user_id'], unique=False)
    op.create_index(op.f('ix_sessions_workspace_id'), 'sessions', ['workspace_id'], unique=False)
    op.create_index(op.f('ix_sessions_visibility'), 'sessions', ['visibility'], unique=False)


def downgrade() -> None:
    # 删除索引
    op.drop_index(op.f('ix_sessions_visibility'), table_name='sessions')
    op.drop_index(op.f('ix_sessions_workspace_id'), table_name='sessions')
    op.drop_index(op.f('ix_sessions_user_id'), table_name='sessions')

    # 删除外键约束
    op.drop_constraint('fk_sessions_workspace_id', 'sessions', type_='foreignkey')
    op.drop_constraint('fk_sessions_user_id', 'sessions', type_='foreignkey')

    # 删除列
    op.drop_column('sessions', 'shared_with')
    op.drop_column('sessions', 'visibility')
    op.drop_column('sessions', 'workspace_id')
    op.drop_column('sessions', 'user_id')
