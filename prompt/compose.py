"""system prompt 组装。

段序按易失性单调排列：恒定文案 → 环境 → Memory 规则 → skills catalog →
会话指令 → Memory Catalog。渲染顺序是 tools → system → messages，因此把最
易失的 Memory Catalog 放在末尾，可让其前的 tools + system 前缀跨 turn 复用。
Memory 正文（rendered_memories）走 messages 而非 system，见 session/service.py。
"""

from anthropic.types import TextBlockParam

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


def compose_lead_blocks(ctx: PromptContext) -> list[TextBlockParam]:
    """组装带 cache 断点的 system blocks，供 prompt caching 使用。

    渲染顺序为 tools → system → messages，故第一个断点落在稳定段末尾时，会把
    tools 数组（22 个工具约 2.8k token，是缓存收益主体）与稳定段一起缓存，跨
    turn 复用。第二个断点落在 Memory Catalog 末尾，让同一 turn 内多轮工具迭代
    （max_tool_iterations=50）复用完整 system。

    用默认 5m TTL：一个 turn 内的迭代是密集调用，5m 足够，避免 1h 的 2 倍写溢价。
    拼接后的纯文本与 compose_lead_prompt 完全一致，二者可互换。
    """
    stable = "\n\n".join(section.strip() for section in _stable_sections(ctx))
    blocks: list[TextBlockParam] = [
        {
            "type": "text",
            "text": stable,
            "cache_control": {"type": "ephemeral"},
        }
    ]
    volatile = _volatile_sections(ctx)
    if volatile:
        blocks.append(
            {
                "type": "text",
                "text": "\n\n".join(section.strip() for section in volatile),
                "cache_control": {"type": "ephemeral"},
            }
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
