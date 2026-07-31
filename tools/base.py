from abc import ABC, abstractmethod
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from uuid import UUID

from anthropic.types import ToolParam
from pydantic import BaseModel, ValidationError
from sqlalchemy.ext.asyncio import AsyncSession

from core.event_bus import StreamBus
from tools.shell_policy import ShellAccess


@dataclass
class ToolContext:
    session_id: int
    db: AsyncSession
    bus: StreamBus | None = None
    turn_id: UUID | None = None
    user_id: int | None = None
    workspace_id: int | None = None
    # shell 访问级别与可写临时目录:由 SubAgent 规格下发,Bash 工具据此自校验。
    # 主代理路径不设置,等价于 FULL。
    shell_access: ShellAccess = ShellAccess.FULL
    shell_tmp_root: Path | None = None


class BaseTool(ABC):
    name: str
    description: str
    input_model: type[BaseModel]

    def to_param(self) -> ToolParam:
        return ToolParam(
            name=self.name,
            description=self.description,
            input_schema=self.input_model.model_json_schema() if self.input_model else {},
        )

    async def run_with_dict(self, data: dict[str, Any], ctx: ToolContext) -> str:
        try:
            # 将大模型返回的字典类型转为实体对象类型
            args = self.input_model.model_validate(data)
        except ValidationError as err:
            return f"error: {err}. hint: Please fix the parameters and try again"
        return await self.run(args, ctx)

    @abstractmethod
    async def run(self, args: BaseModel, ctx: ToolContext) -> str:
        pass
