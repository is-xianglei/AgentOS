from __future__ import annotations

from app.core.hooks import (
    HookContext,
    HookEvent,
    HookOutcome,
    HookRegistry,
    get_hook_registry,
)

# 单条工具输出超过该字符数视为"大输出",仅打日志提醒(不改写结果)。
LARGE_OUTPUT_THRESHOLD = 20000


async def large_output_hook(ctx: HookContext) -> HookOutcome | None:
    """PostToolUse:工具输出过大时打日志提醒"""
    if ctx.tool_output and len(ctx.tool_output) > LARGE_OUTPUT_THRESHOLD:
        print(
            f"[hook] 大输出提醒: 工具 {ctx.tool_name} 产出 "
            f"{len(ctx.tool_output)} 字符 (actor={ctx.actor.name})"
        )
    return None


def register_builtin_hooks(registry: HookRegistry | None = None) -> None:
    """把内建 hook 注册到全局注册表

    这里显式注册即"配置":业务方新增 hook 在此追加 register 调用即可。
    """
    registry = registry or get_hook_registry()
    registry.register(HookEvent.POST_TOOL_USE, large_output_hook)
