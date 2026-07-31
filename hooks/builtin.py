"""内建 hook 实现与装配。

这里写具体的 hook 回调,并通过 register_builtin_hooks 把它们挂到全局注册表。
业务方新增 hook 在本文件追加回调 + 在 register_builtin_hooks 里追加一行 register 即可
"""

from __future__ import annotations

import logging

from core.events import ActorRole
from hooks import (
    HookContext,
    HookEvent,
    HookOutcome,
    HookRegistry,
    get_hook_registry,
)
from tools.subagents.trigger import assess, compose_hint

logger = logging.getLogger(__name__)


async def user_prompt_submit(ctx: HookContext) -> HookOutcome | None:
    logger.debug("用户已提交消息", extra={"session_id": ctx.session_id})
    return None


async def pre_tool_use(ctx: HookContext) -> HookOutcome | None:
    logger.debug(
        "工具准备开始执行：%s",
        ctx.tool_name,
        extra={"session_id": ctx.session_id, "turn_id": str(ctx.turn_id or "")},
    )
    return None


async def post_tool_use(ctx: HookContext) -> HookOutcome | None:
    logger.debug(
        "工具已执行完成：%s",
        ctx.tool_name,
        extra={"session_id": ctx.session_id, "turn_id": str(ctx.turn_id or "")},
    )
    return None


async def stop(ctx: HookContext) -> HookOutcome | None:
    logger.debug(
        "交互轮次即将结束",
        extra={"session_id": ctx.session_id, "turn_id": str(ctx.turn_id or "")},
    )
    return None


async def verification_trigger(ctx: HookContext) -> HookOutcome | None:
    """非平凡改动结束前提示补一次独立验收。

    只对主代理生效:子代理不能再派生子代理,给它注入这条提示只会让它反复
    尝试一个拿不到的工具。判定逻辑在 tools/subagents/trigger.py。
    """
    if ctx.actor.role is not ActorRole.ORCHESTRATOR:
        return None
    assessment = assess(ctx.messages)
    if not assessment.should_verify:
        return None
    logger.info(
        "回合结束前触发验收提示:原因=%s 改动文件数=%s",
        assessment.reason_text,
        len(assessment.changed_files),
        extra={"session_id": ctx.session_id, "turn_id": str(ctx.turn_id or "")},
    )
    return HookOutcome(continuation=compose_hint(assessment))


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
    # 对话结束时:非平凡改动补一次独立验收(阈值判定见 tools/subagents/trigger.py)
    registry.register(HookEvent.STOP, verification_trigger)
