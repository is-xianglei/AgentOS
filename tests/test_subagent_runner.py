"""子代理运行时测试:权限坍缩、轮次上限、shell 档位透传与记忆隔离。

用 Fake LLM 与 Fake ToolService 驱动 ReAct 循环,断言的是执行边界的实际行为,
而非提示词内容:提示词能被模型忽略,执行边界不能。
"""

from types import SimpleNamespace
from typing import Any

import pytest

import database.engine as database_engine
import database.registry  # noqa: F401
import runtime.subagent as subagent_runtime
import team.service as team_service_module
from runtime.agent import AgentRuntime
from runtime.subagent import SubAgentRunner
from tools.registry import build_tool_registry
from tools.shell_policy import ShellAccess
from tools.subagents.definition import AgentType, SubAgentSpec
from tools.subagents.registry import SUBAGENT_SPECS


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


class _ToolUse:
    """模拟 llm.types 解析出的 tool_use 结构。"""

    def __init__(self, name: str, tool_id: str = "tu_1", tool_input: dict[str, Any] | None = None):
        self.name = name
        self.id = tool_id
        self.input = tool_input or {}


class _FakeToolService:
    """记录每次执行的工具名与 shell 档位,不真正执行。"""

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    async def run(
        self, session_id: int, tool_name: str, input_args: dict[str, Any], **kwargs: Any
    ) -> str:
        self.calls.append({"tool": tool_name, **kwargs})
        return f"{tool_name} 执行完毕"


class _FakePermissionService:
    """按预置映射返回判定,并记录是否带上了 agent_type。"""

    def __init__(self, behaviors: dict[str, str] | None = None):
        self.behaviors = behaviors or {}
        self.seen: list[tuple[str, str | None]] = []

    async def evaluate(
        self,
        session_id: int,
        tool_name: str,
        tool_input: dict[str, Any] | None = None,
        agent_type: str | None = None,
    ) -> str:
        self.seen.append((tool_name, agent_type))
        return self.behaviors.get(tool_name, "allow")


class _FakeDB:
    """ReAct 循环只在轮末 flush,测试里无需真实事务。"""

    def __init__(self) -> None:
        self.flushes = 0

    async def flush(self) -> None:
        self.flushes += 1


def _runner(
    permission: _FakePermissionService | None = None,
) -> tuple[SubAgentRunner, _FakeToolService, _FakePermissionService]:
    """构造只装了 Fake 依赖的 runner,不走 __init__ 的真实连接。"""
    runner = SubAgentRunner.__new__(SubAgentRunner)
    runner.db = _FakeDB()  # type: ignore[assignment]
    runner.bus = None
    perm = permission or _FakePermissionService()
    runner.permission_service = perm  # type: ignore[assignment]
    return runner, _FakeToolService(), perm


def _spec(**overrides: Any) -> SubAgentSpec:
    base: dict[str, Any] = {
        "agent_type": AgentType.EXPLORE,
        "system_prompt": "x",
        "shell_access": ShellAccess.READ_ONLY,
    }
    base.update(overrides)
    return SubAgentSpec(**base)


class TestParentAgentBoundary:
    def test_子代理工具集先与父Agent白名单求交(self) -> None:
        spec = _spec(agent_type=AgentType.GENERAL_PURPOSE, shell_access=ShellAccess.FULL)

        registry = SubAgentRunner._resolve_tools(
            spec,
            frozenset({"Agent", "Read"}),
        )

        assert registry.names == {"Read"}

    def test_默认Agent仍按原有全量注册表解析(self) -> None:
        spec = _spec(agent_type=AgentType.GENERAL_PURPOSE, shell_access=ShellAccess.FULL)

        unrestricted = SubAgentRunner._resolve_tools(spec, None)
        inherited_default = SubAgentRunner._resolve_tools(
            spec,
            build_tool_registry().names,
        )

        assert inherited_default.names == unrestricted.names
        assert {"Read", "Write", "Bash", "Skill"}.issubset(inherited_default.names)

    @pytest.mark.anyio
    async def test_Skill白名单透传到子代理工具执行边界(self) -> None:
        runner, tools, _ = _runner()
        allowed = frozenset({"approved-skill"})

        await runner._execute_tool_uses(
            1,
            None,
            [_ToolUse("Skill")],
            [],
            tools,
            _spec(),
            None,
            None,
            3,
            allowed_skill_names=allowed,
        )

        assert tools.calls[0]["allowed_skill_names"] == allowed

    @pytest.mark.anyio
    async def test_队友唤醒继承父Agent工具和Skill边界(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        calls: list[dict[str, Any]] = []

        class _SessionContext:
            async def __aenter__(self) -> object:
                return object()

            async def __aexit__(self, *args: object) -> None:
                return None

        class _TeamService:
            async def list_members(self, session_id: int) -> list[SimpleNamespace]:
                return [
                    SimpleNamespace(
                        id=8,
                        name="reviewer",
                        status="working",
                        agent_type="general-purpose",
                    )
                ]

        class _SubAgentRunner:
            def __init__(self, db: object, bus: object = None) -> None:
                pass

            async def run_teammate(self, *args: object, **kwargs: Any) -> str:
                calls.append(kwargs)
                return "完成"

        monkeypatch.setattr(database_engine, "AsyncSessionLocal", _SessionContext)
        monkeypatch.setattr(team_service_module, "TeamService", lambda db: _TeamService())
        monkeypatch.setattr(subagent_runtime, "SubAgentRunner", _SubAgentRunner)

        runtime = AgentRuntime.__new__(AgentRuntime)
        runtime.db = object()  # type: ignore[assignment]
        runtime.bus = None  # type: ignore[assignment]
        runtime.tool_registry = build_tool_registry().only("Agent", "Read", "Skill")
        runtime.allowed_skill_names = frozenset({"workspace-skill"})
        turn = SimpleNamespace(id=None, user_id=5, workspace_id=3)

        await runtime._wake_team_members(7, turn)

        assert calls[0]["parent_allowed_tool_names"] == runtime.tool_registry.names
        assert calls[0]["allowed_skill_names"] == runtime.allowed_skill_names


@pytest.mark.anyio
class TestPermissionEnforcement:
    async def test_放行时才真正执行工具(self) -> None:
        runner, tools, _ = _runner()
        messages: list[dict[str, Any]] = []
        await runner._execute_tool_uses(
            1, None, [_ToolUse("Read")], messages, tools, _spec(), None, None, None
        )
        assert [c["tool"] for c in tools.calls] == ["Read"]
        assert "执行完毕" in messages[0]["content"][0]["content"]

    async def test_裁决必须带上agent_type(self) -> None:
        """不传 agent_type 就用不到定向规则,verification 会被默认策略挡死。"""
        runner, tools, perm = _runner()
        spec = _spec(agent_type=AgentType.VERIFICATION)
        await runner._execute_tool_uses(
            1, None, [_ToolUse("Bash")], [], tools, spec, None, None, None
        )
        assert perm.seen == [("Bash", "verification")]

    async def test_deny时不执行工具(self) -> None:
        runner, tools, _ = _runner(_FakePermissionService({"Bash": "deny"}))
        messages: list[dict[str, Any]] = []
        await runner._execute_tool_uses(
            1, None, [_ToolUse("Bash")], messages, tools, _spec(), None, None, None
        )
        assert tools.calls == []
        assert "已被权限策略禁止" in messages[0]["content"][0]["content"]

    async def test_ask坍缩为拒绝且不执行(self) -> None:
        """子代理没有审批挂起通道,ask 不能当作放行,否则成为绕过审批的提权路径。"""
        runner, tools, _ = _runner(_FakePermissionService({"Bash": "ask"}))
        messages: list[dict[str, Any]] = []
        await runner._execute_tool_uses(
            1, None, [_ToolUse("Bash")], messages, tools, _spec(), None, None, None
        )
        assert tools.calls == []
        content = messages[0]["content"][0]["content"]
        assert "无审批通道" in content
        assert "error:" in content

    async def test_拒绝也要回tool_result保持消息配对(self) -> None:
        """少一条 tool_result 会让下一轮请求结构非法,模型侧直接报错。"""
        runner, tools, _ = _runner(_FakePermissionService({"Bash": "deny"}))
        messages: list[dict[str, Any]] = []
        uses = [_ToolUse("Bash", "tu_a"), _ToolUse("Read", "tu_b")]
        await runner._execute_tool_uses(1, None, uses, messages, tools, _spec(), None, None, None)
        assert len(messages) == 2
        assert [m["content"][0]["tool_use_id"] for m in messages] == ["tu_a", "tu_b"]
        # 被拒的不执行,放行的照常执行。
        assert [c["tool"] for c in tools.calls] == ["Read"]


@pytest.mark.anyio
class TestShellAccessThreading:
    async def test_shell档位随每次工具执行透传(self) -> None:
        """档位不落到 ToolContext,BashTool 就无从校验,只读约束等于没有。"""
        runner, tools, _ = _runner()
        spec = _spec(agent_type=AgentType.VERIFICATION, shell_access=ShellAccess.TMP_WRITABLE)
        await runner._execute_tool_uses(
            1, None, [_ToolUse("Bash")], [], tools, spec, None, None, None
        )
        call = tools.calls[0]
        assert call["shell_access"] is ShellAccess.TMP_WRITABLE
        assert call["shell_tmp_root"] == spec.shell_tmp_root()

    async def test_只读档位不带可写目录(self) -> None:
        runner, tools, _ = _runner()
        await runner._execute_tool_uses(
            1, None, [_ToolUse("Bash")], [], tools, _spec(), None, None, None
        )
        assert tools.calls[0]["shell_access"] is ShellAccess.READ_ONLY
        assert tools.calls[0]["shell_tmp_root"] is None

    async def test_full档位照常透传(self) -> None:
        runner, tools, _ = _runner()
        spec = _spec(agent_type=AgentType.GENERAL_PURPOSE, shell_access=ShellAccess.FULL)
        await runner._execute_tool_uses(
            1, None, [_ToolUse("Bash")], [], tools, spec, None, None, None
        )
        assert tools.calls[0]["shell_access"] is ShellAccess.FULL


class TestDeniedMessage:
    def test_区分显式拒绝与无通道坍缩(self) -> None:
        """两种原因要给模型不同的改道提示,不能混成一句。"""
        ask_msg = SubAgentRunner._denied_message("Bash", "ask")
        deny_msg = SubAgentRunner._denied_message("Bash", "deny")
        assert ask_msg != deny_msg
        assert "无审批通道" in ask_msg
        assert "主代理" in ask_msg
        assert "权限策略禁止" in deny_msg

    def test_原因里带工具名(self) -> None:
        assert "SkillRun" in SubAgentRunner._denied_message("SkillRun", "deny")

    def test_原因以error开头便于模型识别(self) -> None:
        for behavior in ("ask", "deny"):
            assert SubAgentRunner._denied_message("Bash", behavior).startswith("error:")


class _StubTranslator:
    """吞掉全部 chunk,不产出事件。"""

    stop_reason = "end_turn"
    last_usage = None

    def translate(self, chunk: dict[str, Any]) -> list[Any]:
        return []


def _install_fake_stream(
    runner: SubAgentRunner,
    scripted: list[list[dict[str, Any]]],
    seen_models: list[str | None],
) -> None:
    """把 _stream_and_translate 换成按脚本逐轮返回 final_content 的假实现。"""

    async def fake_stream(
        messages: list[dict[str, Any]],
        system_prompt: str,
        tool_registry: Any,
        translator: Any,
        rendered_memories: str | None = None,
        model: str | None = None,
    ) -> list[dict[str, Any]]:
        seen_models.append(model)
        index = min(len(seen_models) - 1, len(scripted) - 1)
        return scripted[index]

    runner._stream_and_translate = fake_stream  # type: ignore[method-assign]


def _tool_content(tool_id: str = "tu_1") -> list[dict[str, Any]]:
    """一轮"还要继续调工具"的模型输出。"""
    return [{"type": "tool_use", "id": tool_id, "name": "Read", "input": {"file_path": "a.py"}}]


def _text_content(text: str) -> list[dict[str, Any]]:
    """一轮"给出结论"的模型输出。"""
    return [{"type": "text", "text": text}]


@pytest.mark.anyio
class TestRoundBudgetEnforcement:
    async def test_无工具调用时立即返回结论(self) -> None:
        runner, tools, _ = _runner()
        _install_fake_stream(runner, [_text_content("结论正文")], [])
        report = await runner._run_react_loop(
            1, None, _StubTranslator(), "任务", "sys", None, None, tools, _spec(), None, None, None
        )
        assert report == "结论正文"

    async def test_轮次上限取自spec而非硬编码(self) -> None:
        """原实现写死 6 轮,verification 需要 24 轮,写死会让它结构上无法完成职责。"""
        runner, tools, _ = _runner()
        models: list[str | None] = []
        _install_fake_stream(runner, [_tool_content()], models)
        spec = _spec(max_rounds=3)
        await runner._run_react_loop(
            1, None, _StubTranslator(), "任务", "sys", None, None, tools, spec, None, None, None
        )
        assert len(models) == 3
        assert len(tools.calls) == 3

    async def test_verification的轮次预算被真正使用(self) -> None:
        runner, tools, _ = _runner()
        models: list[str | None] = []
        _install_fake_stream(runner, [_tool_content()], models)
        spec = SUBAGENT_SPECS[AgentType.VERIFICATION]
        await runner._run_react_loop(
            1, None, _StubTranslator(), "任务", "sys", None, None, tools, spec, None, None, None
        )
        assert len(models) == spec.max_rounds
        assert spec.max_rounds >= 20

    async def test_轮次耗尽返回截断报告而不抛异常(self) -> None:
        """抛异常会让整轮子代理白跑;截断报告仍能让主代理判断并补做。"""
        runner, tools, _ = _runner()
        _install_fake_stream(
            runner,
            [[*_text_content("已查到关键实现"), *_tool_content()]],
            [],
        )
        report = await runner._run_react_loop(
            1,
            None,
            _StubTranslator(),
            "任务",
            "sys",
            None,
            None,
            tools,
            _spec(max_rounds=2),
            None,
            None,
            None,
        )
        assert "上限" in report
        assert "已查到关键实现" in report

    async def test_耗尽且无任何文本时也返回可读提示(self) -> None:
        runner, tools, _ = _runner()
        _install_fake_stream(runner, [_tool_content()], [])
        report = await runner._run_react_loop(
            1,
            None,
            _StubTranslator(),
            "任务",
            "sys",
            None,
            None,
            tools,
            _spec(max_rounds=1),
            None,
            None,
            None,
        )
        assert report.strip()
        assert "上限" in report

    async def test_轮次配置为0时至少跑一轮(self) -> None:
        """配置写成 0 不该让子代理直接空转返回,兜底跑一轮。"""
        runner, tools, _ = _runner()
        models: list[str | None] = []
        _install_fake_stream(runner, [_text_content("结论")], models)
        await runner._run_react_loop(
            1,
            None,
            _StubTranslator(),
            "任务",
            "sys",
            None,
            None,
            tools,
            _spec(max_rounds=0),
            None,
            None,
            None,
        )
        assert len(models) == 1


@pytest.mark.anyio
class TestModelThreading:
    async def test_spec的模型档位传到每轮请求(self) -> None:
        """Explore 定了速度档位,若不透传就还是用主会话模型,档位形同虚设。"""
        runner, tools, _ = _runner()
        models: list[str | None] = []
        _install_fake_stream(runner, [_text_content("done")], models)
        spec = _spec(model="claude-haiku-4-5-20251001")
        await runner._run_react_loop(
            1, None, _StubTranslator(), "任务", "sys", None, None, tools, spec, None, None, None
        )
        assert models == ["claude-haiku-4-5-20251001"]

    async def test_未指定模型时透传None以继承会话模型(self) -> None:
        runner, tools, _ = _runner()
        models: list[str | None] = []
        _install_fake_stream(runner, [_text_content("done")], models)
        await runner._run_react_loop(
            1,
            None,
            _StubTranslator(),
            "任务",
            "sys",
            None,
            None,
            tools,
            _spec(model=None),
            None,
            None,
            None,
        )
        assert models == [None]


class TestMemoryContextIsolation:
    def test_记忆只进请求副本不改原始消息(self) -> None:
        """teammate 的 history 要落库,注入正文会让长期记忆被反复写进历史。"""
        messages = [{"role": "user", "content": "原始任务"}]
        merged = SubAgentRunner._with_memory_context(messages, "记忆正文")
        assert messages[0]["content"] == "原始任务"
        assert "记忆正文" in merged[0]["content"]
        assert "原始任务" in merged[0]["content"]

    def test_无记忆时原样返回(self) -> None:
        messages = [{"role": "user", "content": "任务"}]
        assert SubAgentRunner._with_memory_context(messages, None)[0]["content"] == "任务"
        assert SubAgentRunner._with_memory_context(messages, "")[0]["content"] == "任务"

    def test_不注入到identity消息(self) -> None:
        """identity 是 teammate 的人格头,前置记忆会破坏其识别。"""
        messages = [
            {"role": "user", "content": "<identity>You are 'a'</identity>"},
            {"role": "user", "content": "真实任务"},
        ]
        merged = SubAgentRunner._with_memory_context(messages, "记忆正文")
        assert merged[0]["content"].startswith("<identity>")
        assert "记忆正文" in merged[1]["content"]

    def test_只注入一次(self) -> None:
        messages = [
            {"role": "user", "content": "第一条"},
            {"role": "user", "content": "第二条"},
        ]
        merged = SubAgentRunner._with_memory_context(messages, "记忆正文")
        assert sum("记忆正文" in m["content"] for m in merged) == 1
