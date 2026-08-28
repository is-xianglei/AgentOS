import json
import logging
import time
from typing import Any

from core.errors import AgentException
from llm import BaseLLMClient, create_llm_client
from llm.types import LLMEventKind, LLMTurnResult
from session.repository import SessionRepository

logger = logging.getLogger(__name__)

# 压缩 L1 的保留参考资料工具白名单:这类工具的输出一旦被替换,模型只能重读文件,反而更费 token.
_PRESERVE_TOOL_NAMES: frozenset[str] = frozenset({"read_file"})

# L1 微压缩跳过的最近工具结果条数.
_MICRO_KEEP_RECENT = 3

# L1 微压缩跳过的工具结果字符串长度阈值,过短压缩收益太小.
_MICRO_MIN_LEN = 500


class CompactService:
    def __init__(
        self,
        session_repo: SessionRepository,
        llm: BaseLLMClient | None = None,
        threshold: int = 50_000,
    ):
        """初始化上下文压缩服务依赖。"""
        self.session_repo = session_repo
        self.llm = llm or create_llm_client(scenario="compact")
        self.threshold = threshold

    async def maybe_compact(
        self,
        session_id: int,
        messages: list[dict[str, Any]],
        through_message_id: int | None = None,
    ) -> list[dict[str, Any]]:
        """LLM 调用前的两级压缩入口。

        - 每次都先跑 L1 micro_compact(原地修改旧的 tool_result);
        - 若估算 token 仍超阈值,再触发 L2 auto_compact 生成摘要并落快照。
        """
        messages = self._micro_compact(messages)

        est_tokens_before = self._estimate_tokens(messages)
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

        # 压缩含一次 LLM 调用,先记触发再记完成,中途失败时可据触发日志定位中断点。
        logger.info(
            "上下文超过阈值,触发自动压缩",
            extra={
                "session_id": session_id,
                "est_tokens_before": est_tokens_before,
                "threshold": self.threshold,
            },
        )
        started_at = time.perf_counter()
        compacted = await self._auto_compact(messages)
        summary = compacted[0]["content"] if compacted else ""
        if through_message_id is not None:
            await self.session_repo.add_snapshot(
                session_id,
                compacted,
                summary,
                through_message_id=through_message_id,
            )
        logger.info(
            "上下文自动压缩完成",
            extra={
                "session_id": session_id,
                "est_tokens_before": est_tokens_before,
                "est_tokens_after": self._estimate_tokens(compacted),
                "duration_ms": round((time.perf_counter() - started_at) * 1000, 2),
            },
        )
        return compacted

    def _estimate_tokens(self, messages: list[dict[str, Any]]) -> int:
        """粗略估算消息列表的 token 数(约 4 字符 = 1 token)。"""
        return len(str(messages)) // 4

    def _micro_compact(self, messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """L1 微压缩:把第 4 条以后的旧工具结果替换为占位符。

        遍历的是中立上下文:工具结果是 role="tool" 的 ToolResultMessage dict,
        自带 tool_name,无需再从 assistant 的 tool_use 块反查;语义不随 provider 变。
        - 仅当历史工具结果超过 3 条时才动手;
        - 跳过最近 3 条工具结果(保留近期上下文完整);
        - read_file 类参考资料原样保留;
        - 内容短于 500 字符的不压(收益太小)。
        """
        tool_results: list[dict[str, Any]] = []
        for message in messages:
            if not isinstance(message, dict) or message.get("role") != "tool":
                continue
            content = message.get("content")
            if isinstance(content, dict):
                tool_results.append(content)

        if len(tool_results) <= _MICRO_KEEP_RECENT:
            return messages

        # 注意:这里压缩"前 N-3"条(即较旧的部分),保留最近 3 条。
        for result_item in tool_results[:-_MICRO_KEEP_RECENT]:
            output = result_item.get("output")
            if not isinstance(output, str):
                continue
            if len(output) <= _MICRO_MIN_LEN:
                continue
            tool_name = result_item.get("tool_name") or "tool"
            if tool_name in _PRESERVE_TOOL_NAMES:
                continue
            result_item["output"] = f"[Previous: used {tool_name}]"

        return messages

    async def _auto_compact(self, messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """L2 自动压缩:调用 LLM 生成摘要并替换整段历史。"""
        compact_prompt = (
            "为保持上下文连贯,请总结本次对话,内容需包含三点:\n"
            "1)已完成事项;2)当前状态;3)作出的关键决策。\n"
            "行文简洁,但务必保留核心关键信息。\n\n"
            f"{json.dumps(messages, ensure_ascii=False, default=str)}"
        )

        # 压缩只要一段摘要文本,不往 bus 发事件,故不建装配器、直接消费中立流。
        result: LLMTurnResult | None = None
        async for event in self.llm.stream(messages=[{"role": "user", "content": compact_prompt}]):
            if event.kind is LLMEventKind.TURN_RESULT:
                result = event.result
        if result is None:
            raise AgentException.message("自动压缩失败：LLM 流未产出终局结果")

        summary = result.text
        return [
            {
                "role": "user",
                "content": f"[Conversation compressed.]\n\n{summary}",
            }
        ]
