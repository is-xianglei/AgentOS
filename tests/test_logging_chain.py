"""问答链路日志回归测试(AC5 派生执行层)。

锁死两条本次任务新补的日志语义,全程 Fake/monkeypatch,不碰真实数据库与 LLM:
- teammate 唤醒失败时 `_wake_team_members` 必须留下 logger.exception 现场
  (客户端断连后 SSE error 事件无人接收,服务端日志是唯一凭据);
- CompactService 触发/跳过压缩时必须输出含 token 估算的中文日志。
"""

import logging
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Any, Self
from uuid import UUID, uuid4

import pytest

from core.event_bus import StreamBus
from llm.types import LLMEventKind, LLMStreamEvent, LLMTurnResult
from runtime.agent import AgentRuntime
from runtime.compact import CompactService

pytestmark = pytest.mark.anyio

_SID = 42


# ---------- 用例组 1:teammate 唤醒失败必须留下 logger.exception ----------


@dataclass
class _FakeMember:
    """仅覆盖 _wake_team_members 用到的团队成员字段。"""

    id: int
    name: str
    agent_type: str = "general-purpose"
    status: str = "working"


@dataclass
class _FakeTurn:
    """仅覆盖 _wake_team_members 用到的回合字段。"""

    id: UUID = field(default_factory=uuid4)
    user_id: int = 1
    workspace_id: int = 1


@dataclass
class _FakeRegistry:
    """替身工具注册表,只提供传给子代理的 names 边界。"""

    names: frozenset[str] = frozenset()


class _FakeSessionScope:
    """替身 AsyncSessionLocal:进出上下文都不建立数据库连接。"""

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(self, *exc_info: object) -> bool:
        return False


def _make_runtime() -> AgentRuntime:
    """绕过 __init__ 组装 _wake_team_members 所需的最小运行时,避免建真实 LLM 客户端。"""
    runtime = AgentRuntime.__new__(AgentRuntime)
    runtime.db = object()
    runtime.bus = StreamBus()
    runtime.tool_registry = _FakeRegistry()
    runtime.allowed_skill_names = None
    return runtime


def _install_wake_fakes(
    monkeypatch: pytest.MonkeyPatch,
    members: list[_FakeMember],
    failing_names: set[str],
) -> list[str]:
    """替换 _wake_team_members 函数内导入的三处依赖,返回唤醒调用顺序记录。"""
    calls: list[str] = []

    class _FakeTeamService:
        def __init__(self, db: object) -> None:
            pass

        async def list_members(self, session_id: int) -> list[_FakeMember]:
            return members

    class _FakeSubAgentRunner:
        def __init__(self, db: object, bus: object | None = None) -> None:
            pass

        async def run_teammate(
            self,
            session_id: int,
            member_name: str,
            spec: object,
            **kwargs: Any,
        ) -> str:
            calls.append(member_name)
            if member_name in failing_names:
                raise RuntimeError("唤醒链路故障注入")
            return "推进完成"

    monkeypatch.setattr("runtime.agent.TeamService", _FakeTeamService)
    monkeypatch.setattr("runtime.agent.AsyncSessionLocal", _FakeSessionScope)
    monkeypatch.setattr("runtime.agent.SubAgentRunner", _FakeSubAgentRunner)
    return calls


async def test_wake_failure_logs_exception_with_context(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """R6.2:唤醒失败落 logger.exception,带 session_id/turn_id/member_name 且不上抛。"""
    member = _FakeMember(id=7, name="拆弹员")
    _install_wake_fakes(monkeypatch, [member], failing_names={"拆弹员"})
    runtime = _make_runtime()
    turn = _FakeTurn()

    with caplog.at_level(logging.ERROR, logger="runtime.agent"):
        # 正常返回本身就是断言:异常必须被隔离在方法内部,不得中断主流程。
        await runtime._wake_team_members(_SID, turn)

    failures = [r for r in caplog.records if "唤醒队友失败" in r.message]
    assert len(failures) == 1
    record = failures[0]
    assert record.session_id == _SID
    assert record.turn_id == str(turn.id)
    assert record.member_name == "拆弹员"
    # exc_info 非空证明用的是 logger.exception 而非 logger.error:断连复盘全靠堆栈。
    assert record.exc_info is not None

    # 隔离分支走完全程:error 事件也已发往总线。
    await runtime.bus.close()
    events = [event async for event in runtime.bus.stream()]
    assert [event["type"] for event in events] == ["error"]


async def test_wake_failure_is_isolated_and_continues(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """R6.2:单个队友失败被隔离后继续唤醒其余成员,且只留一条失败日志。"""
    members = [_FakeMember(id=1, name="先崩者"), _FakeMember(id=2, name="幸存者")]
    calls = _install_wake_fakes(monkeypatch, members, failing_names={"先崩者"})
    runtime = _make_runtime()

    with caplog.at_level(logging.ERROR, logger="runtime.agent"):
        await runtime._wake_team_members(_SID, _FakeTurn())

    assert calls == ["先崩者", "幸存者"]
    assert sum("唤醒队友失败" in r.message for r in caplog.records) == 1


# ---------- 用例组 2:上下文压缩触发时输出前后 token 估算 ----------


class _FakeSessionRepo:
    """会话仓储替身;through_message_id 为 None 的用例不应触碰快照写入。"""

    async def add_snapshot(self, *args: Any, **kwargs: Any) -> None:
        raise AssertionError("through_message_id=None 时不应写快照")


class _FakeCompactLLM:
    """压缩 LLM 替身:直接产出终局摘要,不发真实请求。"""

    async def stream(
        self, messages: list[dict[str, Any]], **kwargs: Any
    ) -> AsyncIterator[LLMStreamEvent]:
        yield LLMStreamEvent(
            kind=LLMEventKind.TURN_RESULT,
            result=LLMTurnResult(text="已完成事项、当前状态与关键决策摘要"),
        )


class _ForbiddenLLM:
    """未触发压缩的用例不允许任何 LLM 调用,一旦调用立即失败。"""

    def stream(self, *args: Any, **kwargs: Any) -> AsyncIterator[LLMStreamEvent]:
        raise AssertionError("未超阈值不应调用 LLM")


async def test_compact_triggered_logs_token_estimates(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """R6.3:超阈值时输出触发与完成两条 INFO,含前后 token 估算与耗时。"""
    service = CompactService(_FakeSessionRepo(), llm=_FakeCompactLLM(), threshold=1)
    messages = [{"role": "user", "content": "这是一段足够长的历史上下文" * 4}]

    with caplog.at_level(logging.INFO, logger="runtime.compact"):
        compacted = await service.maybe_compact(_SID, messages)

    triggered = [r for r in caplog.records if "上下文超过阈值" in r.message]
    finished = [r for r in caplog.records if "上下文自动压缩完成" in r.message]
    assert len(triggered) == 1
    assert len(finished) == 1
    assert triggered[0].session_id == _SID
    assert triggered[0].est_tokens_before > 1
    assert triggered[0].threshold == 1
    assert finished[0].session_id == _SID
    assert finished[0].est_tokens_before == triggered[0].est_tokens_before
    assert finished[0].est_tokens_after > 0
    assert finished[0].duration_ms >= 0
    # 压缩路径真实走通:历史被替换成单条摘要消息。
    assert len(compacted) == 1
    assert "摘要" in compacted[0]["content"]


async def test_compact_skipped_logs_debug_only(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """R6.3:未超阈值只落一条 DEBUG 跳过日志,不出现触发/完成 INFO,不调用 LLM。"""
    service = CompactService(_FakeSessionRepo(), llm=_ForbiddenLLM(), threshold=10_000)
    messages = [{"role": "user", "content": "短对话"}]

    with caplog.at_level(logging.DEBUG, logger="runtime.compact"):
        result = await service.maybe_compact(_SID, messages)

    assert result == messages
    skipped = [r for r in caplog.records if "上下文未超阈值" in r.message]
    assert len(skipped) == 1
    assert skipped[0].levelno == logging.DEBUG
    assert skipped[0].session_id == _SID
    assert skipped[0].est_tokens <= 10_000
    assert skipped[0].threshold == 10_000
    assert not any("上下文超过阈值" in r.message for r in caplog.records)
    assert not any("自动压缩完成" in r.message for r in caplog.records)
