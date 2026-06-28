from datetime import datetime, timezone
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.models.tool import ToolCallRecord


class ToolRepository:
    def __init__(self, db: AsyncSession):
        self.db = db

    async def start(
        self,
        session_id: int,
        tool_name: str,
        input_args: dict[str, Any],
        message_id: int | None = None,
    ) -> ToolCallRecord:
        record = ToolCallRecord(
            session_id=session_id,
            message_id=message_id,
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
