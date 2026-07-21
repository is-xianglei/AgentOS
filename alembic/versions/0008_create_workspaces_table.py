"""create_workspaces_table

Revision ID: 0008_create_workspaces
Revises: d5245f5576f8
Create Date: 2026-07-20 16:00:00.000000
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = '0008_create_workspaces'
down_revision = 'd5245f5576f8'
branch_labels = None
depends_on = None


def upgrade() -> None:
    # 创建 workspaces 表
    op.create_table(
        'workspaces',
        sa.Column('id', sa.Integer(), nullable=False, comment='工作区ID'),
        sa.Column('name', sa.String(length=255), nullable=False, comment='工作区名称(唯一)'),
        sa.Column('slug', sa.String(length=64), nullable=False, comment='工作区标识(URL友好)'),
        sa.Column('display_name', sa.String(length=255), nullable=False, comment='显示名称'),
        sa.Column('logo_url', sa.String(length=512), nullable=True, comment='工作区Logo URL'),
        sa.Column('workspace_type', sa.String(length=32), nullable=False, comment='类型: personal/team/enterprise'),
        sa.Column('suspended', sa.Boolean(), nullable=False, comment='是否被暂停使用'),
        sa.Column('industry', sa.String(length=64), nullable=True, comment='所属行业'),
        sa.Column('company_size', sa.String(length=32), nullable=True, comment='公司规模'),
        sa.Column('billing_email', sa.String(length=255), nullable=True, comment='账单邮箱'),
        sa.Column('plan', sa.String(length=32), nullable=False, comment='订阅计划: free/pro/enterprise'),
        sa.Column('quotas', postgresql.JSONB(astext_type=sa.Text()), nullable=False, comment='配额配置 {sessions_per_month: 100}'),
        sa.Column('settings', postgresql.JSONB(astext_type=sa.Text()), nullable=False, comment='工作区设置'),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False, comment='创建时间'),
        sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False, comment='更新时间'),
        sa.Column('is_deleted', sa.Boolean(), server_default=sa.text('false'), nullable=False, comment='软删除标记: false 有效 / true 已删除'),
        sa.Column('deleted_at', sa.DateTime(timezone=True), nullable=True, comment='软删除时间, 空表示未删除'),
        sa.PrimaryKeyConstraint('id'),
        comment='工作区表（个人/团队/企业）'
    )

    # 创建索引
    op.create_index(op.f('ix_workspaces_name'), 'workspaces', ['name'], unique=True)
    op.create_index(op.f('ix_workspaces_slug'), 'workspaces', ['slug'], unique=True)
    op.create_index(op.f('ix_workspaces_workspace_type'), 'workspaces', ['workspace_type'], unique=False)
    op.create_index(op.f('ix_workspaces_suspended'), 'workspaces', ['suspended'], unique=False)
    op.create_index(op.f('ix_workspaces_is_deleted'), 'workspaces', ['is_deleted'], unique=False)


def downgrade() -> None:
    # 删除索引
    op.drop_index(op.f('ix_workspaces_is_deleted'), table_name='workspaces')
    op.drop_index(op.f('ix_workspaces_suspended'), table_name='workspaces')
    op.drop_index(op.f('ix_workspaces_workspace_type'), table_name='workspaces')
    op.drop_index(op.f('ix_workspaces_slug'), table_name='workspaces')
    op.drop_index(op.f('ix_workspaces_name'), table_name='workspaces')

    # 删除表
    op.drop_table('workspaces')
