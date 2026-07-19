from pathlib import Path

from pydantic import BaseModel, Field

from core.errors import AgentException
from tools.base import BaseTool, ToolContext


class ReadInput(BaseModel):
    file_path: str = Field(description="要读取的文件路径(绝对路径)")
    offset: int | None = Field(default=None, description="起始行号(从 1 开始)")
    limit: int | None = Field(default=None, description="读取的行数")


class ReadTool(BaseTool):
    name = "Read"
    description = "读取文件内容,带行号输出。支持按行偏移和限制读取行数。"
    input_model = ReadInput

    async def run(self, args: ReadInput, ctx: ToolContext) -> str:
        try:
            path = Path(args.file_path).resolve()
            if not path.exists():
                raise AgentException.message(f"文件不存在: {args.file_path}")
            if not path.is_file():
                raise AgentException.message(f"不是文件: {args.file_path}")

            with open(path, "r", encoding="utf-8", errors="replace") as f:
                lines = f.readlines()

            offset = (args.offset or 1) - 1
            limit = args.limit or len(lines)

            if offset < 0 or offset >= len(lines):
                raise AgentException.message(f"偏移量超出范围: {args.offset}")

            selected = lines[offset : offset + limit]
            numbered = [
                f"{offset + idx + 1}\t{line.rstrip()}" for idx, line in enumerate(selected)
            ]
            return "\n".join(numbered)
        except AgentException:
            raise
        except Exception as exc:
            raise AgentException.message(f"读取文件失败: {exc}") from exc
