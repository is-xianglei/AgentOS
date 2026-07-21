"""fix_users_table_schema

Revision ID: 0011_fix_users_table_schema
Revises: 0010_add_user_workspace_to_sessions
Create Date: 2026-07-20 17:00:00.000000

修正 users 表字段：
1. 将 password_hash 重命名为 password
2. 删除 status 字段
3. 删除 bio 字段
4. 删除 extra_data 字段
5. 添加 phone 字段
6. 添加 email_verified 字段
7. 添加 suspended 字段
8. 添加 auth_provider 字段
9. 添加 provider_user_id 字段
10. 添加 preferences 字段
11. 添加 last_login_at 字段
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = '0011_fix_users_table_schema'
down_revision = '0010_add_user_workspace'
branch_labels = None
depends_on = None


def upgrade() -> None:
    # 1. 重命名 password_hash 为 password
    op.alter_column('users', 'password_hash', new_column_name='password')

    # 2. 删除不需要的字段
    op.drop_index('ix_users_status', table_name='users')
    op.drop_column('users', 'status')
    op.drop_column('users', 'bio')
    op.drop_column('users', 'extra_data')

    # 3. 添加新字段
    op.add_column('users', sa.Column('phone', sa.String(length=32), nullable=True, comment='手机号'))
    op.add_column('users', sa.Column('email_verified', sa.Boolean(), server_default=sa.text('false'), nullable=False, comment='邮箱是否验证'))
    op.add_column('users', sa.Column('suspended', sa.Boolean(), server_default=sa.text('false'), nullable=False, comment='是否被暂停使用'))
    op.add_column('users', sa.Column('auth_provider', sa.String(length=32), server_default='local', nullable=False, comment='认证提供商: local/google/github/saml'))
    op.add_column('users', sa.Column('provider_user_id', sa.String(length=255), nullable=True, comment='第三方用户ID'))
    op.add_column('users', sa.Column('preferences', postgresql.JSONB(astext_type=sa.Text()), server_default='{}', nullable=False, comment='用户偏好设置'))
    op.add_column('users', sa.Column('last_login_at', sa.DateTime(timezone=True), nullable=True, comment='最后登录时间'))

    # 4. 创建新索引
    op.create_index('ix_users_suspended', 'users', ['suspended'])
    op.create_index('ix_users_auth_provider_provider_user_id', 'users', ['auth_provider', 'provider_user_id'])

    # 5. 更新注释
    op.alter_column('users', 'email', existing_type=sa.String(255), comment='邮箱(唯一登录标识)')
    op.alter_column('users', 'password', existing_type=sa.String(255), comment='密码哈希(MD5)')
    op.alter_column('users', 'full_name', existing_type=sa.String(128), comment='真实姓名')


def downgrade() -> None:
    # 1. 删除新索引
    op.drop_index('ix_users_auth_provider_provider_user_id', table_name='users')
    op.drop_index('ix_users_suspended', table_name='users')

    # 2. 删除新字段
    op.drop_column('users', 'last_login_at')
    op.drop_column('users', 'preferences')
    op.drop_column('users', 'provider_user_id')
    op.drop_column('users', 'auth_provider')
    op.drop_column('users', 'suspended')
    op.drop_column('users', 'email_verified')
    op.drop_column('users', 'phone')

    # 3. 恢复旧字段
    op.add_column('users', sa.Column('extra_data', postgresql.JSONB(astext_type=sa.Text()), server_default='{}', nullable=False, comment='扩展元数据'))
    op.add_column('users', sa.Column('bio', sa.Text(), nullable=True, comment='个人简介'))
    op.add_column('users', sa.Column('status', sa.String(length=32), server_default='active', nullable=False, comment='用户状态'))
    op.create_index('ix_users_status', 'users', ['status'])

    # 4. 重命名 password 为 password_hash
    op.alter_column('users', 'password', new_column_name='password_hash')

    # 5. 恢复旧注释
    op.alter_column('users', 'email', existing_type=sa.String(255), comment='邮箱(唯一)')
    op.alter_column('users', 'password_hash', existing_type=sa.String(255), comment='密码哈希')
    op.alter_column('users', 'full_name', existing_type=sa.String(128), comment='全名')
