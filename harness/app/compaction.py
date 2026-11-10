"""上下文压缩编排：L1 微压缩 → 判断阈值 → 用 Compactor 的 Context 调模型 → 经会话存储写快照。

压缩只要一段摘要文本，不往事件总线发事件，直接消费模型的中立流。快照经会话存储端口的
CompactSnapshotWriter 写入（只写不提交），随调用方的事务生效。
"""

import logging
import time
from collections.abc import Callable
from typing import Any

from core.errors import AgentException
from harness.app.ports import ModelClient, ModelEventKind, ModelTurnResult, ResolvedModel
from harness.app.session_port import CompactSnapshotWriter
from harness.domain.roles.compactor import Compactor

logger = logging.getLogger(__name__)


class ContextCompaction:
    """在每次模型调用前控制上下文长度。

    model 是压缩用的模型，可写：主 Agent 解析出 compact 场景模型后写入；为 None 时 L2 调用
    default_model 现建，两者都没有则抛中文异常。threshold 是触发 L2 的估算 token 数，可写：
    端点声明了上下文长度时由主 Agent 改成其 3/4。llm 供测试注入替身。
    """

    def __init__(
        self,
        snapshots: CompactSnapshotWriter,
        llm: ModelClient | None = None,
        threshold: int = 50_000,
        *,
        default_model: Callable[[], ResolvedModel] | None = None,
    ) -> None:
        self.snapshots = snapshots
        self.model: ResolvedModel | None = None
        if llm is not None:
            self.model = ResolvedModel.from_client(llm)
        self.threshold = threshold
        self._default_model = default_model
        self._compactor = Compactor()

    async def maybe_compact(
        self,
        session_id: int,
        messages: list[dict[str, Any]],
        through_message_id: int | None = None,
    ) -> list[dict[str, Any]]:
        """模型调用前的两级压缩入口。

        - 每次都先跑 L1 微压缩（替换旧的工具结果）；
        - 估算 token 仍超阈值时，再触发 L2 生成摘要；有可锚定的消息 ID 时把摘要落为快照。
        """
        messages = self._compactor.micro_compact(messages)

        est_tokens_before = self._compactor.estimate_tokens(messages)
        if est_tokens_before <= self.threshold:
            logger.debug(
                "上下文未超阈值,本轮不触发自动压缩",
                extra={
                    "session_id": session_id,
                    "est_tokens": est_tokens_before,
                    "threshold": self.threshold,
                },
            )
            return messages

        # 压缩含一次模型调用，先记触发再记完成，中途失败时可据触发日志定位中断点。
        logger.info(
            "上下文超过阈值,触发自动压缩",
            extra={
                "session_id": session_id,
                "est_tokens_before": est_tokens_before,
                "threshold": self.threshold,
            },
        )
        started_at = time.perf_counter()
        compacted = await self.summarize(messages)
        if compacted:
            summary = compacted[0]["content"]
        else:
            summary = ""
        # 没有可锚定的消息 ID 时只返回压缩结果，不落快照。
        if through_message_id is not None:
            await self.snapshots.save_compact_snapshot(
                session_id,
                compacted,
                summary,
                through_message_id,
            )
        logger.info(
            "上下文自动压缩完成",
            extra={
                "session_id": session_id,
                "est_tokens_before": est_tokens_before,
                "est_tokens_after": self._compactor.estimate_tokens(compacted),
                "duration_ms": round((time.perf_counter() - started_at) * 1000, 2),
            },
        )
        return compacted

    async def summarize(self, messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """L2：让模型总结整段历史，返回替换历史用的单条摘要消息。"""
        context = self._compactor.context(messages)
        model = self._resolve_model()
        result: ModelTurnResult | None = None
        async for event in model.client.stream(context, model.settings):
            if event.kind is ModelEventKind.TURN_RESULT:
                result = event.result
        if result is None:
            raise AgentException.message("自动压缩失败：LLM 流未产出终局结果")
        return self._compactor.summary_messages(result.text)

    def _resolve_model(self) -> ResolvedModel:
        if self.model is not None:
            return self.model
        if self._default_model is None:
            raise AgentException.message("自动压缩失败：未配置压缩模型")
        return self._default_model()
