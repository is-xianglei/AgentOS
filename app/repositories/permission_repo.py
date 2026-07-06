from typing import Any

from sqlalchemy import and_, delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.permission import PermissionRuleRecord


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
        stmt = delete(PermissionRuleRecord).where(PermissionRuleRecord.id == rule_id)
        result = await self.db.execute(stmt)
        await self.db.flush()
        return result.rowcount or 0
