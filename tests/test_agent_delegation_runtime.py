"""主运行时侧的委派测试:可委派目录的加载、渲染与注入,以及本轮生效工具名单的透传。

数据库与 LLM 全部打桩;目录内容的工作区隔离与启用状态见 test_agent_delegation_pg.py。
"""

from datetime import date
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest

from agent.service import AgentRuntimeConfig, DelegateCatalogEntry
from core.events import ORCHESTRATOR_ACTOR
from harness.app.ports import ModelEventKind, ModelStreamEvent, ModelTurnResult
from harness.domain.messages import ToolCall
from prompt import PromptContext, compose_lead_blocks
from prompt.sections import DELEGATE_CATALOG_HEADER, SKILLS_CATALOG_HEADER
from runtime.agent import PLAN_MODE_ALLOWED_TOOLS, AgentRuntime
from tools.policy_engine import ToolAccessDecision
from tools.registry import build_tool_registry

pytestmark = pytest.mark.anyio

_CATALOG = "- agent_id=7 名称:审稿人 描述:负责审稿 工具:Read"


class _FakeLLM:
    """记录每次请求的 system prompt,返回一段无工具调用的文本。"""

    provider = "openai"
    model = "model-a"

    def __init__(self) -> None:
        self.system_prompts: list[Any] = []

    async def stream(self, messages, system_prompt=None, tools=None, model=None):
        self.system_prompts.append(system_prompt)
        yield ModelStreamEvent(
            ModelEventKind.TURN_RESULT,
            result=ModelTurnResult(text="答案", stop_reason="end_turn"),
        )


# ---------- 提示词组装 ----------


def test_delegate_catalog_is_a_stable_section_after_skills() -> None:
    blocks = compose_lead_blocks(
        PromptContext.create(
            skills_catalog="- s: 技能",
            delegate_catalog=_CATALOG,
            session_prompt="会话指令",
            memory_catalog="- m: 记忆",
            cwd="/srv",
            today=date(2026, 11, 4),
        )
    )

    stable = blocks[0].text
    # 目录属于跨轮次稳定段(带缓存断点的第一块),位于 skills 之后、会话指令之前。
    assert f"{DELEGATE_CATALOG_HEADER}\n{_CATALOG}" in stable
    assert stable.index(SKILLS_CATALOG_HEADER) < stable.index(DELEGATE_CATALOG_HEADER)
    assert stable.index(DELEGATE_CATALOG_HEADER) < stable.index("会话指令")
    assert DELEGATE_CATALOG_HEADER not in blocks[1].text


@pytest.mark.parametrize("catalog", [None, "", "  \n"])
def test_empty_delegate_catalog_is_not_injected(catalog: str | None) -> None:
    blocks = compose_lead_blocks(PromptContext.create(delegate_catalog=catalog))

    assert "可委派 Agent 目录" not in blocks[0].text


def _loop_runtime(llm: _FakeLLM, delegate_catalog: str | None) -> AgentRuntime:
    """绕过 __init__ 组装非极速模式 _run_llm_loop 所需的最小运行时。"""
    runtime = AgentRuntime.__new__(AgentRuntime)
    runtime.db = SimpleNamespace(commit=AsyncMock())
    runtime.workspace_id = 2
    runtime.mode_slug = "standard"
    runtime.active_agent_id = None
    runtime.active_agent_prompt = None
    runtime.active_skill_ids = None
    runtime.delegate_catalog = delegate_catalog
    runtime._database_model = True
    runtime.llm = llm
    runtime.bus = SimpleNamespace(emit=AsyncMock())
    runtime.tool_registry = build_tool_registry()
    runtime.session_service = SimpleNamespace(
        load_context_for_turn=AsyncMock(return_value=([], [], 1)),
        add_message=AsyncMock(return_value=SimpleNamespace(id=99)),
    )
    runtime.memory_recall_service = SimpleNamespace(
        get_or_create_context=AsyncMock(return_value=None)
    )
    runtime.plan_service = SimpleNamespace(get_active=AsyncMock(return_value=None))
    runtime.compact_service = SimpleNamespace(maybe_compact=AsyncMock(return_value=[]))
    runtime._apply_stop_hooks = AsyncMock(return_value=None)
    return runtime


@pytest.mark.parametrize("catalog", [_CATALOG, None])
async def test_run_llm_loop_injects_loaded_catalog_only(
    monkeypatch: pytest.MonkeyPatch,
    catalog: str | None,
) -> None:
    monkeypatch.setattr("runtime.agent.SkillService.get_catalog", AsyncMock(return_value=""))
    llm = _FakeLLM()
    runtime = _loop_runtime(llm, catalog)

    result = await runtime._run_llm_loop(
        1, SimpleNamespace(system_prompt=None), SimpleNamespace(id=uuid4()), None
    )

    assert result.completed_message_id == 99
    stable = llm.system_prompts[0][0].text
    if catalog is None:
        assert DELEGATE_CATALOG_HEADER not in stable
    else:
        assert f"{DELEGATE_CATALOG_HEADER}\n{_CATALOG}" in stable


# ---------- _configure_agent:只在 lead 分支加载目录 ----------


def _install_agent_definition_service(
    monkeypatch: pytest.MonkeyPatch,
    entries: tuple[DelegateCatalogEntry, ...],
) -> list[int]:
    """替换运行时使用的 agent 域 Service,返回目录查询记录(按工作区 ID)。"""
    catalog_requests: list[int] = []

    class _FakeAgentDefinitionService:
        def __init__(self, db: object) -> None:
            pass

        async def list_delegate_catalog(
            self, workspace_id: int
        ) -> tuple[DelegateCatalogEntry, ...]:
            catalog_requests.append(workspace_id)
            return entries

        async def get_runtime_config(self, workspace_id: int, agent_id: int) -> AgentRuntimeConfig:
            return AgentRuntimeConfig(
                agent_id=agent_id,
                system_prompt="你是审稿人",
                model_name=None,
                tool_runtime_names=("Read",),
                skill_ids=(),
                skill_names=(),
                name="审稿人",
                description=None,
            )

    monkeypatch.setattr("runtime.agent.AgentDefinitionService", _FakeAgentDefinitionService)
    return catalog_requests


_ENTRIES = (
    DelegateCatalogEntry(agent_id=7, name="审稿人", description="负责\n审稿", tool_names=("Read",)),
    DelegateCatalogEntry(agent_id=9, name="写手", description=None, tool_names=()),
)


def _configure_runtime(workspace_id: int | None) -> AgentRuntime:
    db: Any = object()
    llm: Any = _FakeLLM()
    return AgentRuntime(db, user_id=1, workspace_id=workspace_id, llm=llm)


async def test_lead_session_loads_and_renders_delegate_catalog(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    requests = _install_agent_definition_service(monkeypatch, _ENTRIES)
    runtime = _configure_runtime(2)

    await runtime._configure_agent(SimpleNamespace(agent_id=None, mode_slug="standard"))

    assert requests == [2]
    # 一个 Agent 一行;描述里的换行压成空格,缺失的描述与工具给出占位。
    assert runtime.delegate_catalog == (
        "- agent_id=7 名称:审稿人 描述:负责 审稿 工具:Read\n"
        "- agent_id=9 名称:写手 描述:(无描述) 工具:(无)"
    )


async def test_bound_agent_session_gets_no_catalog(monkeypatch: pytest.MonkeyPatch) -> None:
    requests = _install_agent_definition_service(monkeypatch, _ENTRIES)
    runtime = _configure_runtime(2)

    await runtime._configure_agent(SimpleNamespace(agent_id=7, mode_slug="standard"))

    assert runtime.active_profile == "user_agent"
    assert runtime.delegate_catalog is None
    assert requests == []
    # 绑定了自建 Agent 的会话本就拿不到 Agent 工具,委派深度为 1。
    assert "Agent" not in runtime.tool_registry.names


@pytest.mark.parametrize(("workspace_id", "entries"), [(None, _ENTRIES), (2, ())])
async def test_no_workspace_or_no_enabled_agent_means_no_catalog(
    monkeypatch: pytest.MonkeyPatch,
    workspace_id: int | None,
    entries: tuple[DelegateCatalogEntry, ...],
) -> None:
    _install_agent_definition_service(monkeypatch, entries)
    runtime = _configure_runtime(workspace_id)

    await runtime._configure_agent(SimpleNamespace(agent_id=None, mode_slug="standard"))

    assert runtime.delegate_catalog is None


# ---------- _execute_tool_calls:透传本轮实际生效的工具名单 ----------


def _tool_runtime(monkeypatch: pytest.MonkeyPatch, active_plan: Any) -> tuple[AgentRuntime, list]:
    """真实 __init__ 的 lead 运行时;Plan 状态、动态权限与工具执行打桩,返回网关调用记录。"""
    runtime = _configure_runtime(2)
    runtime.plan_service = SimpleNamespace(get_active=AsyncMock(return_value=active_plan))
    runtime.session_service = SimpleNamespace(add_message=AsyncMock())
    runtime.bus = SimpleNamespace(emit=AsyncMock())
    runtime.policy_engine.authorize = AsyncMock(  # type: ignore[method-assign]
        return_value=ToolAccessDecision("Agent", "lead", True, "allow")
    )
    gateway_calls: list[dict[str, Any]] = []

    async def _gateway_run(session_id, tool_name, input_args, **kwargs):
        gateway_calls.append({"tool_name": tool_name, **kwargs})
        return "ok"

    monkeypatch.setattr(runtime.tool_gateway, "run", _gateway_run)
    return runtime, gateway_calls


async def _execute(runtime: AgentRuntime, tool_input: dict[str, Any]) -> None:
    await runtime._execute_tool_calls(
        1,
        SimpleNamespace(),
        SimpleNamespace(id=uuid4()),
        ORCHESTRATOR_ACTOR,
        99,
        [ToolCall(id="t1", tool_name="Agent", arguments=tool_input)],
        done_ids=[],
        additional_context=None,
    )


async def test_normal_round_tool_names_reach_the_tool(monkeypatch: pytest.MonkeyPatch) -> None:
    runtime, calls = _tool_runtime(monkeypatch, active_plan=None)

    await _execute(runtime, {"agent_id": 7, "prompt": "审稿"})

    round_names = calls[0]["round_tool_names"]
    assert round_names == runtime.tool_registry.without("ExitPlanMode", "WritePlan").names
    assert {"Agent", "Write"} <= round_names


async def test_plan_mode_round_tool_names_are_read_only(monkeypatch: pytest.MonkeyPatch) -> None:
    runtime, calls = _tool_runtime(monkeypatch, active_plan=SimpleNamespace(content="计划"))

    await _execute(runtime, {"agent_type": "Explore", "prompt": "找代码"})

    round_names = calls[0]["round_tool_names"]
    assert round_names == runtime.tool_registry.only(*PLAN_MODE_ALLOWED_TOOLS).names
    assert "Write" not in round_names


async def test_plan_mode_rejects_delegation_before_execution(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Plan Mode 原有守卫只放行 Explore/Plan,委派自建 Agent 在执行前就被拒绝。"""
    runtime, calls = _tool_runtime(monkeypatch, active_plan=SimpleNamespace(content="计划"))

    await _execute(runtime, {"agent_id": 7, "prompt": "审稿"})

    assert calls == []
    event = runtime.bus.emit.await_args.args[0].to_dict()
    assert event["type"] == "tool_result"
    assert event["tool"]["is_error"] is True
    assert "Plan Mode只能派发Explore或Plan只读子代理" in event["tool"]["output"]
