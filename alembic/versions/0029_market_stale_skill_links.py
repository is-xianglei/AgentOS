"""清除市场安装记录里指向已软删除 Skill 的旧关联，只修数据，不改表结构。

skill 域新建同名 Skill 时会把软删行原地复活、沿用原 ID。代码修复之前，删除 Skill 不会通知市场，
安装记录里还留着指向已删 Skill 的 target_skill_id 和 managed_skill_ids；
这些 Skill 一旦被复活，就会被误认成从市场装来的。代码修复只管以后的删除，存量数据由本迁移清理。

Revision ID: 0029_market_stale_skill_links
Revises: 0028_market_platform_admin
"""
from alembic import op

revision = "0029_market_stale_skill_links"
down_revision = "0028_market_platform_admin"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # 1. 安装出来的 Skill 已软删除：目标置空，这条记录按现有口径视为未安装，重新安装时复用这一行。
    #    已软删除的安装记录也一起处理，与代码里删除 Skill 时的清理范围一致。
    op.execute(
        """
        UPDATE market_installs AS install
        SET target_skill_id = NULL
        FROM skills AS skill
        WHERE skill.id = install.target_skill_id
          AND skill.is_deleted = true
        """
    )

    # 2. 托管 Skill 列表里去掉已软删除的 Skill，其余 ID 保持原来的顺序；全部去掉时留空列表。
    #    只更新确实含有已删 Skill 的行，没有问题的行不动。
    op.execute(
        """
        UPDATE market_installs AS install
        SET managed_skill_ids = (
            SELECT COALESCE(
                jsonb_agg(to_jsonb(element.skill_id::int) ORDER BY element.ordinal),
                '[]'::jsonb
            )
            FROM jsonb_array_elements_text(install.managed_skill_ids)
                WITH ORDINALITY AS element(skill_id, ordinal)
            WHERE NOT EXISTS (
                SELECT 1
                FROM skills AS skill
                WHERE skill.id = element.skill_id::int
                  AND skill.is_deleted = true
            )
        )
        WHERE EXISTS (
            SELECT 1
            FROM jsonb_array_elements_text(install.managed_skill_ids) AS element(skill_id)
            JOIN skills AS skill ON skill.id = element.skill_id::int
            WHERE skill.is_deleted = true
        )
        """
    )


def downgrade() -> None:
    # 显式不做任何事：被清除的是指向已删除 Skill 的失效关联，原值没有保存，无法还原；
    # 即使能还原，也只会把已修复的错误重新带回来。降级时表结构不受影响。
    pass
