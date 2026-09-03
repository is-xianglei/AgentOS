"""SubAgentSpec.runtime_name 的回归测试。

runtime_name 是子代理的运行标识(运行记录、事件、权限、日志共用)。内置 spec 不传
display_name 时必须逐字节回落到 agent_type 枚举值,保证改造前后行为一致;
动态构造的 spec(如 workflow 成员)传入 display_name 后以其为标识。
"""

import pytest

from tools.subagents.definition import AgentType, SubAgentSpec
from tools.subagents.registry import get_subagent_spec


@pytest.mark.parametrize("agent_type", list(AgentType))
def test_builtin_spec_runtime_name_falls_back_to_enum_value(agent_type: AgentType) -> None:
    spec = get_subagent_spec(agent_type)
    assert spec.display_name == ""
    assert spec.runtime_name == agent_type.value


def test_explore_spec_runtime_name_is_explore() -> None:
    assert get_subagent_spec(AgentType.EXPLORE).runtime_name == "Explore"


def test_display_name_overrides_runtime_name() -> None:
    spec = SubAgentSpec(
        agent_type=AgentType.GENERAL_PURPOSE,
        system_prompt="测试用人格",
        display_name="wf-测试",
    )
    assert spec.runtime_name == "wf-测试"
    # agent_type 只表示能力档位,不受 display_name 影响。
    assert spec.agent_type is AgentType.GENERAL_PURPOSE
