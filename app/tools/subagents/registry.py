"""SubAgent 规格注册表。

集中登记四种内置 SubAgent 的 system_prompt 与工具权限。Agent 工具按
agent_type 取出对应 SubAgentSpec 交给 SubAgentRunner 执行。

注意:除 general_purpose 外,其余三种的 system_prompt 与 allowed_tools 仍是
占位(见 TODO),待按各自定位补全。
"""

from app.core.errors import ValidationAppError
from app.tools.subagents.definition import AgentType, SubAgentSpec

# 通用子代理提示词(沿用 SubAgentRunner 原有文案)。
_GENERAL_PURPOSE_PROMPT = """
你是会话内的子代理。完成分派任务后，用简洁报告回复。
不要创建新的子代理；需要工具时只使用允许的工具。
"""

# TODO: 按「只读探索、定位代码、不做修改」的定位补全 Explore 提示词与白名单工具。
_EXPLORE_PROMPT = """
你是只读探索子代理。# TODO: 补全提示词。
"""

# TODO: 按「产出实现方案、不写代码」的定位补全 Plan 提示词与白名单工具。
_PLAN_PROMPT = """
你是方案规划子代理。# TODO: 补全提示词。
"""

# TODO: 按「跑测试 / 验证改动是否符合预期」的定位补全 Verification 提示词与工具。
_VERIFICATION_PROMPT = """
你是验证子代理。# TODO: 补全提示词。
"""


SUBAGENT_SPECS: dict[AgentType, SubAgentSpec] = {
    AgentType.GENERAL_PURPOSE: SubAgentSpec(
        agent_type=AgentType.GENERAL_PURPOSE,
        system_prompt=_GENERAL_PURPOSE_PROMPT,
        # 通用子代理:除黑名单外全部工具可用。
        allowed_tools=None,
    ),
    AgentType.EXPLORE: SubAgentSpec(
        agent_type=AgentType.EXPLORE,
        system_prompt=_EXPLORE_PROMPT,
        # TODO: 收紧为只读工具白名单(禁 Write/Edit 等写操作)。
        allowed_tools=None,
    ),
    AgentType.PLAN: SubAgentSpec(
        agent_type=AgentType.PLAN,
        system_prompt=_PLAN_PROMPT,
        # TODO: 限定为规划所需的只读工具。
        allowed_tools=None,
    ),
    AgentType.VERIFICATION: SubAgentSpec(
        agent_type=AgentType.VERIFICATION,
        system_prompt=_VERIFICATION_PROMPT,
        # TODO: 限定为验证所需工具(测试 / 读取)。
        allowed_tools=None,
    ),
}


def get_subagent_spec(agent_type: AgentType) -> SubAgentSpec:
    """按 agent_type 取出 SubAgent 规格,未知类型抛校验错误。"""
    spec = SUBAGENT_SPECS.get(agent_type)
    if spec is None:
        raise ValidationAppError("SUBAGENT_TYPE_UNKNOWN", f"未知子代理类型: {agent_type}")
    return spec
