"""上下文压缩编排测试：L1 → 阈值 → Compactor 的 Context 调模型 → 经会话域 Service 写快照。

会话侧用真实 SessionService，只把它的仓储换成记录替身，确认快照经 Service 的公开方法写入，
编排层不再持有 SessionRepository。
"""

from collections.abc import AsyncIterator
from types import SimpleNamespace
from typing import Any

import pytest

from core.errors import AgentException
from harness.app.compaction import ContextCompaction
from harness.app.ports import (
    ModelEventKind,
    ModelSettings,
    ModelStreamEvent,
    ModelTurnResult,
    ResolvedModel,
)
from session.service import SessionService

pytestmark = pytest.mark.anyio

_HISTORY = [{"role": "user", "content": "这是一段足够长的历史上下文" * 4}]


class _Repo:
    """会话仓储替身：只记录快照写入。"""

    def __init__(self) -> None:
        self.snapshots: list[dict[str, Any]] = []

    async def add_snapshot(
        self,
        session_id: int,
        messages: list[Any],
        summary: str,
        through_message_id: int,
        snapshot_type: str = "compact",
    ) -> SimpleNamespace:
        self.snapshots.append(
            {
                "session_id": session_id,
                "messages": messages,
                "summary": summary,
                "through_message_id": through_message_id,
                "snapshot_type": snapshot_type,
            }
        )
        return SimpleNamespace(id=1)


class _LLM:
    model = "compact-model"

    def __init__(self, text: str | None = "摘要") -> None:
        self.text = text
        self.contexts: list[Any] = []

    async def stream(self, context: Any, settings: ModelSettings) -> AsyncIterator[ModelStreamEvent]:
        self.contexts.append((context, settings))
        if self.text is not None:
            yield ModelStreamEvent(
                kind=ModelEventKind.TURN_RESULT, result=ModelTurnResult(text=self.text)
            )


def _sessions() -> tuple[SessionService, _Repo]:
    service = SessionService.__new__(SessionService)
    repo = _Repo()
    service.repo = repo  # type: ignore[assignment]
    return service, repo


async def test_snapshot_is_written_through_session_service() -> None:
    sessions, repo = _sessions()
    llm = _LLM()
    compaction = ContextCompaction(sessions, llm=llm, threshold=1)  # type: ignore[arg-type]

    compacted = await compaction.maybe_compact(7, list(_HISTORY), through_message_id=40)

    assert compacted == [{"role": "user", "content": "[Conversation compressed.]\n\n摘要"}]
    assert repo.snapshots == [
        {
            "session_id": 7,
            "messages": compacted,
            "summary": "[Conversation compressed.]\n\n摘要",
            "through_message_id": 40,
            "snapshot_type": "compact",
        }
    ]
    context, settings = llm.contexts[0]
    assert context.instruction is None
    assert settings.model_id == "compact-model"


async def test_no_snapshot_without_anchor_or_below_threshold() -> None:
    sessions, repo = _sessions()
    compaction = ContextCompaction(sessions, llm=_LLM(), threshold=1)  # type: ignore[arg-type]
    await compaction.maybe_compact(7, list(_HISTORY))

    compaction.threshold = 10_000
    result = await compaction.maybe_compact(7, list(_HISTORY), through_message_id=40)

    assert repo.snapshots == []
    assert result == _HISTORY


async def test_resolved_model_is_writable_and_default_model_is_lazy() -> None:
    sessions, _ = _sessions()
    built: list[str] = []

    def _default() -> ResolvedModel:
        built.append("default")
        return ResolvedModel.from_client(_LLM("默认"))  # type: ignore[arg-type]

    compaction = ContextCompaction(sessions, threshold=1, default_model=_default)
    compaction.model = ResolvedModel.from_client(_LLM("注入"))  # type: ignore[arg-type]
    assert (await compaction.summarize(_HISTORY))[0]["content"].endswith("注入")
    assert built == []

    compaction.model = None
    assert (await compaction.summarize(_HISTORY))[0]["content"].endswith("默认")
    assert built == ["default"]


async def test_missing_turn_result_or_model_raises() -> None:
    sessions, repo = _sessions()
    compaction = ContextCompaction(sessions, llm=_LLM(None), threshold=1)  # type: ignore[arg-type]
    with pytest.raises(AgentException, match="未产出终局结果"):
        await compaction.maybe_compact(7, list(_HISTORY), through_message_id=40)
    assert repo.snapshots == []

    with pytest.raises(AgentException, match="未配置压缩模型"):
        await ContextCompaction(sessions, threshold=1).summarize(_HISTORY)


async def test_agent_runtime_compaction_uses_session_service() -> None:
    from runtime.agent import AgentRuntime

    runtime = AgentRuntime(object(), user_id=3, workspace_id=2, llm=object())  # type: ignore[arg-type]

    assert isinstance(runtime.compact_service, ContextCompaction)
    assert runtime.compact_service.snapshots is runtime.sessions
