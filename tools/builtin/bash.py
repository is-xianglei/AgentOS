import logging
import os
from pathlib import Path

from pydantic import BaseModel, Field

from core.errors import AgentException
from core.executor import get_executor
from tools.base import BaseTool, ToolContext
from tools.shell_policy import ShellAccess, check_shell_command

logger = logging.getLogger(__name__)


class BashInput(BaseModel):
    command: str = Field(description="要执行的 shell 命令")
    cwd: str | None = Field(default=None, description="工作目录(绝对路径),默认当前工作目录")
    timeout: int = Field(default=30, description="超时时间(秒)")


class BashTool(BaseTool):
    name = "Bash"
    description = "执行 shell 命令并返回输出。支持设置工作目录和超时时间。"
    input_model = BashInput

    @staticmethod
    def _resolve_workdir(cwd: str | None, ctx: ToolContext) -> Path:
        """定出工作目录,受限档位下限制其范围。

        命令白名单只看命令本身,不看在哪执行:`ls` 在任意目录都合规,
        但把 cwd 指到项目外就等于给了越界的读取面。因此受限档位下要求 cwd
        落在项目目录或该 spec 的临时目录内,拒绝时抛业务异常而非静默改路径。
        """
        if cwd is None:
            return Path.cwd()
        target = Path(cwd).expanduser().resolve(strict=False)
        if ctx.shell_access is ShellAccess.FULL:
            return target
        roots = [Path.cwd().resolve(strict=False)]
        if ctx.shell_tmp_root is not None:
            roots.append(ctx.shell_tmp_root.resolve(strict=False))
        if not any(target == root or root in target.parents for root in roots):
            raise AgentException.message(f"受限模式下工作目录越界: {target}")
        return target

    async def run(self, args: BashInput, ctx: ToolContext) -> str:
        # 只读约束在命令进入 bash -c 之前强制,不依赖提示词自律。拒绝理由回灌给
        # 模型而非抛异常,便于其改用允许的手段重试。
        verdict = check_shell_command(
            args.command,
            ctx.shell_access,
            tmp_root=ctx.shell_tmp_root,
        )
        if not verdict.allowed:
            logger.warning(
                "shell 命令被访问策略拒绝,会话 ID=%s 级别=%s 原因=%s",
                ctx.session_id,
                ctx.shell_access.value,
                verdict.reason,
                extra={"session_id": ctx.session_id, "shell_access": ctx.shell_access.value},
            )
            return f"error: {verdict.reason}"

        try:
            workdir = self._resolve_workdir(args.cwd, ctx)
            if not workdir.exists():
                raise AgentException.message(f"工作目录不存在: {workdir}")
            if not workdir.is_dir():
                raise AgentException.message(f"工作目录不是目录: {workdir}")

            # 使用 bash -c 执行命令,与 core/executor.py 保持一致的最小环境变量
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
