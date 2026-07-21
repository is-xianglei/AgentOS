"""create_workspace_members_table

Revision ID: 0009_create_workspace_members
Revises: 0008_create_workspaces
Create Date: 2026-07-20 16:01:00.000000
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = '0009_workspace_members'
down_revision = '0008_create_workspaces'
branch_labels = None
depends_on = None


def upgrade() -> None:
    # 创建 workspace_members 表
    op.create_table(
        'workspace_members',
        sa.Column('id', sa.Integer(), nullable=False, comment='成员记录ID'),
        sa.Column('workspace_id', sa.Integer(), nullable=False, comment='工作区ID'),
        sa.Column('user_id', sa.Integer(), nullable=False, comment='用户ID'),
        sa.Column('invited_by', sa.Integer(), nullable=True, comment='邀请人用户ID'),
        sa.Column('invited_at', sa.DateTime(timezone=True), nullable=True, comment='邀请时间'),
        sa.Column('joined_at', sa.DateTime(timezone=True), nullable=True, comment='加入时间(接受邀请的时间)'),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False, comment='创建时间'),
        sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False, comment='更新时间'),
        sa.Column('is_deleted', sa.Boolean(), server_default=sa.text('false'), nullable=False, comment='软删除标记: false 有效 / true 已删除'),
        sa.Column('deleted_at', sa.DateTime(timezone=True), nullable=True, comment='软删除时间, 空表示未删除'),
        sa.ForeignKeyConstraint(['workspace_id'], ['workspaces.id'], ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['user_id'], ['users.id'], ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['invited_by'], ['users.id']),
        sa.PrimaryKeyConstraint('id'),
        comment='工作区成员关系表'
    )

    # 创建索引
    op.create_index(op.f('ix_workspace_members_workspace_id'), 'workspace_members', ['workspace_id'], unique=False)
    op.create_index(op.f('ix_workspace_members_user_id'), 'workspace_members', ['user_id'], unique=False)
    op.create_index(op.f('ix_workspace_members_is_deleted'), 'workspace_members', ['is_deleted'], unique=False)

    # 创建复合唯一索引
    op.create_index(
        'ix_workspace_members_workspace_user',
        'workspace_members',
        ['workspace_id', 'user_id'],
        unique=True
    )


def downgrade() -> None:
    # 删除索引
    op.drop_index('ix_workspace_members_workspace_user', table_name='workspace_members')
    op.drop_index(op.f('ix_workspace_members_is_deleted'), table_name='workspace_members')
    op.drop_index(op.f('ix_workspace_members_user_id'), table_name='workspace_members')
    op.drop_index(op.f('ix_workspace_members_workspace_id'), table_name='workspace_members')

    # 删除表
    op.drop_table('workspace_members')
