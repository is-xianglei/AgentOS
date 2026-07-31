"""SubAgent 规格注册表与输出协议解析。

集中登记四种内置 SubAgent 的提示词、工具权限、模型档位、轮次上限与 shell
访问级别。Agent 工具按 agent_type 取出 SubAgentSpec 交给 SubAgentRunner 执行。

工具权限用黑名单而非白名单表达能力边界(与参考实现一致):Explore / Plan /
verification 都需要 shell 跑 git diff、find 与测试,禁 shell 会直接废掉它们的
核心能力,因此只读由 tools/shell_policy.py 在执行边界按命令强制。
"""

from __future__ import annotations

import re

from core.config import settings
from core.errors import AgentException
from tools.shell_policy import ShellAccess
from tools.subagents.definition import AgentType, SubAgentSpec
from tools.subagents.prompts import (
    EXPLORE_PROMPT,
    EXPLORE_WHEN_TO_USE,
    GENERAL_PURPOSE_PROMPT,
    GENERAL_PURPOSE_WHEN_TO_USE,
    PLAN_CRITICAL_FILES_HEADING,
    PLAN_PROMPT,
    PLAN_WHEN_TO_USE,
    VERDICT_PREFIX,
    VERIFICATION_CRITICAL_REMINDER,
    VERIFICATION_PROMPT,
    VERIFICATION_WHEN_TO_USE,
)

# 只读检索类共用的工具集:能读能搜能跑只读 shell,不能写。
_READ_ONLY_TOOLS: tuple[str, ...] = ("Read", "Glob", "Grep", "Bash")

# 写工具黑名单。工具层直接不授予,shell 层再拦重定向等旁路。
_WRITE_TOOLS: tuple[str, ...] = ("Write", "Edit", "SkillRun")


def _build_specs() -> dict[AgentType, SubAgentSpec]:
    """构造四种内置规格。模型档位从配置读取,便于按环境调整而不散落硬编码。"""
    return {
        AgentType.GENERAL_PURPOSE: SubAgentSpec(
            agent_type=AgentType.GENERAL_PURPOSE,
            system_prompt=GENERAL_PURPOSE_PROMPT,
            when_to_use=GENERAL_PURPOSE_WHEN_TO_USE,
            # 除全局禁用外全部可用;危险工具仍逐次经 permission 裁决。
            allowed_tools=None,
            # 不在此处写死:resolve_model() 已把 subagent_default_model 作为兜底。
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
            # 速度优先档位。留空则回落主会话模型。
            model=settings.subagent_fast_model,
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


# ============ 输出协议解析 ============

# VERDICT 锚点:必须独占一行。允许行首尾空白与可选加粗残留,但不接受行内混排,
# 否则「报告正文提到 VERDICT: PASS」会被误判为结论。
_VERDICT_RE = re.compile(
    rf"^\s*\**\s*{re.escape(VERDICT_PREFIX)}\s*(PASS|FAIL|PARTIAL)\s*\**\s*\.?\s*$",
    re.MULTILINE,
)

# 命令证据块:提示词要求「执行的命令」+ 代码围栏。缺失即视为无证据。
_COMMAND_EVIDENCE_RE = re.compile(r"执行的命令[:：]\s*\**\s*\n*\s*```")
_BACKTICK_RE = re.compile(r"`([^`]+)`")


def parse_verdict(report: str) -> str | None:
    """解析 verification 报告末尾的判定值,取最后一个匹配。

    找不到合规锚点返回 None,调用方应视为「未给出判定」而非默认通过。
    """
    matches = _VERDICT_RE.findall(report or "")
    return matches[-1] if matches else None


def count_command_evidence(report: str) -> int:
    """统计报告中带代码围栏的命令证据块数量。"""
    return len(_COMMAND_EVIDENCE_RE.findall(report or ""))


def validate_verification_report(report: str) -> tuple[str, str | None]:
    """校验 verification 报告,返回(有效判定, 驳回原因)。

    无命令证据的 PASS 会被降级为 PARTIAL:参考实现规定「没有命令块的 PASS 视为
    跳过」,若原样透出 PASS,主代理会据一份没有执行证据的报告宣布完成。
    """
    verdict = parse_verdict(report)
    if verdict is None:
        return "PARTIAL", "报告未给出合规的 VERDICT 单行判定,按 PARTIAL 处理"
    if verdict == "PASS" and count_command_evidence(report) == 0:
        return "PARTIAL", "报告给出 PASS 但不含任何命令执行证据,降级为 PARTIAL"
    return verdict, None


def parse_plan_critical_files(report: str) -> list[str]:
    """解析 Plan 报告结尾的「实施关键文件」清单条目。"""
    index = (report or "").rfind(PLAN_CRITICAL_FILES_HEADING)
    if index < 0:
        return []
    tail = report[index + len(PLAN_CRITICAL_FILES_HEADING):]
    items: list[str] = []
    for line in tail.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        if stripped.startswith("#"):
            break
        if stripped.startswith(("-", "*", "+")):
            path = _extract_path(stripped[1:].strip())
            if path:
                items.append(path)
    return items


def _extract_path(entry: str) -> str:
    """从清单条目里取出纯路径,便于调用方直接喂给 Read。

    条目形如 `` `path/to/file.py` — 说明``:优先取反引号内内容,
    否则退回首个空白分隔的片段并剥掉行尾标点。
    """
    quoted = _BACKTICK_RE.search(entry)
    if quoted is not None:
        return quoted.group(1).strip()
    head = entry.split()[0] if entry.split() else ""
    return head.rstrip(":,;。:,").strip()


def routing_catalog() -> str:
    """给主代理的路由说明:每种类型的用途与实际工具范围。"""
    lines: list[str] = []
    for spec in SUBAGENT_SPECS.values():
        tools = "除全局禁用外全部工具" if spec.allowed_tools is None else "、".join(
            spec.resolve_tools_names()
        )
        lines.append(f"- {spec.agent_type.value}: {spec.when_to_use} 可用工具: {tools}。")
    return "\n".join(lines)
