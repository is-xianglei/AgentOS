"""verification 触发阈值的去重与提示文案回归测试。

Agent 工具的入参字段是 agent_type(见 AgentToolInput);去重判定和给模型的提示
若写成别的字段名,已派发的 verification 会检测不到,阈值提示每次回合结束都重复注入。
"""

from typing import Any

import pytest

from llm.types import ToolUse, build_assistant_blocks
from tools.builtin.agent import AgentToolInput
from tools.subagents.definition import AgentType
from tools.subagents.trigger import assess, compose_hint, verification_already_ran


def _assistant(*tool_uses: ToolUse) -> dict[str, Any]:
    """按运行时落库路径构造中立 assistant 消息。"""
    return {"role": "assistant", "content": build_assistant_blocks("", list(tool_uses))}


def _agent_call(agent_type: AgentType) -> ToolUse:
    """用真实入参模型序列化 Agent 调用,字段名随 AgentToolInput 变化而变化。"""
    args = AgentToolInput(agent_type=agent_type, prompt="复核本次改动")
    return ToolUse(id="t-agent", name="Agent", input=args.model_dump(mode="json"))


# 单个后端文件即命中阈值,用于观察去重是否压住提示。
_BACKEND_WRITE = ToolUse(id="t-write", name="Write", input={"file_path": "user/service.py"})


def test_verification_dispatched_suppresses_hint() -> None:
    messages = [_assistant(_BACKEND_WRITE), _assistant(_agent_call(AgentType.VERIFICATION))]

    assert verification_already_ran(messages) is True
    assessment = assess(messages)
    # 阈值原因仍在,压住提示的是去重而不是阈值未达。
    assert assessment.reasons
    assert assessment.should_verify is False


@pytest.mark.parametrize(
    "agent_type",
    [item for item in AgentType if item is not AgentType.VERIFICATION],
)
def test_other_agent_type_does_not_count_as_verification(agent_type: AgentType) -> None:
    messages = [_assistant(_BACKEND_WRITE), _assistant(_agent_call(agent_type))]

    assert verification_already_ran(messages) is False
    assert assess(messages).should_verify is True


def test_no_agent_call_still_requires_verification() -> None:
    messages = [_assistant(_BACKEND_WRITE)]

    assert verification_already_ran(messages) is False
    assert assess(messages).should_verify is True


def test_wrong_field_name_is_not_verification() -> None:
    """subagent_type 不是 Agent 工具字段,这样的调用实际按默认类型执行,不算验收。"""
    wrong_field = ToolUse(
        id="t-agent",
        name="Agent",
        input={"subagent_type": "verification", "prompt": "复核本次改动"},
    )
    messages = [_assistant(_BACKEND_WRITE), _assistant(wrong_field)]

    assert verification_already_ran(messages) is False
    assert assess(messages).should_verify is True


def test_compose_hint_names_agent_type_field() -> None:
    assessment = assess([_assistant(_BACKEND_WRITE)])
    hint = compose_hint(assessment)

    assert "agent_type" in AgentToolInput.model_fields
    assert f"agent_type 为 {AgentType.VERIFICATION.value}" in hint
    assert "subagent_type" not in hint
