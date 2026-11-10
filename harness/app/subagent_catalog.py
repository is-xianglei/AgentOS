"""内置子代理目录：四种内置规格，按类型取规格，以及给主代理的路由说明。

规格的提示词、工具权限、模型档位与 shell 访问级别是纯数据，轮次上限读配置，所以目录放在应用层，
领域层只保留 SubAgentSpec 的类型定义。Agent 工具按 agent_type 取出规格交给 SubAgentRunner 执行。

工具权限用黑名单而非白名单表达能力边界(与参考实现一致):Explore / Plan /
verification 都需要 shell 跑 git diff、find 与测试,禁 shell 会直接废掉它们的
核心能力,因此只读由 harness/adapters/tools/builtin/shell_policy.py 在执行边界按命令强制。
"""

from typing import TYPE_CHECKING

from core.config import settings
from core.errors import AgentException
from harness.domain.prompts.subagents import (
    EXPLORE_PROMPT,
    EXPLORE_WHEN_TO_USE,
    GENERAL_PURPOSE_PROMPT,
    GENERAL_PURPOSE_WHEN_TO_USE,
    PLAN_PROMPT,
    PLAN_WHEN_TO_USE,
    VERIFICATION_CRITICAL_REMINDER,
    VERIFICATION_PROMPT,
    VERIFICATION_WHEN_TO_USE,
)
from harness.domain.roles.subagent import AgentType, SubAgentSpec, render_routing_catalog
from harness.domain.tool import ShellAccess, ToolProfile

if TYPE_CHECKING:
    # 仅作类型标注：agent 域经 tools 域加载内置工具，顶层导入会与 Agent 工具成环。
    from agent.service import AgentRuntimeConfig

# 只读检索类共用的工具集:能读能搜能跑只读 shell,不能写。
_READ_ONLY_TOOLS: tuple[str, ...] = ("Read", "Glob", "Grep", "Bash")

# 写工具黑名单。工具层直接不授予,shell 层再拦重定向等旁路。
_WRITE_TOOLS: tuple[str, ...] = ("Write", "Edit", "SkillRun")


def _build_specs() -> dict[AgentType, SubAgentSpec]:
    """构造四种内置规格。轮次等参数从配置读取;模型档位由 model_scenario 在调用期惰性解析。"""
    return {
        AgentType.GENERAL_PURPOSE: SubAgentSpec(
            agent_type=AgentType.GENERAL_PURPOSE,
            system_prompt=GENERAL_PURPOSE_PROMPT,
            when_to_use=GENERAL_PURPOSE_WHEN_TO_USE,
            # 除全局禁用外全部可用;危险工具仍逐次经 permission 裁决。
            allowed_tools=None,
            # 不在此处写死:默认档模型由 SubAgentRunner 的客户端按 subagent 场景解析。
            model=None,
            max_rounds=settings.subagent_max_rounds,
            # shell 不额外限制,但 Bash 属危险工具,默认 ask 会坍缩为拒绝。
            shell_access=ShellAccess.FULL,
        ),
        AgentType.EXPLORE: SubAgentSpec(
            agent_type=AgentType.EXPLORE,
            system_prompt=EXPLORE_PROMPT,
            when_to_use=EXPLORE_WHEN_TO_USE,
            allowed_tools=_READ_ONLY_TOOLS,
            disallowed_tools=_WRITE_TOOLS,
            # 速度优先档:调用时按 {provider}_subagent_fast_model 场景惰性解析,
            # 未配置覆盖时回落 provider 主模型。
            model_scenario="subagent_fast",
            max_rounds=settings.subagent_max_rounds,
            shell_access=ShellAccess.READ_ONLY,
            # 只读检索不需要父会话的历史偏好,注入反而稀释任务并可能引入陈旧信息。
            omit_inherited_memory=True,
        ),
        AgentType.PLAN: SubAgentSpec(
            agent_type=AgentType.PLAN,
            system_prompt=PLAN_PROMPT,
            when_to_use=PLAN_WHEN_TO_USE,
            # 工具集与只读约束复用 Explore。
            allowed_tools=_READ_ONLY_TOOLS,
            disallowed_tools=_WRITE_TOOLS,
            # 方案质量依赖推理能力,继承主会话模型。
            model=None,
            max_rounds=settings.subagent_plan_max_rounds,
            shell_access=ShellAccess.READ_ONLY,
            omit_inherited_memory=True,
        ),
        AgentType.VERIFICATION: SubAgentSpec(
            agent_type=AgentType.VERIFICATION,
            system_prompt=VERIFICATION_PROMPT,
            when_to_use=VERIFICATION_WHEN_TO_USE,
            allowed_tools=_READ_ONLY_TOOLS,
            disallowed_tools=_WRITE_TOOLS,
            model=None,
            # 须跑构建 + 相关测试 + 全量测试 + lint + 对抗探测,轮次必须显著放宽。
            max_rounds=settings.subagent_verification_max_rounds,
            # 项目目录只读,仅临时目录可写(落短生命周期测试脚本)。
            shell_access=ShellAccess.TMP_WRITABLE,
            critical_reminder=VERIFICATION_CRITICAL_REMINDER,
        ),
    }


SUBAGENT_SPECS: dict[AgentType, SubAgentSpec] = _build_specs()


def get_subagent_spec(agent_type: AgentType) -> SubAgentSpec:
    """按 agent_type 取出 SubAgent 规格,未知类型抛校验错误。"""
    spec = SUBAGENT_SPECS.get(agent_type)
    if spec is None:
        raise AgentException.message(f"未知子代理类型: {agent_type}")
    return spec


def routing_catalog() -> str:
    """给主代理的路由说明:每种内置类型的用途与实际工具范围。"""
    return render_routing_catalog(SUBAGENT_SPECS.values())


def build_agent_spec(
    config: AgentRuntimeConfig,
    *,
    display_name: str,
    tool_profile: ToolProfile,
) -> SubAgentSpec:
    """把数据库 Agent 的运行配置桥接成 SubAgentSpec,Workflow 成员与默认 Agent 委派共用。

    agent_type 只表示能力档位,display_name 才是运行标识(落运行记录、发事件、判权限);
    tool_profile 决定工具按 workflow_node 还是 delegate 档位裁决。模型取 Agent 自身配置,
    是否生效由子代理运行器按会话模型选项决定,两条调用路径规则一致。
    """
    return SubAgentSpec(
        agent_type=AgentType.GENERAL_PURPOSE,
        display_name=display_name,
        agent_id=config.agent_id,
        system_prompt=config.system_prompt,
        allowed_tools=config.tool_runtime_names,
        model=config.model_name,
        tool_profile=tool_profile,
    )
