from datetime import datetime, timezone
from typing import Any

from sqlalchemy import and_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from permission.models import PermissionRuleRecord


def matcher_agent_type(matcher: dict[str, Any] | None) -> str | None:
    """从 matcher 中取出 agent_type 维度;缺失或非字符串视为未限定。"""
    if not matcher:
        return None
    value = matcher.get("agent_type")
    return value if isinstance(value, str) and value else None


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

    async def find_candidates(
        self, scope: str, session_id: int | None, tool_name: str
    ) -> list[PermissionRuleRecord]:
        """列出作用域内针对某工具的全部规则,按 id 倒序(最新优先)。

        matcher 维度的取舍在 Python 侧完成:同一 (scope, session_id, tool_name) 下
        规则数极少,换取不与 JSONB 查询方言耦合,单测无需真实 PostgreSQL。
        """
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
        )
        return list(await self.db.scalars(stmt))

    async def find_match(
        self,
        scope: str,
        session_id: int | None,
        tool_name: str,
        agent_type: str | None = None,
    ) -> PermissionRuleRecord | None:
        """按 agent_type 优先级取一条规则:定向规则 > 未限定 agent_type 的通用规则。

        agent_type 为 None(主代理)时只认通用规则,不会误命中子代理的定向放行。
        """
        candidates = await self.find_candidates(scope, session_id, tool_name)
        fallback: PermissionRuleRecord | None = None
        for rule in candidates:
            scoped = matcher_agent_type(rule.matcher)
            if scoped is None:
                fallback = fallback or rule
            elif agent_type is not None and scoped == agent_type:
                return rule
        return fallback

    async def find_exact(
        self,
        scope: str,
        session_id: int | None,
        tool_name: str,
        agent_type: str | None = None,
    ) -> PermissionRuleRecord | None:
        """精确匹配同一 agent_type 维度的规则,供 upsert 判定是否为同一条。"""
        candidates = await self.find_candidates(scope, session_id, tool_name)
        for rule in candidates:
            if matcher_agent_type(rule.matcher) == agent_type:
                return rule
        return None

    async def upsert(
        self,
        scope: str,
        session_id: int | None,
        tool_name: str,
        behavior: str,
        source: str = "user",
        matcher: dict[str, Any] | None = None,
    ) -> PermissionRuleRecord:
        """新增或更新一条规则。

        去重键为 (scope, session_id, tool_name, matcher.agent_type):
        定向规则与通用规则互不覆盖,否则给 verification 放行 Bash 会顺带改掉主代理的规则。
        """
        existing = await self.find_exact(
            scope, session_id, tool_name, matcher_agent_type(matcher)
        )
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
