"""verification 触发阈值的运行时判定。

阈值只写在提示词里等于"建议",长上下文下会被稀释;这里把它做成回合结束前的
一次结构化检查:扫描本回合实际发生的工具调用,若已构成非平凡改动且尚未走过
独立验收,就注入一条路由提示强制续跑。判定的是"是否提示",不是"代为执行"——
是否调用仍由主代理决定,避免把编排权从模型手里拿走。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

# 产生文件改动的工具名。只认写入类,Read/Grep 不计入阈值。
MUTATING_TOOLS: frozenset[str] = frozenset({"Write", "Edit", "MultiEdit", "NotebookEdit"})

# 阈值:改动文件数达到该值即视为非平凡。与提示词里的"3 个及以上"保持一致。
FILE_COUNT_THRESHOLD = 3

# 后端 / API 变更的路径特征。命中任一即视为需验收,不再看文件数。
BACKEND_PATH_HINTS: tuple[str, ...] = (
    "api/",
    "service.py",
    "repository.py",
    "models.py",
    "schemas.py",
)

# 基础设施变更的路径特征:迁移、依赖、编排与 CI 配置。
INFRA_PATH_HINTS: tuple[str, ...] = (
    "alembic/",
    "pyproject.toml",
    "uv.lock",
    "dockerfile",
    "docker-compose",
    ".github/workflows/",
    "database/",
)


@dataclass(frozen=True)
class TriggerAssessment:
    """一次阈值判定的结果,便于日志与测试观察到"为什么触发"。"""

    should_verify: bool
    changed_files: tuple[str, ...]
    reasons: tuple[str, ...]

    @property
    def reason_text(self) -> str:
        return "、".join(self.reasons)


def _iter_tool_uses(messages: list[dict[str, Any]] | None):
    """从消息上下文里取出所有 tool_use 块,忽略结构不符的条目。"""
    for message in messages or []:
        content = message.get("content")
        if not isinstance(content, list):
            continue
        for block in content:
            if isinstance(block, dict) and block.get("type") == "tool_use":
                yield block


def _extract_file_path(tool_input: dict[str, Any]) -> str | None:
    """兼容不同写入类工具的路径字段名。"""
    for key in ("file_path", "path", "notebook_path"):
        value = tool_input.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def _matches(path: str, hints: tuple[str, ...]) -> bool:
    lowered = path.lower()
    return any(hint in lowered for hint in hints)


def verification_already_ran(messages: list[dict[str, Any]] | None) -> bool:
    """本回合是否已派发过 verification 子代理。

    只认 Agent 工具且 subagent_type 为 verification 的调用:阈值提示是"补一次
    验收",已经做过就不该再提示,否则回合永远结束不了。
    """
    for block in _iter_tool_uses(messages):
        if block.get("name") != "Agent":
            continue
        tool_input = block.get("input")
        if isinstance(tool_input, dict) and tool_input.get("subagent_type") == "verification":
            return True
    return False


def assess(messages: list[dict[str, Any]] | None) -> TriggerAssessment:
    """扫描本回合上下文,判定是否已达到需要独立验收的门槛。"""
    changed: list[str] = []
    for block in _iter_tool_uses(messages):
        if block.get("name") not in MUTATING_TOOLS:
            continue
        tool_input = block.get("input")
        if not isinstance(tool_input, dict):
            continue
        path = _extract_file_path(tool_input)
        # 同一文件多次编辑只算一处改动,否则单文件反复改也会误触阈值。
        if path is not None and path not in changed:
            changed.append(path)

    reasons: list[str] = []
    if len(changed) >= FILE_COUNT_THRESHOLD:
        reasons.append(f"改动文件数 {len(changed)} 已达 {FILE_COUNT_THRESHOLD} 个阈值")
    if any(_matches(path, BACKEND_PATH_HINTS) for path in changed):
        reasons.append("包含后端 / API 变更")
    if any(_matches(path, INFRA_PATH_HINTS) for path in changed):
        reasons.append("包含基础设施变更")

    return TriggerAssessment(
        should_verify=bool(reasons) and not verification_already_ran(messages),
        changed_files=tuple(changed),
        reasons=tuple(reasons),
    )


def compose_hint(assessment: TriggerAssessment) -> str:
    """把判定结果写成给主代理的路由提示。

    提示里直接把变更清单列全:verification 要求调用方提供文件清单,这里预先
    备好,免得主代理为凑参数再翻一遍历史。措辞保留"若确认无需验收请说明理由",
    给出正当的跳过路径,避免把提示变成死循环。
    """
    files = "\n".join(f"- {path}" for path in assessment.changed_files)
    return (
        f"[系统检查] 本回合已产生非平凡改动({assessment.reason_text})。\n"
        f"变更文件清单:\n{files}\n\n"
        "按验收规则,此类改动需要一次独立验收:请调用 Agent 工具、"
        "subagent_type 为 verification,并提供原始需求、上述变更文件清单、"
        "实现方法与已知风险。若确认本次确实无需验收,请说明理由后结束。"
    )
