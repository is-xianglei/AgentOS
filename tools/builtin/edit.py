from pathlib import Path

from pydantic import BaseModel, Field

from core.errors import AgentException
from tools.base import BaseTool, ToolContext


class EditInput(BaseModel):
    file_path: str = Field(description="要编辑的文件路径(绝对路径)")
    old_string: str = Field(description="要替换的原字符串(必须完全匹配)")
    new_string: str = Field(description="替换后的新字符串")
    replace_all: bool = Field(default=False, description="是否替换所有匹配项")


class EditTool(BaseTool):
    name = "Edit"
    description = "精确字符串替换编辑文件。old_string 必须在文件中完全匹配且唯一(或 replace_all=true)。"
    input_model = EditInput

    async def run(self, args: EditInput, ctx: ToolContext) -> str:
        try:
            path = Path(args.file_path).resolve()
            if not path.exists():
                raise AgentException.message(f"文件不存在: {args.file_path}")
            if not path.is_file():
                raise AgentException.message(f"不是文件: {args.file_path}")

            with open(path, "r", encoding="utf-8", errors="replace") as f:
                content = f.read()

            if args.old_string not in content:
                raise AgentException.message(
                    f"在文件中未找到匹配的字符串:\n{args.old_string[:100]}"
                )

            count = content.count(args.old_string)
            if not args.replace_all and count > 1:
                raise AgentException.message(
                    f"找到 {count} 个匹配项,但 replace_all=false。请设置 replace_all=true 或使用更精确的 old_string。"
                )

            if args.replace_all:
                new_content = content.replace(args.old_string, args.new_string)
            else:
                new_content = content.replace(args.old_string, args.new_string, 1)

            with open(path, "w", encoding="utf-8") as f:
                f.write(new_content)

            replaced = count if args.replace_all else 1
            return f"已编辑文件: {args.file_path} (替换了 {replaced} 处)"
        except AgentException:
            raise
        except Exception as exc:
            raise AgentException.message(f"编辑文件失败: {exc}") from exc
