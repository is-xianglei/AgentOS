from pathlib import Path

from pydantic import BaseModel, Field

from core.errors import AgentException
from tools.base import BaseTool, ToolContext


class WriteInput(BaseModel):
    file_path: str = Field(description="要写入的文件路径(绝对路径)")
    content: str = Field(description="要写入的内容")


class WriteTool(BaseTool):
    name = "Write"
    description = "创建或覆盖文件,写入指定内容。"
    input_model = WriteInput

    async def run(self, args: WriteInput, ctx: ToolContext) -> str:
        try:
            path = Path(args.file_path).resolve()
            path.parent.mkdir(parents=True, exist_ok=True)

            with open(path, "w", encoding="utf-8") as f:
                f.write(args.content)

            return f"已写入文件: {args.file_path} ({len(args.content)} 字符)"
        except Exception as exc:
            raise AgentException.message(f"写入文件失败: {exc}") from exc
