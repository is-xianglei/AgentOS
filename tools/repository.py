from datetime import datetime, timezone
from typing import Any

from sqlalchemy import func, or_, select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from tools.models import ToolCallRecord, ToolRecord


class ToolCatalogRepository:
    """管理数据库中的内置工具目录。"""

    def __init__(self, db: AsyncSession):
        self.db = db

    async def sync_builtin(
        self,
        *,
        tool_key: str,
        name: str,
        description: str,
        runtime_name: str,
        input_schema: dict[str, Any],
    ) -> None:
        stmt = insert(ToolRecord).values(
            tool_key=tool_key,
            name=name,
            description=description,
            tool_type="builtin",
            runtime_name=runtime_name,
            input_schema=input_schema,
            implementation_config={},
            is_system=True,
            is_enabled=True,
            is_deleted=False,
            deleted_at=None,
        )
        stmt = stmt.on_conflict_do_update(
            index_elements=[ToolRecord.tool_key],
            set_={
                "name": name,
                "description": description,
                "tool_type": "builtin",
                "runtime_name": runtime_name,
                "input_schema": input_schema,
                "implementation_config": {},
                "is_system": True,
                "is_deleted": False,
                "deleted_at": None,
                "updated_at": func.now(),
            },
        )
        await self.db.execute(stmt)

    async def mark_missing_deleted(self, runtime_names: set[str]) -> None:
        stmt = (
            update(ToolRecord)
            .where(
                ToolRecord.tool_type == "builtin",
                ToolRecord.runtime_name.not_in(runtime_names),
                ToolRecord.is_deleted.is_(False),
            )
            .values(
                is_deleted=True,
                deleted_at=func.now(),
                updated_at=func.now(),
            )
        )
        await self.db.execute(stmt)

    async def get_by_id(self, tool_id: int) -> ToolRecord | None:
        return await self.db.get(ToolRecord, tool_id)

    async def get_by_runtime_name(self, runtime_name: str) -> ToolRecord | None:
        stmt = select(ToolRecord).where(ToolRecord.runtime_name == runtime_name)
        return (await self.db.scalars(stmt)).first()

    async def list_by_ids(self, tool_ids: list[int]) -> list[ToolRecord]:
        if not tool_ids:
            return []
        stmt = select(ToolRecord).where(ToolRecord.id.in_(tool_ids))
        return list(await self.db.scalars(stmt))

    async def list_by_runtime_names(self, runtime_names: list[str]) -> list[ToolRecord]:
        if not runtime_names:
            return []
        stmt = select(ToolRecord).where(ToolRecord.runtime_name.in_(runtime_names))
        return list(await self.db.scalars(stmt))

    async def list_page(
        self,
        *,
        keyword: str | None,
        is_enabled: bool | None,
        limit: int,
        offset: int,
    ) -> tuple[list[ToolRecord], int]:
        filters = []
        if keyword:
            pattern = f"%{keyword.strip()}%"
            filters.append(
                or_(
                    ToolRecord.tool_key.ilike(pattern),
                    ToolRecord.name.ilike(pattern),
                    ToolRecord.description.ilike(pattern),
                )
            )
        if is_enabled is not None:
            filters.append(ToolRecord.is_enabled.is_(is_enabled))
        stmt = (
            select(ToolRecord)
            .where(*filters)
            .order_by(ToolRecord.name, ToolRecord.id)
            .limit(limit)
            .offset(offset)
        )
        count_stmt = select(func.count(ToolRecord.id)).where(*filters)
        records = list(await self.db.scalars(stmt))
        total = int((await self.db.scalar(count_stmt)) or 0)
        return records, total


class ToolRepository:
    def __init__(self, db: AsyncSession):
        self.db = db

    async def get(self, tool_call_id: int) -> ToolCallRecord | None:
        return await self.db.get(ToolCallRecord, tool_call_id)

    async def start(
        self,
        session_id: int,
        tool_name: str,
        input_args: dict[str, Any],
        message_id: int | None = None,
        tool_id: int | None = None,
        agent_id: int | None = None,
    ) -> ToolCallRecord:
        record = ToolCallRecord(
            session_id=session_id,
            message_id=message_id,
            tool_id=tool_id,
            agent_id=agent_id,
            tool_name=tool_name,
            input_args=input_args,
            status="running",
        )
        self.db.add(record)
        await self.db.flush()
        await self.db.refresh(record)
        return record

    async def succeed(self, record: ToolCallRecord, output_data: Any) -> ToolCallRecord:
        record.status = "succeeded"
        record.output_data = output_data
        record.finished_at = datetime.now(timezone.utc)
        await self.db.flush()
        await self.db.refresh(record)
        return record

    async def fail(self, record: ToolCallRecord, error_message: str) -> ToolCallRecord:
        record.status = "failed"
        record.error_message = error_message
        record.finished_at = datetime.now(timezone.utc)
        await self.db.flush()
        await self.db.refresh(record)
        return record

    async def mark_awaiting_interaction(self, record: ToolCallRecord) -> ToolCallRecord:
        """把工具调用置为等待人工交互。"""
        record.status = "awaiting_interaction"
        await self.db.flush()
        await self.db.refresh(record)
        return record

    async def reject_awaiting(self, record: ToolCallRecord, message: str) -> ToolCallRecord:
        """把等待交互的工具调用标记为拒绝。"""
        record.status = "rejected"
        record.error_message = message
        record.finished_at = datetime.now(timezone.utc)
        await self.db.flush()
        await self.db.refresh(record)
        return record
