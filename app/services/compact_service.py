import json
from typing import Any

from anthropic.types import MessageParam

from app.llm.client import LLMClient
from app.llm.types import chunk_type, text_from_content
from app.repositories.session_repo import SessionRepository

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
        llm: LLMClient | None = None,
        threshold: int = 50_000,
    ):
        """初始化上下文压缩服务依赖。"""
        self.session_repo = session_repo
        self.llm = llm or LLMClient()
        self.threshold = threshold

    async def maybe_compact(
        self,
        session_id: int,
        messages: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        """LLM 调用前的两级压缩入口。

        - 每次都先跑 L1 micro_compact(原地修改旧的 tool_result);
        - 若估算 token 仍超阈值,再触发 L2 auto_compact 生成摘要并落快照。
        """
        messages = self._micro_compact(messages)

        if self._estimate_tokens(messages) <= self.threshold:
            return messages

        compacted = await self._auto_compact(messages)
        summary = compacted[0]["content"] if compacted else ""
        await self.session_repo.add_snapshot(session_id, compacted, summary)
        return compacted

    def _estimate_tokens(self, messages: list[dict[str, Any]]) -> int:
        """粗略估算消息列表的 token 数(约 4 字符 = 1 token)。"""
        return len(str(messages)) // 4

    def _micro_compact(self, messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """L1 微压缩:把第 4 条以后的旧工具结果替换为占位符。

        - 仅当历史 tool_result 超过 3 条时才动手;
        - 跳过最近 3 条工具结果(保留近期上下文完整);
        - read_file 类参考资料原样保留;
        - 内容短于 100 字符的不压(收益太小)。
        """
        tool_results: list[dict[str, Any]] = []
        for message in messages:
            if not isinstance(message, dict) or message.get("role") != "user":
                continue
            content = message.get("content")
            if not isinstance(content, list):
                continue
            for block in content:
                if isinstance(block, dict) and block.get("type") == "tool_result":
                    tool_results.append(block)

        if len(tool_results) <= _MICRO_KEEP_RECENT:
            return messages

        # 反查 tool_use_id -> tool_name,assistant 消息的 content 是 tool_use 块列表。
        tool_name_map: dict[str, str] = {}
        for message in messages:
            if not isinstance(message, dict) or message.get("role") != "assistant":
                continue
            content = message.get("content")
            if not isinstance(content, list):
                continue
            for block in content:
                if isinstance(block, dict) and block.get("type") == "tool_use":
                    tool_name_map[block.get("id", "")] = block.get("name", "")

        # 注意:这里压缩"前 N-3"条(即较旧的部分),保留最近 3 条。
        for result_item in tool_results[:-_MICRO_KEEP_RECENT]:
            content_value = result_item.get("content")
            if not isinstance(content_value, str):
                continue
            if len(content_value) <= _MICRO_MIN_LEN:
                continue
            tool_id = result_item.get("tool_use_id", "")
            tool_name = tool_name_map.get(tool_id, "tool")
            if tool_name in _PRESERVE_TOOL_NAMES:
                continue
            result_item["content"] = f"[Previous: used {tool_name}]"

        return messages

    async def _auto_compact(self, messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """L2 自动压缩:调用 LLM 生成摘要并替换整段历史。"""
        compact_prompt = (
            "为保持上下文连贯,请总结本次对话,内容需包含三点:\n"
            "1)已完成事项;2)当前状态;3)作出的关键决策。\n"
            "行文简洁,但务必保留核心关键信息。\n\n"
            f"{json.dumps(messages, ensure_ascii=False, default=str)}"
        )

        final_content: list[dict[str, Any]] | None = None

        async for chunk in self.llm.stream(messages=[MessageParam(role="user", content=compact_prompt)]):
            if chunk_type(chunk) == "message_final":
                final_content = chunk.get("content")

        summary = text_from_content(final_content)
        return [
            {
                "role": "user",
                "content": f"[Conversation compressed.]\n\n{summary}",
            }
        ]