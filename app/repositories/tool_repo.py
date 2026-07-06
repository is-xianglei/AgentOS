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

    async def mark_awaiting(self, record: ToolCallRecord) -> ToolCallRecord:
        """把工具调用置为待审批(不执行,入参已存,等待用户裁决)。"""
        record.status = "awaiting_approval"
        await self.db.flush()
        await self.db.refresh(record)
        return record

    async def resolve_awaiting(
        self, record: ToolCallRecord, decision: str
    ) -> ToolCallRecord:
        """裁决一条待审批记录。

        decision=deny 时直接终态为 rejected;allow_once/always_allow 时回到 running,
        由后续真正执行走 succeed/fail。
        """
        if decision == "deny":
            record.status = "rejected"
            record.finished_at = datetime.now(timezone.utc)
        else:
            record.status = "running"
        await self.db.flush()
        await self.db.refresh(record)
        return record

