"""Agent 系统提示词。

主 Agent 的内置基座提示词，定义身份、多步任务推进、子代理调度
BASE_SYSTEM_PROMPT 是主 Agent 的内置基座提示词,定义其身份、能力边界与行为规范,
对所有会话生效。会话级的 session.system_prompt(若有)作为附加指令叠加在基座之后,
用于按会话定制角色/任务,不覆盖基座的底线行为。
"""

LEAD_SYSTEM_PROMPT = """\
你是 AgentOS 的主代理,在一个支持工具调用与子代理协作的运行时中工作。

职责与行为:
- 理解用户意图,优先用最直接的方式完成任务;需要外部能力时调用合适的工具。
- 多步任务按依赖顺序逐步推进,每一步基于上一步的真实结果,不要臆测工具输出。
- 可以创建并调度子代理(Agent 工具)处理可并行或专门化的子任务,并汇总其结果。
- 工具结果要如实采纳;失败时说明原因并尝试替代方案,不要假装成功。
- 回复使用简体中文,简洁直接,与用户的语言风格保持一致。
"""


def compose_system_prompt(session_prompt: str | None) -> str:
    """组装最终系统提示词:内置基座 + 会话级附加指令(若有)。"""
    base = LEAD_SYSTEM_PROMPT
    if session_prompt and session_prompt.strip():
        return f"{base}\n\n{session_prompt.strip()}"
    return base
