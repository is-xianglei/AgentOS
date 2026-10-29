from datetime import datetime, timezone
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import defer

from skill.models import SkillRecord


def _metadata_only() -> tuple[Any, ...]:
    """列表类查询不加载正文与附属文件:二者可达数 MB,catalog 和列表只用元数据。

    须在调用时构造:模块级 defer() 会在导入期触发 mapper 配置,早于 registry 登记全部实体。
    """
    return (defer(SkillRecord.content), defer(SkillRecord.extra_files))


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
        """按 name 取整行(含正文与附属文件)。include_deleted=True 时可读已软删记录。"""
        stmt = select(SkillRecord).where(
            SkillRecord.workspace_id == workspace_id,
            SkillRecord.name == name,
        )
        if include_deleted:
            # 全局软删过滤(database/engine.py 的 do_orm_execute)默认会追加 is_deleted=false,
            # 复活需读到已软删行,故显式关闭本次过滤。
            stmt = stmt.execution_options(include_deleted=True)
        return (await self.db.scalars(stmt)).first()

    async def list_enabled(
        self,
        workspace_id: int,
        skill_ids: list[int] | None = None,
    ) -> list[SkillRecord]:
        """列出已启用的 skill 元数据,按创建时间倒序,供 catalog 发现用。"""
        stmt = (
            select(SkillRecord)
            .options(*_metadata_only())
            .where(SkillRecord.workspace_id == workspace_id, SkillRecord.is_enabled.is_(True))
        )
        if skill_ids is not None:
            stmt = stmt.where(SkillRecord.id.in_(skill_ids))
        stmt = stmt.order_by(SkillRecord.created_at.desc())
        return list(await self.db.scalars(stmt))

    async def list_all(self, workspace_id: int) -> list[SkillRecord]:
        """列出全部未软删 skill 元数据(含禁用),按创建时间倒序,供列表接口用。"""
        stmt = (
            select(SkillRecord)
            .options(*_metadata_only())
            .where(SkillRecord.workspace_id == workspace_id)
            .order_by(SkillRecord.created_at.desc())
        )
        return list(await self.db.scalars(stmt))

    async def list_by_ids(self, workspace_id: int, skill_ids: list[int]) -> list[SkillRecord]:
        """按 ID 取未软删 skill 元数据(含禁用)。"""
        if not skill_ids:
            return []
        stmt = (
            select(SkillRecord)
            .options(*_metadata_only())
            .where(SkillRecord.workspace_id == workspace_id, SkillRecord.id.in_(skill_ids))
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
        skill_hash: str,
        content: str,
        extra_files: dict[str, dict[str, str]],
        actor_user_id: int,
    ) -> SkillRecord:
        """覆盖更新 + 软删复活;只 flush/refresh,commit 交给请求边界。

        启用状态:新建与软删复活为启用;更新有效记录时保持原值,重新上传不会自动启用。
        """
        existing = await self.get_by_name(workspace_id, name, include_deleted=True)
        if existing is None:
            record = SkillRecord(
                workspace_id=workspace_id,
                name=name,
                is_enabled=True,
                created_by_user_id=actor_user_id,
            )
            self.db.add(record)
        else:
            record = existing
            if record.is_deleted:
                # 复活等同重新创建,创建者与启用状态随之重置。
                record.is_deleted = False
                record.deleted_at = None
                record.is_enabled = True
                record.created_by_user_id = actor_user_id
        record.description = description
        record.frontmatter = frontmatter
        record.version = version
        record.skill_hash = skill_hash
        record.content = content
        record.extra_files = extra_files
        await self.db.flush()
        await self.db.refresh(record)
        return record

    async def set_enabled(self, record: SkillRecord, enabled: bool) -> SkillRecord:
        record.is_enabled = enabled
        await self.db.flush()
        await self.db.refresh(record)
        return record

    async def soft_delete(self, record: SkillRecord) -> None:
        """软删除:置 is_deleted=True/deleted_at=now。"""
        record.is_deleted = True
        record.deleted_at = datetime.now(timezone.utc)
        await self.db.flush()
