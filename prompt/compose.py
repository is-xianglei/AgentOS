"""system prompt 组装。

段序按易失性单调排列：恒定文案 → 环境 → Memory 规则 → skills catalog →
会话指令 → Memory Catalog。把最易失的 Memory Catalog 放在末尾，可让其前的
稳定前缀跨 turn 复用。Memory 正文（rendered_memories）走 messages 而非
system，见 session/service.py。
"""

from llm.types import PromptBlock
from prompt.context import PromptContext
from prompt.sections import (
    LEAD_SYSTEM_PROMPT,
    MEMORY_CATALOG_HEADER,
    MEMORY_TRUST_RULES,
    SKILLS_CATALOG_HEADER,
    render_environment,
)


def _stable_sections(ctx: PromptContext) -> list[str]:
    """跨 turn 稳定的段：恒定文案、环境、Memory 规则、skills、会话指令。"""
    sections = [
        LEAD_SYSTEM_PROMPT,
        render_environment(ctx.cwd, ctx.today),
        MEMORY_TRUST_RULES,
    ]
    if ctx.skills_catalog and ctx.skills_catalog.strip():
        sections.append(f"{SKILLS_CATALOG_HEADER}\n{ctx.skills_catalog.strip()}")
    if ctx.session_prompt and ctx.session_prompt.strip():
        sections.append(ctx.session_prompt.strip())
    return sections


def _volatile_sections(ctx: PromptContext) -> list[str]:
    """每 turn 可能变化的段：Memory Catalog（按 turn 冻结，turn 内恒定）。"""
    if ctx.memory_catalog and ctx.memory_catalog.strip():
        return [f"{MEMORY_CATALOG_HEADER}\n{ctx.memory_catalog.strip()}"]
    return []


def compose_lead_prompt(ctx: PromptContext) -> str:
    """组装主 Agent 的 system prompt。"""
    sections = _stable_sections(ctx) + _volatile_sections(ctx)
    return "\n\n".join(section.strip() for section in sections)


def compose_lead_blocks(ctx: PromptContext) -> list[PromptBlock]:
    """组装带 cache 断点的中立 system 块，供 prompt caching 使用。

    两个断点：稳定段末尾一个（跨 turn 复用），Memory Catalog 末尾一个
    （供同一 turn 内多轮工具迭代复用完整 system）。断点如何落成各家 SDK
    形状由客户端编码决定，不支持缓存的 provider 忽略该标志。
    拼接后的纯文本与 compose_lead_prompt 完全一致，二者可互换。
    """
    stable = "\n\n".join(section.strip() for section in _stable_sections(ctx))
    blocks: list[PromptBlock] = [PromptBlock(text=stable, cache_breakpoint=True)]
    volatile = _volatile_sections(ctx)
    if volatile:
        blocks.append(
            PromptBlock(
                text="\n\n".join(section.strip() for section in volatile),
                cache_breakpoint=True,
            )
        )
    return blocks


def compose_subagent_prompt(base_prompt: str, rendered_memories: str | None) -> str:
    """向子代理声明 Memory 的不可信历史参考边界。"""
    if not rendered_memories:
        return base_prompt
    return f"{base_prompt.strip()}\n\n{MEMORY_TRUST_RULES}"


def compose_teammate_prompt(
    base_prompt: str,
    session_id: int,
    name: str,
    role: str,
) -> str:
    """在子代理规格提示词之上拼接 teammate 身份头，不改 registry 里的 spec。"""
    header = (
        f"你是团队成员 '{name}',角色: {role},隶属会话 {session_id} 的团队。\n"
        f"你可以通过 Team 工具与其他成员协作,并认领本会话的待办任务。\n"
    )
    return header + (base_prompt or "")
