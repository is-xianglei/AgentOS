from datetime import datetime, timezone
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from skill.models import SkillRecord


class SkillRepository:

    def __init__(self, db: AsyncSession):
        self.db = db

    async def get_by_name(
        self,
        workspace_id: int,
        name: str,
        *,
        include_deleted: bool = False,
    ) -> SkillRecord | None:
        """按 name 取单条。include_deleted=True 时跳过全局软删读过滤,可读已软删记录。"""
        stmt = select(SkillRecord).where(
            SkillRecord.workspace_id == workspace_id,
            SkillRecord.name == name,
        )
        if include_deleted:
            # 全局软删过滤(database/engine.py 的 do_orm_execute)默认会追加 is_deleted=false,
            # 复活/幂等判重需读到已软删行,故显式关闭本次过滤(仓库首次引入该用法)。
            stmt = stmt.execution_options(include_deleted=True)
        return (await self.db.scalars(stmt)).first()

    async def list_enabled(
        self,
        workspace_id: int,
        skill_ids: list[int] | None = None,
    ) -> list[SkillRecord]:
        """列出全部未软删的 skill,按创建时间倒序,供 catalog 发现用。

        未软删即"可用"(is_deleted 由全局软删过滤兜底,无需再显式过滤)。
        取整行返回(而非只 select name/description 两列):catalog 渲染只用两列,
        但整行返回类型统一为 ORM 记录、调用方无需处理 Row 元组,开销差异可忽略。
        """
        stmt = select(SkillRecord).where(SkillRecord.workspace_id == workspace_id)
        if skill_ids is not None:
            stmt = stmt.where(SkillRecord.id.in_(skill_ids))
        stmt = stmt.order_by(SkillRecord.created_at.desc())
        return list(await self.db.scalars(stmt))

    async def list_all(self, workspace_id: int) -> list[SkillRecord]:
        """列出全部(未软删)skill,按创建时间倒序,供列表接口用。"""
        stmt = (
            select(SkillRecord)
            .where(SkillRecord.workspace_id == workspace_id)
            .order_by(SkillRecord.created_at.desc())
        )
        return list(await self.db.scalars(stmt))

    async def list_by_ids(self, workspace_id: int, skill_ids: list[int]) -> list[SkillRecord]:
        if not skill_ids:
            return []
        stmt = select(SkillRecord).where(
            SkillRecord.workspace_id == workspace_id,
            SkillRecord.id.in_(skill_ids),
        )
        return list(await self.db.scalars(stmt))

    async def upsert(
        self,
        *,
        workspace_id: int,
        name: str,
        description: str,
        frontmatter: dict[str, Any],
        version: str | None,
        skill_hash: str | None,
    ) -> SkillRecord:
        """覆盖更新 + 软删复活。

        1. 含已软删地查同名记录;
        2. 命中:更新元数据字段,并复活(is_deleted=False/deleted_at=None);
        3. 未命中:新建。
        只 flush/refresh,commit 交给 service。
        """
        existing = await self.get_by_name(workspace_id, name, include_deleted=True)
        if existing is not None:
            existing.description = description
            existing.frontmatter = frontmatter
            existing.version = version
            existing.skill_hash = skill_hash
            existing.is_deleted = False
            existing.deleted_at = None
            await self.db.flush()
            await self.db.refresh(existing)
            return existing
        record = SkillRecord(
            workspace_id=workspace_id,
            name=name,
            description=description,
            frontmatter=frontmatter,
            version=version,
            skill_hash=skill_hash,
        )
        self.db.add(record)
        await self.db.flush()
        await self.db.refresh(record)
        return record

    async def soft_delete(self, workspace_id: int, name: str) -> bool:
        """软删除:置 is_deleted=True/deleted_at=now。返回是否命中一条有效记录。"""
        record = await self.get_by_name(workspace_id, name)
        if record is None:
            return False
        record.is_deleted = True
        record.deleted_at = datetime.now(timezone.utc)
        await self.db.flush()
        return True
