from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.event_bus import StreamBus
from app.repositories.tool_repo import ToolRepository
from app.tools.base import ToolContext
from app.tools.registry import ToolRegistry


class ToolService:
    def __init__(self, db: AsyncSession, registry: ToolRegistry):
        """初始化工具执行器依赖。"""
        self.db = db
        self.registry = registry
        self.tool_repo = ToolRepository(db)

    async def run(
        self,
        session_id: int,
        tool_name: str,
        input_args: dict[str, Any],
        message_id: int | None = None,
        bus: StreamBus | None = None,
    ) -> str:
        """执行工具并记录调用状态。"""
        record = await self.tool_repo.start(session_id, tool_name, input_args, message_id=message_id)
        try:
            tool = self.registry.get(tool_name)
            ctx = ToolContext(session_id=session_id, db=self.db, bus=bus)
            output = await tool.run_with_dict(input_args, ctx)
        except Exception as exc:
            await self.tool_repo.fail(record, str(exc))
            await self.db.flush()
            raise
        await self.tool_repo.succeed(record, output)
        await self.db.flush()
        return output
