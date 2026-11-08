"""压缩角色：L1 微压缩旧工具结果，以及 L2 让模型总结整段历史的 Context。

两级都只处理中立消息字典（与 session_messages、session_snapshots 的落库形状一致）：L2 把整段
历史的 JSON 原样拼进提示，摘要替换历史后作为快照落库，经领域消息转一圈会改写缺省字段。
是否触发、调哪个模型、快照写到哪里由应用层 harness/app/compaction.py 编排。
"""

import json
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from harness.domain.context import Context
from harness.domain.messages import UserMessage
from harness.domain.prompts.compaction import (
    COMPACT_INSTRUCTION,
    COMPRESSED_PREFIX,
    PREVIOUS_TOOL_PLACEHOLDER,
)

# L1 保留的参考资料工具：这类工具的输出一旦被替换，模型只能重读文件，反而更费 token。
_PRESERVE_TOOL_NAMES: frozenset[str] = frozenset({"read_file"})

# L1 跳过的最近工具结果条数。
_MICRO_KEEP_RECENT = 3

# L1 跳过的工具结果长度阈值，过短压缩收益太小。
_MICRO_MIN_LEN = 500


@dataclass(frozen=True)
class Compactor:
    """上下文压缩角色，不持有状态；同样的输入总是得到同样的输出。"""

    def estimate_tokens(self, messages: Sequence[dict[str, Any]]) -> int:
        """粗略估算消息列表的 token 数（约 4 字符 = 1 token）。"""
        return len(str(list(messages))) // 4

    def micro_compact(self, messages: Sequence[dict[str, Any]]) -> list[dict[str, Any]]:
        """L1：把最近 3 条以外的旧工具结果替换为占位符，返回新列表，不改写入参。

        工具结果是 role="tool" 的中立 dict，自带 tool_name，语义不随 provider 变：
        - 仅当工具结果超过 3 条时才动手，且跳过最近 3 条（保留近期上下文完整）；
        - read_file 类参考资料原样保留；
        - 输出不是字符串或不超过 500 字符的不压（收益太小）。
        """
        result = list(messages)
        tool_indexes = [
            index
            for index, message in enumerate(result)
            if isinstance(message, dict)
            and message.get("role") == "tool"
            and isinstance(message.get("content"), dict)
        ]
        if len(tool_indexes) <= _MICRO_KEEP_RECENT:
            return result
        for index in tool_indexes[:-_MICRO_KEEP_RECENT]:
            message = result[index]
            content = message["content"]
            output = content.get("output")
            if not isinstance(output, str) or len(output) <= _MICRO_MIN_LEN:
                continue
            tool_name = content.get("tool_name") or "tool"
            if tool_name in _PRESERVE_TOOL_NAMES:
                continue
            placeholder = PREVIOUS_TOOL_PLACEHOLDER.format(tool_name=tool_name)
            # 只替换输出，键序与其余字段原样保留。
            result[index] = {**message, "content": {**content, "output": placeholder}}
        return result

    def context(self, messages: Sequence[dict[str, Any]]) -> Context[None]:
        """L2：让模型总结整段历史；没有系统指令，历史 JSON 拼在唯一一条用户消息里。"""
        history = json.dumps(list(messages), ensure_ascii=False, default=str)
        return Context(instruction=None, messages=(UserMessage(text=COMPACT_INSTRUCTION + history),))

    def summary_messages(self, summary: str) -> list[dict[str, Any]]:
        """摘要替换整段历史后的消息列表：一条带压缩标记的用户消息。"""
        return [{"role": "user", "content": f"{COMPRESSED_PREFIX}{summary}"}]
