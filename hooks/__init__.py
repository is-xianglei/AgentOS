"""Hook 系统引擎层。

本模块只定义 hook 机制本身——事件点、载荷、返回值、注册表——不含任何
具体业务 hook。内建实现在 ``app.core.hooks.builtin``,业务方新增 hook
也应放在 builtin(或另建子模块)后由 register_builtin_hooks 装配,
而不要污染本文件。

对外 import 路径保持不变:``from core.hooks import HookContext, ...``
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from enum import Enum
from typing import Any

from core.events import Actor


class HookEvent(str, Enum):
    """Hook 事件点(覆盖一个 agent cycle 的四个关键节点)。"""

    # 用户输入提交后、进 LLM 前
    USER_PROMPT_SUBMIT = "UserPromptSubmit"
    # 工具执行前(可 deny / 改入参)
    PRE_TOOL_USE = "PreToolUse"
    # 工具执行后(观察副作用 / 检查输出)
    POST_TOOL_USE = "PostToolUse"
    # 回合即将结束时(可强制续跑)
    STOP = "Stop"


@dataclass
class HookContext:
    """Hook 回调收到的载荷。按事件点填不同字段(未用到的留 None)。

    - session_id / actor:所有事件都带,标明归属会话与产出者(三类 actor 通用)。
    - user_content:仅 UserPromptSubmit,原始用户输入。
    - tool_name / tool_input:Pre/PostToolUse,工具名与入参。
    - tool_output / is_error:仅 PostToolUse,工具执行结果。
    - messages:仅 Stop,当前上下文消息(供决定是否续跑)。
    """

    event: HookEvent
    session_id: int
    actor: Actor
    user_content: str | None = None
    tool_name: str | None = None
    tool_input: dict[str, Any] | None = None
    tool_output: str | None = None
    is_error: bool = False
    messages: list[dict[str, Any]] | None = None


@dataclass
class HookOutcome:
    """Hook 回调的返回值(全部可选;返回 None 等价于"放行、无操作")。

    - block:非 None 表示拦截本次工具执行(PreToolUse),其字符串作为
      tool_result 内容回给模型(对齐 s04 "返回非 None 即拦截"的语义)。
    - updated_input:改写工具入参(PreToolUse),后续 hook 与执行都用新入参。
    - additional_context:注入上下文(UserPromptSubmit),追加到用户输入之后。
    - continuation:强制续跑(Stop),非 None 时作为一条 user 消息让回合继续。
    """

    block: str | None = None
    updated_input: dict[str, Any] | None = None
    additional_context: str | None = None
    continuation: str | None = None


# 回调签名:async (HookContext) -> HookOutcome | None
HookCallback = Callable[[HookContext], Awaitable["HookOutcome | None"]]


class HookRegistry:
    """事件名 → 回调列表的注册表。主循环只调 trigger,由注册表决定跑什么。

    分发按注册顺序执行;单个回调异常被隔离(打日志后跳过),不冲垮主流程
    (对齐生产实践:一个坏 hook 不应让整个会话崩溃,区别于 s04 教学版的裸调用)。
    """

    def __init__(self) -> None:
        self._hooks: dict[HookEvent, list[HookCallback]] = {e: [] for e in HookEvent}

    def register(self, event: HookEvent, callback: HookCallback) -> None:
        """注册一个回调到某事件(同一事件可注册多个,按序执行)。"""
        self._hooks[event].append(callback)

    def clear(self, event: HookEvent | None = None) -> None:
        """清空回调(不传 event 清全部)。主要供测试重置用。"""
        if event is None:
            for e in self._hooks:
                self._hooks[e] = []
        else:
            self._hooks[event] = []

    async def trigger(self, ctx: HookContext) -> list[HookOutcome]:
        """依次触发某事件的所有回调,收集非 None 的 outcome 返回。

        不做短路:所有回调都会跑(除非抛异常被隔离),调用方按事件语义
        决定如何合并多个 outcome(如 PreToolUse 取第一个 block,updated_input 依次叠加)。
        """
        outcomes: list[HookOutcome] = []
        for callback in self._hooks[ctx.event]:
            result = await asyncio.wait_for(callback(ctx), timeout=60)
            if result is not None:
                outcomes.append(result)
        return outcomes


# 进程内全局注册表单例。在 app 启动时由 register_builtin_hooks 填充内建 hook。
_registry = HookRegistry()


def get_hook_registry() -> HookRegistry:
    """获取全局 Hook 注册表"""
    return _registry


__all__ = [
    "HookEvent",
    "HookContext",
    "HookOutcome",
    "HookCallback",
    "HookRegistry",
    "get_hook_registry",
]
