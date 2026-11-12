"""测试专用装配辅助：经组装根构造运行时，按需覆盖个别依赖。

运行时的构造参数全部必填，由组装根统一注入；测试不再直接 ``AgentRuntime(db, ...)``，
而是经这里的函数走 harness/bootstrap.py 的同一套工厂，只把要替换的依赖作为关键字覆盖传入。
``harness`` 参数可传入 ``dataclasses.replace(default_harness(), ...)`` 换掉整类实现。
不传时显式关掉作用域工具来源（``scoped_tools=()``），测试不碰网络和数据库；
需要作用域工具时传 ``replace(default_harness(), scoped_tools=(假来源,))``。
"""

from dataclasses import replace
from typing import Any

from harness.bootstrap import Harness, default_harness
from runtime.agent import AgentRuntime
from runtime.subagent import SubAgentRunner
from runtime.workflow import WorkflowRuntime


def _test_harness() -> Harness:
    """测试默认装配：进程默认装配，但不带任何作用域工具来源。"""
    return replace(default_harness(), scoped_tools=())


def make_agent_runtime(
    db: Any,
    *,
    user_id: int | None = None,
    workspace_id: int | None = None,
    harness: Harness | None = None,
    **overrides: Any,
) -> AgentRuntime:
    """经组装根装配主 Agent 运行时；overrides 可含 llm、reviewer、memory、sessions。"""
    return (harness or _test_harness()).agent_runtime(
        db, user_id=user_id, workspace_id=workspace_id, **overrides
    )


def make_subagent_runner(
    db: Any,
    *,
    bus: Any = None,
    harness: Harness | None = None,
    **overrides: Any,
) -> SubAgentRunner:
    """经组装根装配子代理运行器；overrides 可含 llm、reviewer、memory、sessions。"""
    return (harness or _test_harness()).subagent_runner(db, bus=bus, **overrides)


def make_workflow_runtime(
    db: Any,
    *,
    user_id: int | None = None,
    workspace_id: int | None = None,
    harness: Harness | None = None,
    **overrides: Any,
) -> WorkflowRuntime:
    """经组装根装配 Workflow 运行时；overrides 可含 llm、sessions。"""
    return (harness or _test_harness()).workflow_runtime(
        db, user_id=user_id, workspace_id=workspace_id, **overrides
    )


def platform_tools() -> Any:
    """组装根登记的进程级工具集，测试里替代原先直接调用的 builtin_tool_set()。"""
    return default_harness().tools


def install_runtime_classes(
    monkeypatch: Any,
    *,
    agent: Any = None,
    workflow: Any = None,
) -> None:
    """让组装根按请求装配出替身运行时：替身类按 ``cls(db, user_id=..., workspace_id=...)`` 构造。

    替换的是 Harness 的两个装配方法，get_agent_service 与 AgentService 的真实装配链路不变；
    构造替身时的参数形状与原先路由直接 new 运行时一致。
    """
    if agent is not None:

        def _agent_runtime(
            self: Harness, db: Any, *, user_id: int | None, workspace_id: int | None
        ) -> Any:
            return agent(db, user_id=user_id, workspace_id=workspace_id)

        monkeypatch.setattr(Harness, "agent_runtime", _agent_runtime)
    if workflow is not None:

        def _workflow_runtime(
            self: Harness, db: Any, *, user_id: int | None, workspace_id: int | None
        ) -> Any:
            return workflow(db, user_id=user_id, workspace_id=workspace_id)

        monkeypatch.setattr(Harness, "workflow_runtime", _workflow_runtime)
