"""ToolService.run 挂载 Pre/PostToolUse hook 的集成测试(用轻量替身,不触真 DB)。

验证核心不变式:
- PreToolUse 返回 block → 工具不执行,拦截理由作为结果返回;
- PreToolUse 返回 updated_input → 工具收到改写后的入参;
- PostToolUse 在工具成功后被触发,能观察到输出。
"""

import asyncio

from app.core.hooks import HookEvent, HookOutcome, get_hook_registry
from app.services.tool_service import ToolService


class _FakeRecord:
    def __init__(self):
        self.input_args = None
        self.status = None
        self.error = None
        self.output = None


class _FakeToolRepo:
    """替身 tool_repo:只记录状态流转,不落库。"""

    def __init__(self):
        self.last = None

    async def start(self, session_id, tool_name, input_args, message_id=None):
        rec = _FakeRecord()
        rec.input_args = input_args
        rec.status = "running"
        self.last = rec
        return rec

    async def succeed(self, record, output_data):
        record.status = "succeeded"
        record.output = output_data
        return record

    async def fail(self, record, error_message):
        record.status = "failed"
        record.error = error_message
        return record


class _FakeDB:
    async def flush(self):
        return None


class _RecordingTool:
    """替身工具:记录收到的入参,返回固定输出。"""

    def __init__(self):
        self.called_with = None

    async def run_with_dict(self, data, ctx):
        self.called_with = data
        return "TOOL_OUTPUT"


class _FakeRegistry:
    def __init__(self, tool):
        self._tool = tool

    def get(self, name):
        return self._tool


def _make_service(tool):
    svc = ToolService.__new__(ToolService)  # 跳过 __init__ 的真实依赖
    svc.db = _FakeDB()
    svc.registry = _FakeRegistry(tool)
    svc.tool_repo = _FakeToolRepo()
    return svc


def test_pre_tool_use_block_prevents_execution():
    get_hook_registry().clear()
    tool = _RecordingTool()
    svc = _make_service(tool)

    async def veto(ctx):
        return HookOutcome(block="被 hook 拦截")

    get_hook_registry().register(HookEvent.PRE_TOOL_USE, veto)
    try:
        out = asyncio.run(svc.run(1, "Bash", {"cmd": "ls"}))
    finally:
        get_hook_registry().clear()

    assert out == "被 hook 拦截"
    assert tool.called_with is None  # 工具未被执行
    assert svc.tool_repo.last.status == "failed"


def test_pre_tool_use_updated_input_reaches_tool():
    get_hook_registry().clear()
    tool = _RecordingTool()
    svc = _make_service(tool)

    async def rewrite(ctx):
        merged = dict(ctx.tool_input)
        merged["injected"] = True
        return HookOutcome(updated_input=merged)

    get_hook_registry().register(HookEvent.PRE_TOOL_USE, rewrite)
    try:
        out = asyncio.run(svc.run(1, "Bash", {"cmd": "ls"}))
    finally:
        get_hook_registry().clear()

    assert out == "TOOL_OUTPUT"
    assert tool.called_with == {"cmd": "ls", "injected": True}


def test_post_tool_use_observes_output():
    get_hook_registry().clear()
    tool = _RecordingTool()
    svc = _make_service(tool)
    seen = {}

    async def observe(ctx):
        seen["output"] = ctx.tool_output
        seen["tool"] = ctx.tool_name
        return None

    get_hook_registry().register(HookEvent.POST_TOOL_USE, observe)
    try:
        asyncio.run(svc.run(1, "Bash", {"cmd": "ls"}))
    finally:
        get_hook_registry().clear()

    assert seen == {"output": "TOOL_OUTPUT", "tool": "Bash"}


class _FailingTool:
    """替身工具:执行即抛异常。"""

    async def run_with_dict(self, data, ctx):
        raise RuntimeError("工具炸了")


def test_post_tool_use_fires_on_failure():
    get_hook_registry().clear()
    svc = _make_service(_FailingTool())
    seen = {}

    async def observe(ctx):
        seen["output"] = ctx.tool_output
        seen["is_error"] = ctx.is_error
        return None

    get_hook_registry().register(HookEvent.POST_TOOL_USE, observe)
    try:
        try:
            asyncio.run(svc.run(1, "Bash", {"cmd": "ls"}))
            raised = False
        except RuntimeError:
            raised = True
    finally:
        get_hook_registry().clear()

    assert raised  # 异常仍原样上抛,hook 不吞错
    assert seen == {"output": "工具炸了", "is_error": True}
    assert svc.tool_repo.last.status == "failed"
