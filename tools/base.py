from abc import ABC, abstractmethod
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from uuid import UUID

from pydantic import BaseModel, ValidationError
from sqlalchemy.ext.asyncio import AsyncSession

from core.event_bus import StreamBus
from llm.types import ToolSpec
from tools.policy import ToolProfile
from tools.shell_policy import ShellAccess


@dataclass
class ToolContext:
    session_id: int
    db: AsyncSession
    bus: StreamBus | None = None
    turn_id: UUID | None = None
    user_id: int | None = None
    workspace_id: int | None = None
    # 工具调用方的可信身份,由 ToolService 根据 actor 注入,不接受模型入参覆盖。
    principal_name: str | None = None
    # 能力档位由运行时显式注入,不从模型输入或显示名称推断。
    profile: ToolProfile | None = None
    allowed_tool_names: frozenset[str] | None = None
    allowed_skill_names: frozenset[str] | None = None
    # shell 访问级别与可写临时目录:由 SubAgent 规格下发,Bash 工具据此自校验。
    # 主代理路径不设置,等价于 FULL。
    shell_access: ShellAccess = ShellAccess.FULL
    shell_tmp_root: Path | None = None


class BaseTool(ABC):
    name: str
    description: str
    input_model: type[BaseModel]

    def to_spec(self) -> ToolSpec:
        """产出中立工具描述；编码成各家 SDK 形状由对应客户端内部完成。"""
        return ToolSpec(
            name=self.name,
            description=self.description,
            input_schema=self.input_model.model_json_schema() if self.input_model else {},
        )

    async def run_with_dict(self, data: dict[str, Any], ctx: ToolContext) -> str:
        # 参数校验失败不抛异常，把错误作为工具结果回给模型，让它修正参数后重试。
        try:
            args = self.input_model.model_validate(data)
        except ValidationError as err:
            return f"error: {err}. hint: Please fix the parameters and try again"
        return await self.run(args, ctx)

    @abstractmethod
    async def run(self, args: BaseModel, ctx: ToolContext) -> str:
        pass
