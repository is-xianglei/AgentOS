"""队友角色：子代理规格之上叠加身份头，把一次推进的历史组装成 Context。

队友的历史全落库（team_members.history），身份块是历史首条 user 消息，由运行时在唤醒时
检查并补上；记忆正文只拼进请求副本，跳过身份块，不污染落库的历史。与一次性子代理不同，
队友的系统提示不追加 critical_reminder（保持现状）。
"""

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from harness.domain.context import Context, ToolSpec
from harness.domain.messages import Message
from harness.domain.prompts.teammate import (
    IDENTITY_BLOCK_MARKER,
    IDENTITY_BLOCK_PREFIX,
    render_identity_block,
    render_teammate_header,
)
from harness.domain.roles.subagent import SubAgentSpec, prepend_memory_prefix, with_memory_rules


def is_identity_block(content: Any) -> bool:
    """历史首条是否已是身份块；是则唤醒时不再重复注入。"""
    return (
        isinstance(content, str)
        and content.startswith(IDENTITY_BLOCK_PREFIX)
        and IDENTITY_BLOCK_MARKER in content
    )


@dataclass(frozen=True)
class Teammate:
    """队友角色：规格、团队身份与父回合继承的记忆正文。"""

    spec: SubAgentSpec
    session_id: int
    name: str
    role: str
    memory_prefix: str | None = None

    def identity_block(self) -> str:
        """历史首条的身份块文本。"""
        return render_identity_block(self.session_id, self.name, self.role)

    def instruction(self) -> str:
        """系统提示：身份头 + 规格提示词，继承记忆时追加 Memory 规则。"""
        header = render_teammate_header(self.session_id, self.name, self.role)
        prompt = header + (self.spec.system_prompt or "")
        return with_memory_rules(prompt, self.memory_prefix)

    def context(self, messages: Sequence[Message], tools: Sequence[ToolSpec]) -> Context[None]:
        """本轮调用的 Context：记忆正文拼到身份块之后的第一条用户消息前。"""
        return Context(
            instruction=self.instruction(),
            messages=prepend_memory_prefix(messages, self.memory_prefix),
            tools=tuple(tools),
        )
