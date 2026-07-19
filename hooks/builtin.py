"""内建 hook 实现与装配。

这里写具体的 hook 回调,并通过 register_builtin_hooks 把它们挂到全局注册表。
业务方新增 hook 在本文件追加回调 + 在 register_builtin_hooks 里追加一行 register 即可
"""

from __future__ import annotations

from hooks import (
    HookContext,
    HookEvent,
    HookOutcome,
    HookRegistry,
    get_hook_registry,
)


async def user_prompt_submit(ctx: HookContext) -> HookOutcome | None:
    print(f'用户提交了消息 - {ctx.user_content}...')
    return None

async def pre_tool_use(ctx: HookContext) -> HookOutcome | None:
    print(f'{ctx.tool_name} - 工具准备开始执行...')
    return None

async def post_tool_use(ctx: HookContext) -> HookOutcome | None:
    print(f'{ctx.tool_name} - 工具已执行完成...')
    return None

async def stop(ctx: HookContext) -> HookOutcome | None:
    print(f'{ctx.session_id} - 会话即将结束...')
    return None


def register_builtin_hooks(registry: HookRegistry | None = None) -> None:
    """把内建 hook 注册到全局注册表

    这里显式注册即"配置":业务方新增 hook 在此追加 register 调用即可。
    """
    registry = registry or get_hook_registry()
    # 用户输入后
    registry.register(HookEvent.USER_PROMPT_SUBMIT, user_prompt_submit)
    # 工具执行前
    registry.register(HookEvent.PRE_TOOL_USE, pre_tool_use)
    # 工具执行后
    registry.register(HookEvent.POST_TOOL_USE, post_tool_use)
    # 对话结束时
    registry.register(HookEvent.STOP, stop)
