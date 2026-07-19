import re
from pathlib import Path

from pydantic import BaseModel, Field

from core.errors import AgentException
from tools.base import BaseTool, ToolContext


class GrepInput(BaseModel):
    pattern: str = Field(description="搜索模式(支持正则表达式)")
    path: str | None = Field(default=None, description="搜索路径(文件或目录,绝对路径)")
    glob_pattern: str = Field(default="**/*", description="文件匹配模式,如 '**/*.py'")
    case_sensitive: bool = Field(default=True, description="是否大小写敏感")
    max_results: int = Field(default=200, description="返回结果数量上限")
    context_lines: int = Field(default=0, description="显示匹配行前后的上下文行数")


class GrepTool(BaseTool):
    name = "Grep"
    description = "在文件中搜索匹配的内容,支持正则表达式和递归搜索。"
    input_model = GrepInput

    async def run(self, args: GrepInput, ctx: ToolContext) -> str:
        try:
            base = Path(args.path).resolve() if args.path else Path.cwd()
            if not base.exists():
                raise AgentException.message(f"路径不存在: {base}")

            flags = 0 if args.case_sensitive else re.IGNORECASE
            try:
                pattern = re.compile(args.pattern, flags)
            except re.error as exc:
                raise AgentException.message(f"无效的正则表达式: {exc}") from exc

            results = []
            if base.is_file():
                files = [base]
            else:
                files = list(base.glob(args.glob_pattern))
                files = [f for f in files if f.is_file()]

            for file_path in files:
                try:
                    with open(file_path, "r", encoding="utf-8", errors="ignore") as f:
                        lines = f.readlines()

                    for line_num, line in enumerate(lines, 1):
                        if pattern.search(line):
                            match_info = f"{file_path}:{line_num}:{line.rstrip()}"

                            if args.context_lines > 0:
                                context = []
                                start = max(0, line_num - 1 - args.context_lines)
                                end = min(len(lines), line_num + args.context_lines)
                                for i in range(start, end):
                                    prefix = ">" if i == line_num - 1 else " "
                                    context.append(f"{prefix} {i + 1}: {lines[i].rstrip()}")
                                match_info = f"{file_path}:\n" + "\n".join(context)

                            results.append(match_info)

                            if len(results) >= args.max_results:
                                break
                except Exception:
                    # 跳过无法读取的文件
                    continue

                if len(results) >= args.max_results:
                    break

            if not results:
                return f"未找到匹配 '{args.pattern}' 的内容"

            summary = f"找到 {len(results)} 个匹配项"
            if len(results) >= args.max_results:
                summary += f" (已达上限 {args.max_results})"

            return summary + ":\n" + "\n".join(results)
        except AgentException:
            raise
        except Exception as exc:
            raise AgentException.message(f"Grep 搜索失败: {exc}") from exc
