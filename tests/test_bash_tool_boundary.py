"""BashTool 执行边界测试:命令校验前置与工作目录约束。

覆盖的是"策略真的拦在 bash -c 之前"这一点:命令未通过校验时不得进入执行器,
且受限档位下不能靠指定 cwd 把只读面扩到项目外。
"""

from pathlib import Path

import pytest

import database.registry  # noqa: F401
from core.errors import AgentException
from tools.builtin.bash import BashInput, BashTool
from tools.shell_policy import ShellAccess


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


class _Ctx:
    """ToolContext 替身,只带 BashTool 用到的字段。"""

    def __init__(
        self,
        shell_access: ShellAccess = ShellAccess.READ_ONLY,
        shell_tmp_root: Path | None = None,
    ):
        self.session_id = 1
        self.shell_access = shell_access
        self.shell_tmp_root = shell_tmp_root
        self.db = None
        self.bus = None
        self.turn_id = None
        self.user_id = None
        self.workspace_id = None


class TestResolveWorkdir:
    """工作目录约束:命令白名单管"执行什么",这里管"在哪执行"。"""

    def test_未指定时回落到当前目录(self):
        assert BashTool._resolve_workdir(None, _Ctx()) == Path.cwd()

    def test_full档位不限制目录(self):
        # 断言用 resolve() 后的值:macOS 上 /etc 是指向 /private/etc 的符号链接。
        ctx = _Ctx(shell_access=ShellAccess.FULL)
        assert BashTool._resolve_workdir("/etc", ctx) == Path("/etc").resolve()

    def test_只读档位允许项目内目录(self):
        target = Path.cwd() / "tools"
        assert BashTool._resolve_workdir(str(target), _Ctx()) == target.resolve()

    def test_只读档位允许项目根本身(self):
        assert BashTool._resolve_workdir(str(Path.cwd()), _Ctx()) == Path.cwd().resolve()

    def test_只读档位拒绝项目外目录(self):
        with pytest.raises(AgentException, match="工作目录越界"):
            BashTool._resolve_workdir("/etc", _Ctx())

    def test_只读档位拒绝家目录(self):
        # ~ 展开后仍在项目外,不能因为是自己的家目录就放行。
        with pytest.raises(AgentException, match="工作目录越界"):
            BashTool._resolve_workdir("~", _Ctx())

    def test_相对路径上跳被解析后拦下(self):
        # resolve() 先归一化再判定,`tools/../..` 不会因为字面量看起来在项目内而漏过。
        with pytest.raises(AgentException, match="工作目录越界"):
            BashTool._resolve_workdir(str(Path.cwd() / "tools" / ".." / ".."), _Ctx())

    def test_tmp档位允许自己的临时目录(self, tmp_path: Path):
        ctx = _Ctx(shell_access=ShellAccess.TMP_WRITABLE, shell_tmp_root=tmp_path)
        nested = tmp_path / "sub"
        assert BashTool._resolve_workdir(str(nested), ctx) == nested.resolve()

    def test_tmp档位不允许别的临时目录(self, tmp_path: Path):
        ctx = _Ctx(shell_access=ShellAccess.TMP_WRITABLE, shell_tmp_root=tmp_path / "mine")
        with pytest.raises(AgentException, match="工作目录越界"):
            BashTool._resolve_workdir(str(tmp_path / "other"), ctx)

    def test_未配置临时目录时只剩项目根(self):
        ctx = _Ctx(shell_access=ShellAccess.TMP_WRITABLE, shell_tmp_root=None)
        with pytest.raises(AgentException, match="工作目录越界"):
            BashTool._resolve_workdir("/tmp", ctx)


class _FakeExecutor:
    """执行器替身:只记录是否被调用过。"""

    def __init__(self):
        self.calls: list[dict] = []

    async def run(self, **kwargs) -> object:
        self.calls.append(kwargs)

        class _Result:
            stdout = "ok"
            stderr = ""
            exit_code = 0
            timed_out = False
            truncated = False

        return _Result()


@pytest.fixture
def fake_executor(monkeypatch: pytest.MonkeyPatch) -> _FakeExecutor:
    executor = _FakeExecutor()
    monkeypatch.setattr("tools.builtin.bash.get_executor", lambda: executor)
    return executor


@pytest.mark.anyio
class TestPolicyPrecedesExecution:
    """策略必须拦在 bash -c 之前,而不是执行完再判定。"""

    async def test_被拒命令不进入执行器(self, fake_executor: _FakeExecutor):
        out = await BashTool().run(BashInput(command="rm -rf /tmp/x"), _Ctx())
        assert out.startswith("error:")
        assert fake_executor.calls == []

    async def test_full档位放行同一命令(self, fake_executor: _FakeExecutor):
        await BashTool().run(
            BashInput(command="rm -rf /tmp/x"),
            _Ctx(shell_access=ShellAccess.FULL),
        )
        assert len(fake_executor.calls) == 1

    async def test_只读命令正常执行(self, fake_executor: _FakeExecutor):
        await BashTool().run(BashInput(command="ls -la"), _Ctx())
        assert len(fake_executor.calls) == 1

    async def test_命令合规但目录越界仍被拦(self, fake_executor: _FakeExecutor):
        # `ls` 本身合规,越界发生在 cwd 上。这里抛业务异常而非回灌 error 文本:
        # 命令被拒是模型可改道的输入错误,目录越界属调用方越权,应中断本次调用。
        with pytest.raises(AgentException, match="越界"):
            await BashTool().run(BashInput(command="ls", cwd="/etc"), _Ctx())
        assert fake_executor.calls == []

    async def test_越界目录不会被静默改写为项目根(self, fake_executor: _FakeExecutor):
        # 关键点:不是"回退到项目根后照跑",而是整条调用失败。
        with pytest.raises(AgentException):
            await BashTool().run(BashInput(command="ls", cwd="/etc"), _Ctx())
        assert all(call.get("workdir") != str(Path.cwd()) for call in fake_executor.calls)
