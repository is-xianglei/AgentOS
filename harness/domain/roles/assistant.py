"""主 Agent 角色：把一个回合的运行态组装成发给模型的 Context。

系统提示的段序按易失性单调排列：恒定文案 → 环境 → Memory 规则 → skills 目录 →
可委派 Agent 目录 → 会话指令 → Memory 目录 → Plan Mode 指令。最易失的段放在末尾，
其前的稳定前缀才能跨回合复用提示缓存。角色不做 I/O：目录、记忆、Plan 正文、工作目录与日期
都由运行时读好后传进来。
"""

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date

from harness.domain.context import Context, PromptBlock, ToolSpec
from harness.domain.memory import MemoryRecall
from harness.domain.messages import Message
from harness.domain.prompts.lead import (
    DELEGATE_CATALOG_HEADER,
    LEAD_SYSTEM_PROMPT,
    MEMORY_CATALOG_HEADER,
    SKILLS_CATALOG_HEADER,
    TURBO_PROMPT,
    render_environment,
)
from harness.domain.prompts.memory import MEMORY_TRUST_RULES
from harness.domain.prompts.plan_mode import render_plan_mode_instructions


@dataclass(frozen=True)
class Assistant:
    """主 Agent 一个回合的组装输入；系统指令按回合冻结，消息与工具按轮传入。

    字段按易失性从低到高声明，与系统提示的分段顺序一致。段是否加载取决于真实状态
    （目录是否为空、会话是否带指令），不靠在消息里搜关键词。
    memory 只用 instruction（记忆目录）；message_prefix 由会话加载按本轮起始消息 ID 拼进
    messages，角色不再重复拼接。plan_content 为 None 表示不在 Plan Mode，空串表示计划尚未写入。
    turbo 为 True 时整段系统提示换成极速提示，其余字段一律不用。
    """

    cwd: str
    today: date
    skills_catalog: str | None = None
    # 可委派的自建 Agent 目录；只有未绑定自建 Agent 的默认 Agent 会传入。
    delegate_catalog: str | None = None
    session_prompt: str | None = None
    memory: MemoryRecall | None = None
    plan_content: str | None = None
    turbo: bool = False

    def instruction(self) -> tuple[PromptBlock, ...]:
        """组装带缓存断点的系统指令块。

        两个断点：稳定段末尾一个（跨回合复用），Memory 目录末尾一个（供同一回合内多轮工具
        迭代复用完整系统指令）。Plan 正文随计划编辑而变，属易失段，不设断点，以免击穿前两段的
        缓存前缀。断点如何落成各家 SDK 形状由客户端编码决定，不支持缓存的 provider 忽略该标志。
        """
        if self.turbo:
            return (PromptBlock(text=TURBO_PROMPT),)
        # 段文案多以换行结尾（如 LEAD_SYSTEM_PROMPT），拼接前逐段 strip，保证段间恰好一个空行。
        stable = "\n\n".join(section.strip() for section in self._stable_sections())
        blocks = [PromptBlock(text=stable, cache_breakpoint=True)]
        volatile = self._volatile_sections()
        if volatile:
            blocks.append(
                PromptBlock(
                    text="\n\n".join(section.strip() for section in volatile),
                    cache_breakpoint=True,
                )
            )
        if self.plan_content is not None:
            blocks.append(PromptBlock(text=render_plan_mode_instructions(self.plan_content)))
        return tuple(blocks)

    def context(self, messages: Sequence[Message], tools: Sequence[ToolSpec]) -> Context[None]:
        """本轮调用的 Context：本回合冻结的系统指令、本轮消息与本轮可见工具。"""
        return Context(instruction=self.instruction(), messages=tuple(messages), tools=tuple(tools))

    def final_context(self, messages: Sequence[Message]) -> Context[None]:
        """轮次耗尽后的收尾调用：同一份系统指令，不带工具，迫使模型直接作答。"""
        return self.context(messages, ())

    def _stable_sections(self) -> list[str]:
        """跨回合稳定的段：恒定文案、环境、Memory 规则、skills、可委派目录、会话指令。"""
        sections = [
            LEAD_SYSTEM_PROMPT,
            render_environment(self.cwd, self.today),
            MEMORY_TRUST_RULES,
        ]
        if self.skills_catalog and self.skills_catalog.strip():
            sections.append(f"{SKILLS_CATALOG_HEADER}\n{self.skills_catalog.strip()}")
        if self.delegate_catalog and self.delegate_catalog.strip():
            sections.append(f"{DELEGATE_CATALOG_HEADER}\n{self.delegate_catalog.strip()}")
        if self.session_prompt and self.session_prompt.strip():
            sections.append(self.session_prompt.strip())
        return sections

    def _volatile_sections(self) -> list[str]:
        """每回合可能变化的段：Memory 目录（按回合冻结，回合内恒定）。"""
        catalog = self.memory.instruction if self.memory is not None else None
        if catalog and catalog.strip():
            return [f"{MEMORY_CATALOG_HEADER}\n{catalog.strip()}"]
        return []
