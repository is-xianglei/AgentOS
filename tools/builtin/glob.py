from pathlib import Path

from pydantic import BaseModel, Field

from core.errors import AgentException
from tools.base import BaseTool, ToolContext


class GlobInput(BaseModel):
    pattern: str = Field(description="glob 模式,如 '**/*.py' 或 'src/**/*.ts'")
    base_dir: str | None = Field(default=None, description="搜索基准目录(绝对路径),默认当前工作目录")
    max_results: int = Field(default=500, description="返回结果数量上限")


class GlobTool(BaseTool):
    name = "Glob"
    description = "按 glob 模式查找文件。支持 ** 递归通配符。"
    input_model = GlobInput

    async def run(self, args: GlobInput, ctx: ToolContext) -> str:
        try:
            base = Path(args.base_dir).resolve() if args.base_dir else Path.cwd()
            if not base.exists():
                raise AgentException.message(f"基准目录不存在: {base}")
            if not base.is_dir():
                raise AgentException.message(f"基准目录不是目录: {base}")

            matches = list(base.glob(args.pattern))
            matches = [m for m in matches if m.is_file()]
            matches.sort()

            if len(matches) > args.max_results:
                return (
                    f"找到 {len(matches)} 个文件,超过上限 {args.max_results},仅显示前 {args.max_results} 个:\n"
                    + "\n".join(str(m) for m in matches[: args.max_results])
                )

            if not matches:
                return f"未找到匹配 '{args.pattern}' 的文件"

            return "\n".join(str(m) for m in matches)
        except AgentException:
            raise
        except Exception as exc:
            raise AgentException.message(f"Glob 查找失败: {exc}") from exc
