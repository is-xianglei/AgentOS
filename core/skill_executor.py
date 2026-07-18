import asyncio
import os
import signal
from abc import ABC, abstractmethod
from dataclasses import dataclass

from core.config import settings
from core.errors import AgentException


@dataclass(frozen=True)
class ExecResult:
    """执行结果。"""

    exit_code: int | None  # 正常退出为退出码;超时被 kill 为 None
    stdout: str  # 已按上限截断(UTF-8, errors="replace")
    stderr: str  # 已按上限截断
    timed_out: bool  # 是否因超时被终止
    truncated: bool  # stdout/stderr 是否发生过截断


class Executor(ABC):
    """执行器统一接口。"""

    @abstractmethod
    async def run(
        self,
        *,
        workdir: str,
        argv: list[str],
        stdin: bytes | None,
        timeout: int,
        env: dict[str, str],
    ) -> ExecResult:
        """在 workdir 内执行 argv,返回 ExecResult。"""


class SubprocessExecutor(Executor):
    """进程内子进程执行

    - 用 asyncio.create_subprocess_exec(argv 数组),绝不经 shell,杜绝注入。
    - start_new_session=True 建独立进程组;超时用 killpg(SIGKILL) 杀整组防孙进程逃逸。
    - stdout/stderr 各自按 SKILL_EXEC_MAX_OUTPUT 截断,decode(utf-8, errors=replace)。
    - env 由调用方传入(最小白名单),本层原样使用,不追加继承。
    """

    async def run(
        self,
        *,
        workdir: str,
        argv: list[str],
        stdin: bytes | None,
        timeout: int,
        env: dict[str, str],
    ) -> ExecResult:
        try:
            proc = await asyncio.create_subprocess_exec(
                *argv,
                cwd=workdir,
                env=env,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                start_new_session=True,  # 独立进程组,便于超时整组回收
            )
        except (OSError, ValueError) as exc:
            # 解释器不存在等底层异常转领域错误,不外泄 traceback 给模型。
            raise AgentException.message(f"启动子进程失败: {exc}") from exc

        timed_out = False
        try:
            stdout_raw, stderr_raw = await asyncio.wait_for(
                proc.communicate(input=stdin), timeout=timeout
            )
        except (asyncio.TimeoutError, TimeoutError):
            timed_out = True
            stdout_raw, stderr_raw = b"", b""
            # 杀整个进程组(POSIX);兜底直接 kill 单进程。
            try:
                os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
            except (ProcessLookupError, PermissionError, OSError):
                try:
                    proc.kill()
                except ProcessLookupError:
                    pass
            # 回收已被杀死的进程,避免僵尸与 communicate 泄漏。
            try:
                await proc.wait()
            except ProcessLookupError:
                pass

        limit = settings.skill_exec_max_output
        truncated = len(stdout_raw) > limit or len(stderr_raw) > limit
        stdout = stdout_raw[:limit].decode("utf-8", errors="replace")
        stderr = stderr_raw[:limit].decode("utf-8", errors="replace")
        exit_code = None if timed_out else proc.returncode
        return ExecResult(
            exit_code=exit_code,
            stdout=stdout,
            stderr=stderr,
            timed_out=timed_out,
            truncated=truncated,
        )


# 进程内单例缓存。
_executor_instance: Executor | None = None


def get_executor() -> Executor:
    """返回进程内缓存的执行器单例(本期恒为 SubprocessExecutor)。

    将来接入容器 / nsjail 时新增 SandboxExecutor,在此按配置切换,上层不改。
    """
    global _executor_instance
    if _executor_instance is None:
        _executor_instance = SubprocessExecutor()
    return _executor_instance
