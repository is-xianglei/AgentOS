"""Memory Phase 0 的会话上下文与可靠性回归测试。"""

import asyncio
import logging
from types import SimpleNamespace
from uuid import uuid4

import pytest

import hooks as hooks_module
from core.errors import AgentException
from core.events import ORCHESTRATOR_ACTOR
from hooks import HookContext, HookEvent, HookOutcome, HookRegistry
from services.compact_service import CompactService
from services.session_service import SessionService
from services.workspace_service import WorkspaceService


class FakeDb:
    """提供 Service 单元测试需要的最小异步数据库接口。"""

    def __init__(self) -> None:
        self.flush_count = 0

    async def flush(self) -> None:
        self.flush_count += 1


def test_hook_exception_is_fail_open(caplog: pytest.LogCaptureFixture) -> None:
    """单个 Hook 失败不能中断后续 Hook。"""
    registry = HookRegistry()

    async def broken(_: HookContext) -> HookOutcome:
        raise RuntimeError("boom")

    async def healthy(_: HookContext) -> HookOutcome:
        return HookOutcome(additional_context="可用上下文")

    registry.register(HookEvent.USER_PROMPT_SUBMIT, broken)
    registry.register(HookEvent.USER_PROMPT_SUBMIT, healthy)
    ctx = HookContext(
        event=HookEvent.USER_PROMPT_SUBMIT,
        session_id=1,
        actor=ORCHESTRATOR_ACTOR,
    )

    with caplog.at_level(logging.ERROR):
        outcomes = asyncio.run(registry.trigger(ctx))

    assert [item.additional_context for item in outcomes] == ["可用上下文"]
    assert "已按 fail-open 放行" in caplog.text


def test_hook_timeout_is_fail_open(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Hook 超时只记录告警，不向主流程传播异常。"""
    registry = HookRegistry()

    async def callback(_: HookContext) -> HookOutcome:
        return HookOutcome(block="不应返回")

    async def timeout(awaitable, *, timeout: int):
        assert timeout == 60
        awaitable.close()
        raise TimeoutError

    monkeypatch.setattr(hooks_module.asyncio, "wait_for", timeout)
    registry.register(HookEvent.PRE_TOOL_USE, callback)
    ctx = HookContext(
        event=HookEvent.PRE_TOOL_USE,
        session_id=2,
        actor=ORCHESTRATOR_ACTOR,
    )

    with caplog.at_level(logging.WARNING):
        outcomes = asyncio.run(registry.trigger(ctx))

    assert outcomes == []
    assert "Hook 执行超时" in caplog.text


def test_snapshot_context_appends_messages_after_watermark() -> None:
    """有效快照后仍需加载水位之后的新消息。"""
    service = SessionService(FakeDb())
    snapshot = SimpleNamespace(
        through_message_id=2,
        messages=[{"role": "user", "content": "历史摘要"}],
    )
    delta = SimpleNamespace(id=3, role="assistant", content="水位后回复")

    class FakeRepo:
        async def latest_snapshot(self, session_id: int):
            assert session_id == 9
            return snapshot

        async def list_messages_range(self, session_id: int, **kwargs):
            assert session_id == 9
            assert kwargs == {"after_message_id": 2}
            return [delta]

    async def get_required(session_id: int):
        return SimpleNamespace(id=session_id)

    service.repo = FakeRepo()
    service.get_required = get_required

    context = asyncio.run(service.load_context(9))

    assert context == [
        {"role": "user", "content": "历史摘要"},
        {"role": "assistant", "content": "水位后回复"},
    ]


def test_turn_context_keeps_raw_message_unchanged() -> None:
    """临时上下文只进入模型请求副本，不能污染数据库中的原始用户消息。"""
    service = SessionService(FakeDb())
    turn_id = uuid4()
    turn = SimpleNamespace(
        id=turn_id,
        session_id=11,
        started_message_id=4,
    )
    snapshot = SimpleNamespace(
        through_message_id=2,
        messages=[{"role": "user", "content": "压缩历史"}],
    )
    history_message = SimpleNamespace(id=3, role="assistant", content="历史回复")
    raw_message = SimpleNamespace(
        id=4,
        role="user",
        content="原始问题",
    )

    class FakeRepo:
        async def latest_snapshot(self, session_id: int):
            return snapshot

        async def list_messages_range(self, session_id: int, **kwargs):
            if kwargs.get("turn_id") == turn_id:
                return [raw_message]
            assert kwargs == {"after_message_id": 2, "before_message_id": 4}
            return [history_message]

    service.repo = FakeRepo()

    history, current, through_message_id = asyncio.run(
        service.load_context_for_turn(
            turn,
            "Hook 上下文",
            rendered_memories="临时 Memory 正文",
        )
    )

    assert history == [
        {"role": "user", "content": "压缩历史"},
        {"role": "assistant", "content": "历史回复"},
    ]
    assert current == [
        {
            "role": "user",
            "content": "临时 Memory 正文\n\n原始问题\n\nHook 上下文",
        },
    ]
    assert raw_message.content == "原始问题"
    assert through_message_id == 3


def test_start_turn_persists_raw_message_before_binding_turn() -> None:
    """Turn 双向引用采用两阶段写入，首条消息内容必须保持原样。"""
    service = SessionService(FakeDb())
    turn_id = uuid4()
    calls: list[tuple] = []

    class FakeRepo:
        async def add_message(
            self,
            session_id: int,
            role: str,
            content: str,
            token_estimate: int,
            turn_id=None,
        ):
            calls.append(("message", content, turn_id))
            return SimpleNamespace(id=13, content=content, turn_id=turn_id)

        async def create_turn(self, **kwargs):
            calls.append(("turn", kwargs["started_message_id"]))
            return SimpleNamespace(id=turn_id)

        async def bind_message_to_turn(self, message, bound_turn_id):
            calls.append(("bind", message.id, bound_turn_id))
            message.turn_id = bound_turn_id
            return message

    service.repo = FakeRepo()
    session = SimpleNamespace(id=3, user_id=8, workspace_id=5)

    turn, message = asyncio.run(service.start_turn(session, 8, 5, "原始问题"))

    assert turn.id == turn_id
    assert message.content == "原始问题"
    assert calls == [
        ("message", "原始问题", None),
        ("turn", 13),
        ("bind", 13, turn_id),
    ]


def test_session_access_rejects_unscoped_and_cross_workspace_records() -> None:
    """历史无归属会话和跨工作区会话都不能被当前请求访问。"""
    service = SessionService(FakeDb())
    unscoped = SimpleNamespace(
        user_id=None,
        workspace_id=None,
        shared_with=[],
        visibility="public",
    )
    other_workspace = SimpleNamespace(
        user_id=8,
        workspace_id=6,
        shared_with=[],
        visibility="private",
    )

    assert asyncio.run(service.check_access(unscoped, 8, 5)) is False
    assert asyncio.run(service.check_access(other_workspace, 8, 5)) is False


def test_compaction_snapshot_records_message_watermark() -> None:
    """L2 压缩落库时必须保存其覆盖的最后消息 ID。"""
    calls: list[dict] = []

    class FakeRepo:
        async def add_snapshot(self, session_id: int, messages: list, summary: str, **kwargs):
            calls.append(
                {
                    "session_id": session_id,
                    "messages": messages,
                    "summary": summary,
                    **kwargs,
                }
            )

    class FakeLlm:
        async def stream(self, messages):
            yield {"type": "message_final", "content": [{"type": "text", "text": "摘要"}]}

    service = CompactService(FakeRepo(), llm=FakeLlm(), threshold=1)
    messages = [{"role": "user", "content": "很长的历史消息" * 20}]

    compacted = asyncio.run(service.maybe_compact(7, messages, through_message_id=42))

    assert compacted[0]["content"].endswith("摘要")
    assert calls[0]["through_message_id"] == 42


def test_workspace_membership_requires_joined_active_member() -> None:
    """仅有待接受邀请记录时，不能把 JWT claim 当成可信工作区。"""
    service = WorkspaceService(FakeDb())
    workspace = SimpleNamespace(id=5, is_deleted=False, suspended=False)

    class WorkspaceRepo:
        async def get_by_id(self, workspace_id: int):
            assert workspace_id == 5
            return workspace

    class MemberRepo:
        async def get_by_workspace_and_user(self, workspace_id: int, user_id: int):
            return SimpleNamespace(joined_at=None)

    service.repo = WorkspaceRepo()
    service.member_repo = MemberRepo()

    with pytest.raises(AgentException, match="有效成员"):
        asyncio.run(service.require_active_member(5, 8))
