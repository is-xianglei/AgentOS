"""HookRegistry 的纯逻辑测试(不依赖 DB / 网络)。

用 asyncio.run 驱动 async 回调,避免引入 pytest-asyncio 依赖。
覆盖:注册/触发、异常隔离、updated_input 叠加、block 拦截、outcome 收集。
ToolService/AgentRuntime 的实际挂点由端到端验证覆盖,此处只测注册表机制本身。
"""

import asyncio

from app.core.events import ORCHESTRATOR_ACTOR
from app.core.hooks import (
    HookContext,
    HookEvent,
    HookOutcome,
    HookRegistry,
)


def _ctx(event: HookEvent, **kw) -> HookContext:
    return HookContext(event=event, session_id=1, actor=ORCHESTRATOR_ACTOR, **kw)


def test_trigger_collects_non_none_outcomes_in_order():
    reg = HookRegistry()

    async def a(ctx):
        return HookOutcome(additional_context="A")

    async def b(ctx):
        return None  # 放行、无操作:不进结果

    async def c(ctx):
        return HookOutcome(additional_context="C")

    reg.register(HookEvent.USER_PROMPT_SUBMIT, a)
    reg.register(HookEvent.USER_PROMPT_SUBMIT, b)
    reg.register(HookEvent.USER_PROMPT_SUBMIT, c)

    outcomes = asyncio.run(reg.trigger(_ctx(HookEvent.USER_PROMPT_SUBMIT)))
    assert [o.additional_context for o in outcomes] == ["A", "C"]


def test_exception_in_hook_is_isolated():
    reg = HookRegistry()

    async def boom(ctx):
        raise RuntimeError("坏 hook 不应冲垮主流程")

    async def good(ctx):
        return HookOutcome(additional_context="ok")

    reg.register(HookEvent.USER_PROMPT_SUBMIT, boom)
    reg.register(HookEvent.USER_PROMPT_SUBMIT, good)

    # boom 抛异常被隔离,good 仍执行并进结果。
    outcomes = asyncio.run(reg.trigger(_ctx(HookEvent.USER_PROMPT_SUBMIT)))
    assert [o.additional_context for o in outcomes] == ["ok"]


def test_pre_tool_use_block_and_updated_input():
    reg = HookRegistry()

    async def rewrite(ctx):
        merged = dict(ctx.tool_input or {})
        merged["injected"] = True
        return HookOutcome(updated_input=merged)

    async def veto(ctx):
        return HookOutcome(block="拒绝理由")

    reg.register(HookEvent.PRE_TOOL_USE, rewrite)
    reg.register(HookEvent.PRE_TOOL_USE, veto)

    outcomes = asyncio.run(
        reg.trigger(
            _ctx(HookEvent.PRE_TOOL_USE, tool_name="Bash", tool_input={"cmd": "ls"})
        )
    )
    # 调用方约定:updated_input 依次叠加,block 取首个非 None。
    updated = next((o.updated_input for o in outcomes if o.updated_input), None)
    blocked = next((o.block for o in outcomes if o.block is not None), None)
    assert updated == {"cmd": "ls", "injected": True}
    assert blocked == "拒绝理由"


def test_clear_resets_callbacks():
    reg = HookRegistry()

    async def a(ctx):
        return HookOutcome(continuation="go")

    reg.register(HookEvent.STOP, a)
    assert asyncio.run(reg.trigger(_ctx(HookEvent.STOP)))
    reg.clear(HookEvent.STOP)
    assert asyncio.run(reg.trigger(_ctx(HookEvent.STOP))) == []
