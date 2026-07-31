"""Shell 命令级访问策略。

只读约束必须在工具执行边界强制,不能只靠提示词自律:参考实现里 Explore/Plan
虽拿到 shell,但写操作仅由提示词枚举劝阻,模型仍可绕过。本模块把该约束前移到
执行前的静态校验,拒绝重定向、heredoc、命令替换、变更类命令与解释器执行。

三档访问级别:
- FULL:不加额外约束(主代理与 general-purpose,仍受 permission 裁决)。
- READ_ONLY:仅允许白名单只读命令,任何写入途径一律拒绝(Explore / Plan)。
- TMP_WRITABLE:在只读基础上放开构建/测试运行器,且写入目标必须落在临时目录
  内(verification 需要落短生命周期测试脚本)。

注意边界:这是静态命令校验,不是 OS 级沙箱。TMP_WRITABLE 放行的测试运行器
理论上仍能在进程内写项目目录,该残余风险在 docs 中记录。
"""

from __future__ import annotations

import re
import shlex
from dataclasses import dataclass
from enum import Enum
from pathlib import Path


class ShellAccess(str, Enum):
    """shell 访问级别。值用于落库与日志,保持稳定。"""

    FULL = "full"
    READ_ONLY = "read_only"
    TMP_WRITABLE = "tmp_writable"


@dataclass(frozen=True)
class ShellVerdict:
    """校验结论。allowed 为 False 时 reason 必填,用于回灌给模型。"""

    allowed: bool
    reason: str | None = None

    @classmethod
    def ok(cls) -> ShellVerdict:
        return cls(allowed=True)

    @classmethod
    def reject(cls, reason: str) -> ShellVerdict:
        return cls(allowed=False, reason=reason)


# 只读命令白名单。用白名单而非黑名单:黑名单遗漏一个命令就是一个逃逸口,
# 白名单遗漏只会误拒,可按需补。
_READ_ONLY_COMMANDS: frozenset[str] = frozenset(
    {
        # 目录与文件浏览
        "ls", "pwd", "cat", "head", "tail", "wc", "file", "stat", "realpath",
        "basename", "dirname", "readlink", "du", "df", "tree",
        # 检索
        "grep", "egrep", "fgrep", "rg", "ag", "ack", "find", "fd", "locate",
        "which", "type", "whereis",
        # 文本处理(只读用法,写入靠重定向,已在结构检查中拦截)
        "sort", "uniq", "cut", "tr", "diff", "comm", "join", "paste", "nl",
        "column", "expand", "fold", "rev", "strings", "cmp", "md5sum",
        "sha1sum", "sha256sum", "base64", "jq", "yq",
        # 版本控制只读子命令由 _check_git 二次校验
        "git",
        # sed/awk 有写入形态,由专门校验拦截
        "sed", "awk", "gawk",
        # 环境信息
        "echo", "printf", "date", "env", "printenv", "uname", "hostname",
        "whoami", "id", "true", "false", "test", "seq", "sleep",
    }
)

# 构建 / 测试 / 静态检查运行器。仅 TMP_WRITABLE 放行:它们必然产生副作用
# (缓存、编译产物),但这正是 verification 需要的真实执行证据。
_VERIFY_COMMANDS: frozenset[str] = frozenset(
    {
        "uv", "python", "python3", "pytest", "ruff", "mypy", "pyright",
        "alembic", "node", "npm", "npx", "pnpm", "yarn", "bun", "tsc",
        "eslint", "prettier", "go", "cargo", "make", "bash", "sh", "timeout",
        "mktemp",
    }
)

# 变更文件的命令。TMP_WRITABLE 下允许,但每个路径参数都必须落在临时目录内,
# 用于清理自己写的短生命周期脚本。
_PATH_MUTATING_COMMANDS: frozenset[str] = frozenset(
    {"rm", "cp", "mv", "mkdir", "rmdir", "touch", "chmod", "ln", "truncate"}
)

# git 只读子命令。其余(add/commit/push/checkout/reset/clean/rebase 等)一律拒绝。
_GIT_READ_ONLY_SUBCOMMANDS: frozenset[str] = frozenset(
    {
        "status", "log", "diff", "show", "blame", "describe", "rev-parse",
        "rev-list", "ls-files", "ls-tree", "cat-file", "shortlog", "reflog",
        "merge-base", "name-rev", "whatchanged", "grep", "count-objects",
        "verify-pack", "symbolic-ref", "for-each-ref", "check-ignore",
    }
)

# 输出重定向与 heredoc。`<` 是输入重定向,只读,不拦。
_OUTPUT_REDIRECT_RE = re.compile(r"(?<![0-9<>])>>?|[0-9]+>|&>|<<")

# 命令替换:其内容同样是可执行命令,只读档直接拒绝而不做递归校验,
# 避免嵌套解析出现遗漏。
_COMMAND_SUBSTITUTION_RE = re.compile(r"\$\(|`|\$\{[^}]*\(")

# 段分隔符。按这些切开后逐段校验首个命令。
_SEGMENT_SPLIT_RE = re.compile(r"\|\||&&|\||;|\n")

# 永久拒绝:提权、进程控制、包安装与远程取回,任何档位都不放行。
_ALWAYS_DENIED: frozenset[str] = frozenset(
    {
        "sudo", "su", "doas", "systemctl", "service", "kill", "killall",
        "pkill", "shutdown", "reboot", "mount", "umount", "dd", "mkfs",
        "eval", "exec", "source", ".", "xargs", "tee", "nohup", "disown",
        "crontab", "at", "ssh", "scp", "rsync", "nc", "netcat", "telnet",
        "wget", "curl", "apt", "apt-get", "yum", "dnf", "brew", "pacman",
        "docker", "podman", "kubectl", "helm", "terraform", "pip", "pip3",
    }
)

# 依赖安装类子命令。verification 允许跑测试但不得改变依赖树。

# 同一子命令兼具读写用途:`git branch` 列出分支是只读的,`git branch -D x` 删分支,
# 而 `git branch feature-x` 不带任何标志就能建分支。因此判定条件是
# 「无额外位置参数且无变更标志」,只放行纯列出形态。
# stash 不在此列:裸 `git stash` 等价于 push,会直接改工作区。
_GIT_DUAL_USE_SUBCOMMANDS: frozenset[str] = frozenset({"branch", "tag", "remote"})
_GIT_MUTATING_FLAGS: frozenset[str] = frozenset(
    {
        "-d", "-D", "--delete", "-m", "-M", "--move", "-c", "-C", "--copy",
        "-f", "--force", "--set-upstream-to", "-u", "--unset-upstream", "--edit-description",
    }
)

_INSTALL_SUBCOMMANDS: frozenset[str] = frozenset(
    {"install", "add", "remove", "uninstall", "sync", "update", "upgrade", "ci"}
)


def _split_segments(command: str) -> list[str]:
    """按管道与逻辑连接符切分,得到需要逐段校验的子命令。"""
    return [seg.strip() for seg in _SEGMENT_SPLIT_RE.split(command) if seg.strip()]


def _tokenize(segment: str) -> list[str] | None:
    """尽力分词。引号不闭合等无法解析的输入返回 None,由调用方拒绝。"""
    try:
        return shlex.split(segment, comments=False)
    except ValueError:
        return None


def _strip_wrappers(tokens: list[str]) -> list[str]:
    """剥掉子 shell 括号与前置环境变量赋值,取出真正的命令名。"""
    result = list(tokens)
    while result:
        head = result[0]
        if head in ("(", "{", "!"):
            result = result[1:]
            continue
        # FOO=bar cmd 形态:赋值前缀不是命令本身。
        if "=" in head and not head.startswith("-") and "/" not in head.split("=")[0]:
            result = result[1:]
            continue
        break
    return result


def _check_git(tokens: list[str]) -> ShellVerdict:
    """git 仅放行只读子命令,写操作(add/commit/checkout/reset 等)拒绝。"""
    subcommand = next((tok for tok in tokens[1:] if not tok.startswith("-")), None)
    if subcommand is None:
        return ShellVerdict.ok()
    if subcommand in _GIT_DUAL_USE_SUBCOMMANDS:
        positionals = [tok for tok in tokens[1:] if not tok.startswith("-")]
        flags = [tok.split("=", 1)[0] for tok in tokens[1:] if tok.startswith("-")]
        if len(positionals) > 1 or any(flag in _GIT_MUTATING_FLAGS for flag in flags):
            return ShellVerdict.reject(f"只读模式禁止 git {subcommand} 的变更用法")
        return ShellVerdict.ok()
    if subcommand not in _GIT_READ_ONLY_SUBCOMMANDS:
        return ShellVerdict.reject(f"只读模式禁止 git 写操作: git {subcommand}")
    return ShellVerdict.ok()


def _check_sed_awk(tokens: list[str]) -> ShellVerdict:
    """sed -i 原地改写、awk 内 system()/print > file 均视为写操作。"""
    name = Path(tokens[0]).name
    if name == "sed" and any(
        tok.startswith("-i") or tok.startswith("--in-place") for tok in tokens[1:]
    ):
        return ShellVerdict.reject("只读模式禁止 sed -i 原地改写文件")
    if name in ("awk", "gawk"):
        script = " ".join(tokens[1:])
        if "system(" in script or ">" in script or "print >" in script:
            return ShellVerdict.reject("只读模式禁止 awk 内执行命令或写文件")
    return ShellVerdict.ok()


def _check_find(tokens: list[str]) -> ShellVerdict:
    """find 的 -delete / -exec 系列可执行任意变更,拒绝。"""
    dangerous = {"-delete", "-exec", "-execdir", "-ok", "-okdir", "-fprint",
                 "-fprintf", "-fls"}
    hit = next((tok for tok in tokens[1:] if tok in dangerous), None)
    if hit is not None:
        return ShellVerdict.reject(f"只读模式禁止 find {hit}")
    return ShellVerdict.ok()


def _check_install(tokens: list[str]) -> ShellVerdict:
    """包管理器仅放行执行类子命令,禁止改变依赖树。

    只看第二个 token 会被 `uv pip install` 绕过(第二个是 pip),
    因此扫描全部位置参数;但要避开 `uv run` 之后的用户命令,那属被调程序的参数。
    """
    manager = Path(tokens[0]).name
    for index, token in enumerate(tokens[1:], start=1):
        if token.startswith("-"):
            continue
        if token == "run":
            # `uv run <cmd>` 之后是被执行的命令,其参数由该命令所在分段自行校验。
            break
        if token in _INSTALL_SUBCOMMANDS:
            return ShellVerdict.reject(f"禁止安装或变更依赖: {manager} {token}")
        if index >= 3:
            # 位置参数已深过 `<manager> <sub> <sub>`,再往后是包名/路径,不再判定。
            break
    return ShellVerdict.ok()


def _is_under(path: Path, root: Path) -> bool:
    """判断路径是否落在 root 之内(不解析符号链接,避免 TOCTOU 误判)。"""
    try:
        return path == root or root in path.parents
    except (OSError, ValueError):
        return False


def _check_tmp_paths(tokens: list[str], tmp_root: Path) -> ShellVerdict:
    """变更类命令的每个路径参数都必须落在临时目录内。"""
    for token in tokens[1:]:
        if token.startswith("-"):
            continue
        candidate = Path(token)
        resolved = candidate if candidate.is_absolute() else (Path.cwd() / candidate)
        if not _is_under(Path(resolved).resolve(strict=False), tmp_root):
            return ShellVerdict.reject(
                f"仅允许在临时目录 {tmp_root} 内写入,拒绝操作: {token}"
            )
    return ShellVerdict.ok()


def _check_redirect_targets(command: str, tmp_root: Path) -> ShellVerdict:
    """TMP_WRITABLE 下重定向目标必须落在临时目录内。"""
    for match in re.finditer(r"(?:[0-9]*>>?|&>)\s*([^\s;|&]+)", command):
        target = match.group(1).strip("\"'")
        if target.startswith("/dev/"):
            continue
        candidate = Path(target)
        resolved = candidate if candidate.is_absolute() else (Path.cwd() / candidate)
        if not _is_under(Path(resolved).resolve(strict=False), tmp_root):
            return ShellVerdict.reject(
                f"仅允许重定向写入临时目录 {tmp_root},拒绝目标: {target}"
            )
    return ShellVerdict.ok()


def _check_segment(segment: str, access: ShellAccess, tmp_root: Path | None) -> ShellVerdict:
    """校验单个子命令段。"""
    tokens = _tokenize(segment)
    if tokens is None:
        return ShellVerdict.reject("命令无法解析(引号未闭合),受限模式下拒绝执行")
    tokens = _strip_wrappers(tokens)
    if not tokens:
        return ShellVerdict.ok()

    name = Path(tokens[0].rstrip("()")).name
    if name in _ALWAYS_DENIED:
        return ShellVerdict.reject(f"受限模式禁止执行该命令: {name}")

    allowed_names = _READ_ONLY_COMMANDS
    if access is ShellAccess.TMP_WRITABLE:
        allowed_names = allowed_names | _VERIFY_COMMANDS | _PATH_MUTATING_COMMANDS
    if name not in allowed_names:
        return ShellVerdict.reject(
            f"受限模式仅允许白名单命令,{name} 不在允许范围内"
        )

    if name == "git":
        verdict = _check_git(tokens)
        if not verdict.allowed:
            return verdict
    if name in ("sed", "awk", "gawk"):
        verdict = _check_sed_awk(tokens)
        if not verdict.allowed:
            return verdict
    if name == "find":
        verdict = _check_find(tokens)
        if not verdict.allowed:
            return verdict
    if name in _VERIFY_COMMANDS:
        verdict = _check_install(tokens)
        if not verdict.allowed:
            return verdict
    if name in _PATH_MUTATING_COMMANDS:
        if tmp_root is None:
            return ShellVerdict.reject(f"只读模式禁止变更文件的命令: {name}")
        verdict = _check_tmp_paths(tokens, tmp_root)
        if not verdict.allowed:
            return verdict
    return ShellVerdict.ok()


def check_shell_command(
    command: str,
    access: ShellAccess,
    *,
    tmp_root: Path | None = None,
) -> ShellVerdict:
    """在执行前校验 shell 命令是否符合访问级别。

    FULL 直接放行(仍由 permission 裁决);其余档位先做结构检查(重定向、heredoc、
    命令替换),再逐段校验命令白名单与命令特有的写入形态。
    """
    if access is ShellAccess.FULL:
        return ShellVerdict.ok()
    if not command.strip():
        return ShellVerdict.reject("命令为空")

    # macOS 的 /tmp 是 /private/tmp 的符号链接,两侧都需解析后再比较,
    # 否则同一目录会因解析不对称被误判为越界。
    normalized_tmp = tmp_root.resolve(strict=False) if tmp_root is not None else None

    if _COMMAND_SUBSTITUTION_RE.search(command):
        return ShellVerdict.reject("受限模式禁止命令替换($(...) 或反引号)")

    has_redirect = _OUTPUT_REDIRECT_RE.search(command) is not None
    if has_redirect:
        if access is ShellAccess.READ_ONLY:
            return ShellVerdict.reject("只读模式禁止输出重定向与 heredoc 写文件")
        if "<<" in command:
            return ShellVerdict.reject("受限模式禁止 heredoc")
        if normalized_tmp is None:
            return ShellVerdict.reject("未配置可写临时目录,拒绝重定向")
        verdict = _check_redirect_targets(command, normalized_tmp)
        if not verdict.allowed:
            return verdict

    effective_tmp = normalized_tmp if access is ShellAccess.TMP_WRITABLE else None
    for segment in _split_segments(command):
        verdict = _check_segment(segment, access, effective_tmp)
        if not verdict.allowed:
            return verdict
    return ShellVerdict.ok()
