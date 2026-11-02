from pathlib import Path
from unittest.mock import MagicMock

import pytest

from core.errors import AgentException
from tools.base import ToolContext
from tools.builtin.grep import GrepInput, GrepTool

pytestmark = pytest.mark.anyio


def _tool_context() -> ToolContext:
    return ToolContext(session_id=1, db=MagicMock())


async def test_grep_returns_matching_lines(tmp_path: Path) -> None:
    source_file = tmp_path / "example.py"
    source_file.write_text("first\nneedle\nlast\n", encoding="utf-8")

    result = await GrepTool().run(
        GrepInput(pattern="needle", path=str(tmp_path), glob_pattern="*.py"),
        _tool_context(),
    )

    assert "找到 1 个匹配项" in result
    assert f"{source_file}:2:needle" in result


async def test_grep_returns_context_lines(tmp_path: Path) -> None:
    source_file = tmp_path / "example.txt"
    source_file.write_text("before\nneedle\nafter\n", encoding="utf-8")

    result = await GrepTool().run(
        GrepInput(pattern="needle", path=str(source_file), context_lines=1),
        _tool_context(),
    )

    assert f"{source_file}:" in result
    assert "  1: before" in result
    assert "> 2: needle" in result
    assert "  3: after" in result


async def test_grep_stops_at_result_limit(tmp_path: Path) -> None:
    source_file = tmp_path / "example.txt"
    source_file.write_text("needle\nneedle\nneedle\n", encoding="utf-8")

    result = await GrepTool().run(
        GrepInput(pattern="needle", path=str(source_file), max_results=2),
        _tool_context(),
    )

    assert "找到 2 个匹配项 (已达上限 2)" in result
    assert result.count(str(source_file)) == 2


async def test_grep_rejects_invalid_pattern(tmp_path: Path) -> None:
    with pytest.raises(AgentException, match="无效的正则表达式"):
        await GrepTool().run(
            GrepInput(pattern="[", path=str(tmp_path)),
            _tool_context(),
        )


async def test_grep_rejects_missing_path(tmp_path: Path) -> None:
    missing_path = tmp_path / "missing"

    with pytest.raises(AgentException, match="路径不存在"):
        await GrepTool().run(
            GrepInput(pattern="needle", path=str(missing_path)),
            _tool_context(),
        )
