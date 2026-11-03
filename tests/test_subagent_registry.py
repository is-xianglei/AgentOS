"""Plan「实施关键文件」清单条目的路径提取回归测试。

Plan 报告多为中文输出,行尾常带全角标点;剥不掉会让路径带着「：」「，」交给 Read,
直接读不到文件。
"""

import pytest

from tools.subagents.prompts import PLAN_CRITICAL_FILES_HEADING
from tools.subagents.registry import _extract_path, parse_plan_critical_files


@pytest.mark.parametrize(
    ("entry", "expected"),
    [
        ("`foo/bar.py` — 入口模块", "foo/bar.py"),
        ("foo/bar.py: 入口模块", "foo/bar.py"),
        ("foo/bar.py, 入口模块", "foo/bar.py"),
        ("foo/bar.py;", "foo/bar.py"),
        ("foo/bar.py：", "foo/bar.py"),
        ("foo/bar.py，", "foo/bar.py"),
        ("foo/bar.py； 入口模块", "foo/bar.py"),
        ("foo/bar.py。", "foo/bar.py"),
        ("", ""),
        ("   ", ""),
    ],
)
def test_extract_path_strips_trailing_punctuation(entry: str, expected: str) -> None:
    assert _extract_path(entry) == expected


def test_parse_plan_critical_files_strips_fullwidth_punctuation() -> None:
    report = (
        "方案正文\n\n"
        f"{PLAN_CRITICAL_FILES_HEADING}\n"
        "- `session/service.py` — 事务边界\n"
        "- tools/subagents/trigger.py： 阈值判定\n"
        "- runtime/agent.py， 回合主循环\n"
    )

    assert parse_plan_critical_files(report) == [
        "session/service.py",
        "tools/subagents/trigger.py",
        "runtime/agent.py",
    ]
