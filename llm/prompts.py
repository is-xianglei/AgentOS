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


def compose_system_prompt(
    session_prompt: str | None, skills_catalog: str | None = None
) -> str:
    """组装最终系统提示词:内置基座 + 可用 skills 清单(若有)+ 会话级附加指令(若有)。

    skills_catalog 缺省 None 时行为与旧实现一致(向后兼容);非空时把 skill 清单
    拼在基座之后、会话附加之前,作为渐进式披露第一层(发现)。
    """
    parts = [LEAD_SYSTEM_PROMPT]
    if skills_catalog and skills_catalog.strip():
        parts.append(
            "## 可用 skills\n"
            "以下 skill 可按需加载(调用 Skill 工具取回完整说明后再执行):\n"
            f"{skills_catalog.strip()}"
        )
    if session_prompt and session_prompt.strip():
        parts.append(session_prompt.strip())
    return "\n\n".join(parts)
