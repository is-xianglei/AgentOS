import os
from pathlib import Path

from pydantic import BaseModel, Field

from core.config import settings
from core.errors import AgentException
from core.skill_executor import get_executor
from tools.base import BaseTool, ToolContext


class BashInput(BaseModel):
    command: str = Field(description="要执行的 shell 命令")
    cwd: str | None = Field(default=None, description="工作目录(绝对路径),默认当前工作目录")
    timeout: int = Field(default=30, description="超时时间(秒)")


class BashTool(BaseTool):
    name = "Bash"
    description = "执行 shell 命令并返回输出。支持设置工作目录和超时时间。"
    input_model = BashInput

    async def run(self, args: BashInput, ctx: ToolContext) -> str:
        try:
            workdir = Path(args.cwd).resolve() if args.cwd else Path.cwd()
            if not workdir.exists():
                raise AgentException.message(f"工作目录不存在: {workdir}")
            if not workdir.is_dir():
                raise AgentException.message(f"工作目录不是目录: {workdir}")

            # 使用 bash -c 执行命令,与 skill_executor 保持一致的最小环境变量
            executor = get_executor()
            env = {
                "PATH": os.environ.get("PATH", ""),
                "LANG": os.environ.get("LANG", "C.UTF-8"),
                "HOME": os.environ.get("HOME", ""),
            }

            result = await executor.run(
                workdir=str(workdir),
                argv=["bash", "-c", args.command],
                stdin=None,
                timeout=args.timeout,
                env=env,
            )

            parts = []
            if result.timed_out:
                parts.append(f"[命令超时被终止,超时设置: {args.timeout}s]")
            else:
                parts.append(f"[退出码: {result.exit_code}]")

            if result.truncated:
                parts.append("[输出已截断]")

            if result.stdout:
                parts.append(f"=== stdout ===\n{result.stdout}")
            if result.stderr:
                parts.append(f"=== stderr ===\n{result.stderr}")

            if not result.stdout and not result.stderr:
                parts.append("(无输出)")

            return "\n".join(parts)
        except AgentException:
            raise
        except Exception as exc:
            raise AgentException.message(f"执行命令失败: {exc}") from exc
