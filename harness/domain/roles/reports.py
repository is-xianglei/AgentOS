"""子代理报告的输出协议：verification 的 VERDICT 判定与命令证据、Plan 的「实施关键文件」清单。

协议锚点写在子代理提示词里（harness/domain/prompts/subagents.py），这里按同一份常量解析，
改动任何一边都要同步另一边。
"""

import re

from harness.domain.prompts.subagents import PLAN_CRITICAL_FILES_HEADING, VERDICT_PREFIX

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
    words = entry.split()
    if not words:
        return ""
    # 半角与全角的冒号、逗号、分号都要剥:Plan 报告多为中文输出,全角分号与全角
    # 冒号/逗号同属清单行尾常见分隔符。句点只剥全角,半角 "." 可能属于路径本身。
    return words[0].rstrip(":,;：，；。").strip()
