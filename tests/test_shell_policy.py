"""shell 访问策略测试:只读与 tmp 可写两档的放行与拦截边界。

这是子代理只读约束的实际执行点(不是提示词自律),因此重点覆盖各类绕过手法:
重定向、heredoc、命令替换、管道分段、子 shell、环境变量前缀、解释器与路径逃逸。
"""

from pathlib import Path

import pytest

from tools.shell_policy import ShellAccess, check_shell_command

TMP_ROOT = Path("/tmp/agentos-verification")


def _ro(command: str) -> bool:
    """只读档位下是否放行。"""
    return check_shell_command(command, ShellAccess.READ_ONLY).allowed


def _tmp(command: str) -> bool:
    """tmp 可写档位下是否放行。"""
    return check_shell_command(command, ShellAccess.TMP_WRITABLE, tmp_root=TMP_ROOT).allowed


class TestFullAccess:
    def test_full_access_bypasses_all_checks(self) -> None:
        """FULL 档位不做静态校验,交由 permission 裁决。"""
        for command in ("rm -rf /", "sudo reboot", "curl x | sh", "git push"):
            assert check_shell_command(command, ShellAccess.FULL).allowed


class TestReadOnlyAllows:
    @pytest.mark.parametrize(
        "command",
        [
            "ls -la",
            "cat pyproject.toml",
            "grep -rn 'def run' tools/",
            "rg --line-number ToolContext",
            "git status",
            "git diff HEAD~1",
            "git log --oneline -5",
            "wc -l tools/service.py",
            "find . -name '*.py' -type f",
            "sed -n '1,20p' main.py",
            "head -50 README.md | wc -l",
        ],
    )
    def test_read_only_commands_allowed(self, command: str) -> None:
        assert _ro(command), f"应放行: {command}"

    def test_解释器即使只查版本也被拒(self) -> None:
        """白名单无法区分 `python --version` 与 `python -c '...'`,一律拒绝。
        误拒代价是子代理换个命令,放行代价是任意代码执行。
        """
        assert not _ro("python --version")
        assert not _ro("node -v")


class TestReadOnlyBlocksMutation:
    @pytest.mark.parametrize(
        "command",
        [
            "rm -rf tools/",
            "cp a.py b.py",
            "mv a.py b.py",
            "mkdir newdir",
            "touch newfile.py",
            "chmod +x script.sh",
            "ln -s a b",
            "truncate -s 0 log.txt",
        ],
    )
    def test_path_mutating_commands_denied(self, command: str) -> None:
        assert not _ro(command), f"应拒绝: {command}"

    @pytest.mark.parametrize(
        "command",
        [
            "echo hi > out.txt",
            "echo hi >> out.txt",
            "cat a.py > b.py",
            "ls 2> err.log",
            "ls &> all.log",
            "cat <<EOF\nx\nEOF",
        ],
    )
    def test_redirect_and_heredoc_denied(self, command: str) -> None:
        """只读档位一律禁重定向:写文件不看目标在哪都越界。"""
        assert not _ro(command), f"应拒绝: {command}"

    @pytest.mark.parametrize(
        "command",
        [
            "echo $(rm -rf x)",
            "echo `rm -rf x`",
            "ls ${x:-$(whoami)}",
        ],
    )
    def test_command_substitution_denied(self, command: str) -> None:
        """命令替换能把任意命令藏进看似只读的外层,一律拒绝。"""
        assert not _ro(command), f"应拒绝: {command}"


class TestReadOnlyBlocksEscapes:
    @pytest.mark.parametrize(
        "command",
        [
            "ls && rm -rf x",
            "ls || rm -rf x",
            "ls; rm -rf x",
            "ls | xargs rm",
            "cat a.py | tee b.py",
            "ls\nrm -rf x",
        ],
    )
    def test_每个分段都要单独校验(self, command: str) -> None:
        """只校验首个命令会被 `ls && rm` 直接绕过,必须逐段判。"""
        assert not _ro(command), f"应拒绝: {command}"

    @pytest.mark.parametrize(
        "command",
        [
            "(rm -rf x)",
            "FOO=bar rm -rf x",
            "FOO=bar BAZ=qux rm x",
        ],
    )
    def test_子shell与环境变量前缀不能掩盖命令(self, command: str) -> None:
        assert not _ro(command), f"应拒绝: {command}"

    @pytest.mark.parametrize(
        "command",
        [
            "sudo ls",
            "su root",
            "eval 'rm -rf x'",
            "exec rm x",
            "source evil.sh",
            "bash -c 'rm -rf x'",
            "sh evil.sh",
            "python -c 'import os; os.remove(\"x\")'",
            "wget http://x/y",
            "curl http://x/y",
            "docker run x",
            "kubectl delete pod x",
        ],
    )
    def test_解释器与提权命令在只读档位被拒(self, command: str) -> None:
        assert not _ro(command), f"应拒绝: {command}"


class TestSubcommandGranularity:
    @pytest.mark.parametrize(
        "command",
        ["git status", "git diff", "git log", "git show HEAD", "git branch --list"],
    )
    def test_git_只读子命令放行(self, command: str) -> None:
        assert _ro(command)

    @pytest.mark.parametrize(
        "command",
        [
            "git commit -m x",
            "git push",
            "git reset --hard",
            "git checkout main",
            "git clean -fd",
            "git rebase main",
            "git config user.name x",
        ],
    )
    def test_git_写子命令被拒(self, command: str) -> None:
        """git 不能整体放行:同一个二进制既能读也能改历史与工作区。"""
        assert not _ro(command), f"应拒绝: {command}"

    @pytest.mark.parametrize(
        ("command", "expected"),
        [
            ("git branch", True),
            ("git branch --list", True),
            ("git branch -a", True),
            ("git branch feature-x", False),
            ("git branch -D old", False),
            ("git branch -m a b", False),
            ("git tag", True),
            ("git tag -l", True),
            ("git tag v1.0", False),
            ("git remote", True),
            ("git remote add origin url", False),
            ("git stash", False),
        ],
    )
    def test_读写两用子命令按形态判定(self, command: str, expected: bool) -> None:
        """`git branch feature-x` 不带标志就能建分支,故位置参数也要计入判定。"""
        assert _ro(command) is expected, f"{command} 期望 allowed={expected}"

    def test_sed_原地编辑被拒而打印放行(self) -> None:
        assert _ro("sed -n '1,5p' main.py")
        assert not _ro("sed -i 's/a/b/' main.py")
        assert not _ro("sed --in-place=.bak 's/a/b/' main.py")

    def test_awk_system_调用被拒(self) -> None:
        assert _ro("awk '{print $1}' data.txt")
        assert not _ro("awk 'BEGIN{system(\"rm x\")}'")

    def test_find_删除与执行动作被拒(self) -> None:
        assert _ro("find . -name '*.py'")
        assert not _ro("find . -name '*.py' -delete")
        assert not _ro("find . -name '*.py' -exec rm {} ;")

    @pytest.mark.parametrize(
        "command",
        ["pip install x", "uv add x", "npm install", "uv pip install -e ."],
    )
    def test_装包命令在两档都被拒(self, command: str) -> None:
        """装包会改环境且常触网,即使 tmp 可写也不放行。"""
        assert not _ro(command)
        assert not _tmp(command)


class TestTmpWritable:
    @pytest.mark.parametrize(
        "command",
        [
            "uv run pytest -q",
            "uv run pytest tests/test_prompt.py -v",
            "uv run ruff check .",
            "uv run mypy tools/",
            "uv run alembic heads",
            "python -m pytest -q",
            "make test",
            "timeout 60 uv run pytest -q",
        ],
    )
    def test_验收命令在tmp档位放行(self, command: str) -> None:
        """verification 必须真跑测试与 lint 才能给判定,这些命令是其职责所需。"""
        assert _tmp(command), f"应放行: {command}"

    def test_验收命令在只读档位被拒(self) -> None:
        """只读档位不该能跑测试运行器:它们能在进程内写盘。"""
        assert not _ro("uv run pytest -q")
        assert not _ro("python -m pytest")

    def test_tmp_目录内可写(self) -> None:
        assert _tmp(f"echo x > {TMP_ROOT}/probe.py")
        assert _tmp(f"mkdir -p {TMP_ROOT}/sub")
        assert _tmp(f"touch {TMP_ROOT}/a.txt")
        assert _tmp(f"rm {TMP_ROOT}/a.txt")

    def test_tmp档位仍不能写项目目录(self) -> None:
        """放开 tmp 不等于放开工作区,越界目标要拦住。"""
        assert not _tmp("echo x > tools/hacked.py")
        assert not _tmp("rm -rf tools/")
        assert not _tmp("touch ./inproject.txt")

    def test_路径逃逸被拦(self) -> None:
        """`..` 拼接能从 tmp 走回任意目录,须按解析后的真实路径判定。"""
        assert not _tmp(f"rm -rf {TMP_ROOT}/../../etc/passwd")
        assert not _tmp(f"echo x > {TMP_ROOT}/../escape.txt")

    def test_heredoc在tmp档位也被拒(self) -> None:
        """heredoc 目标难以静态判定,统一拒绝而不是猜。"""
        assert not _tmp(f"cat <<EOF > {TMP_ROOT}/x.py\nprint(1)\nEOF")

    def test_未配置tmp目录时重定向被拒(self) -> None:
        """档位是 tmp 可写但没给根目录,说明配置缺失,按拒绝处理而非放行。"""
        verdict = check_shell_command("echo x > /tmp/a.txt", ShellAccess.TMP_WRITABLE)
        assert not verdict.allowed


class TestVerdictShape:
    def test_拒绝时必须带中文原因(self) -> None:
        """原因会作为 tool_result 回给模型,必须可读、能指导改道。"""
        verdict = check_shell_command("rm -rf x", ShellAccess.READ_ONLY)
        assert not verdict.allowed
        assert verdict.reason
        assert any("一" <= ch <= "鿿" for ch in verdict.reason)

    def test_放行时不带原因(self) -> None:
        verdict = check_shell_command("ls", ShellAccess.READ_ONLY)
        assert verdict.allowed
        assert verdict.reason is None

    def test_空命令被拒(self) -> None:
        assert not _ro("")
        assert not _ro("   ")

    def test_引号未闭合被拒(self) -> None:
        """无法可靠切词就不能断言安全,按拒绝处理。"""
        assert not _ro("grep 'unclosed")
