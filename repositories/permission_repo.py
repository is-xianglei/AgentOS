from datetime import datetime, timezone
from typing import Any

from sqlalchemy import and_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from models.permission import PermissionRuleRecord


class PermissionRepository:
    def __init__(self, db: AsyncSession):
        self.db = db

    async def list(
        self, scope: str | None = None, session_id: int | None = None
    ) -> list[PermissionRuleRecord]:
        """按可选 scope / session_id 过滤列出规则。"""
        stmt = select(PermissionRuleRecord)
        if scope is not None:
            stmt = stmt.where(PermissionRuleRecord.scope == scope)
        if session_id is not None:
            stmt = stmt.where(PermissionRuleRecord.session_id == session_id)
        stmt = stmt.order_by(PermissionRuleRecord.id)
        return list(await self.db.scalars(stmt))

    async def get(self, rule_id: int) -> PermissionRuleRecord | None:
        return await self.db.get(PermissionRuleRecord, rule_id)

    async def find_match(
        self, scope: str, session_id: int | None, tool_name: str
    ) -> PermissionRuleRecord | None:
        """查找作用域内针对某工具的规则(取最新一条)。"""
        conditions = [
            PermissionRuleRecord.scope == scope,
            PermissionRuleRecord.tool_name == tool_name,
        ]
        if session_id is None:
            conditions.append(PermissionRuleRecord.session_id.is_(None))
        else:
            conditions.append(PermissionRuleRecord.session_id == session_id)
        stmt = (
            select(PermissionRuleRecord)
            .where(and_(*conditions))
            .order_by(PermissionRuleRecord.id.desc())
            .limit(1)
        )
        return (await self.db.scalars(stmt)).first()

    async def upsert(
        self,
        scope: str,
        session_id: int | None,
        tool_name: str,
        behavior: str,
        source: str = "user",
        matcher: dict[str, Any] | None = None,
    ) -> PermissionRuleRecord:
        """新增或更新一条规则(同 scope/session_id/tool_name 视为同一条)。"""
        existing = await self.find_match(scope, session_id, tool_name)
        if existing is not None:
            existing.behavior = behavior
            existing.source = source
            existing.matcher = matcher
            await self.db.flush()
            await self.db.refresh(existing)
            return existing
        record = PermissionRuleRecord(
            scope=scope,
            session_id=session_id,
            tool_name=tool_name,
            behavior=behavior,
            source=source,
            matcher=matcher,
        )
        self.db.add(record)
        await self.db.flush()
        await self.db.refresh(record)
        return record

    async def delete(self, rule_id: int) -> int:
        """软删除一条规则:标记 is_deleted / deleted_at,返回实际标记行数。

        全局读过滤会自动排除已删除规则,故 find_match/list 无需改动。
        已处于删除态的规则不再重复标记(rowcount 为 0)。
        """
        stmt = (
            update(PermissionRuleRecord)
            .where(
                PermissionRuleRecord.id == rule_id,
                PermissionRuleRecord.is_deleted.is_(False),
            )
            .values(is_deleted=True, deleted_at=datetime.now(timezone.utc))
        )
        result = await self.db.execute(stmt)
        await self.db.flush()
        return result.rowcount or 0
