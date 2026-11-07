"""官方预置目录读取与校验的单元测试：用 tmp_path 构造临时预置目录，不连接数据库。"""

from pathlib import Path

import pytest

import database.registry  # noqa: F401  构造 ORM 相关对象前需全部映射在册
from core.errors import AgentException
from market.presets import PRESETS_DIR, check_preset_tools, load_presets
from skill.parser import SkillBundle
from tools.service import ToolKeyResolution


def _write_skill(
    root: Path,
    dir_name: str,
    *,
    name: str | None = None,
    category: str | None = None,
    extra_files: dict[str, str] | None = None,
) -> None:
    """在 root/skills 下写一个 Skill 预置；name 默认与目录名相同。"""
    skill_dir = root / "skills" / dir_name
    skill_dir.mkdir(parents=True)
    lines = ["---", f"name: {name or dir_name}", f"description: {dir_name} 的描述"]
    if category is not None:
        lines.append("metadata:")
        lines.append(f"  market_category: {category}")
    lines.extend(["---", "", "正文", ""])
    (skill_dir / "SKILL.md").write_text("\n".join(lines), encoding="utf-8")
    for relative_path, text in (extra_files or {}).items():
        path = skill_dir / relative_path
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")


def _write_agent(root: Path, key: str, text: str) -> None:
    agent_dir = root / "agents" / key
    agent_dir.mkdir(parents=True)
    (agent_dir / "AGENT.md").write_text(text, encoding="utf-8")


def _agent_md(
    *,
    name_line: str = "name: 示例助手",
    tools: str = "[Read, Grep]",
    skills: str = "[]",
    body: str = "你是示例助手。",
) -> str:
    return (
        f"---\n{name_line}\ndescription: 示例助手的描述\ncategory: office\n"
        f"tools: {tools}\nskills: {skills}\n---\n\n{body}\n"
    )


def _load_error(root: Path) -> AgentException:
    with pytest.raises(AgentException) as exc_info:
        load_presets(root)
    return exc_info.value


def test_load_presets_builds_snapshots_in_community_format(tmp_path: Path) -> None:
    _write_skill(
        tmp_path,
        "notes",
        category="office",
        extra_files={"templates/minutes.md": "# 模板\n"},
    )
    _write_skill(tmp_path, "alpha")
    # tools 有重复且未排序，skills 也未排序：快照里要和 AgentDefinitionService.build_snapshot 一样排好序。
    _write_agent(tmp_path, "helper", _agent_md(tools="[Read, Grep, Read]", skills="[notes, alpha]"))

    presets = load_presets(tmp_path)

    by_key = {(preset.kind, preset.key): preset for preset in presets}
    assert list(by_key) == [("skill", "alpha"), ("skill", "notes"), ("agent", "helper")]
    notes = by_key[("skill", "notes")]
    assert notes.category == "office"
    assert by_key[("skill", "alpha")].category == "other"
    skill_snapshot = notes.snapshot["skill"]
    bundle = SkillBundle.from_content(skill_snapshot["content"], skill_snapshot["extra_files"])
    assert set(skill_snapshot) == {"name", "content", "extra_files", "skill_hash"}
    assert skill_snapshot["skill_hash"] == bundle.skill_hash
    assert skill_snapshot["extra_files"] == {
        "templates/minutes.md": {"encoding": "utf-8", "content": "# 模板\n"}
    }

    helper = by_key[("agent", "helper")]
    assert helper.category == "office"
    assert helper.snapshot["agent"] == {
        "name": "示例助手",
        "description": "示例助手的描述",
        "system_prompt": "你是示例助手。",
        "tool_keys": ["Grep", "Read"],
    }
    embedded = helper.snapshot["skills"]
    assert [skill["name"] for skill in embedded] == ["alpha", "notes"]
    # 内嵌的 Skill 内容取自 presets/skills，但和官方 Skill 条目的快照是各自独立的对象。
    assert embedded[1] == skill_snapshot
    assert embedded[1] is not skill_snapshot


def test_shipped_presets_are_valid() -> None:
    presets = load_presets(PRESETS_DIR)

    by_key = {(preset.kind, preset.key): preset for preset in presets}
    assert ("skill", "meeting-minutes") in by_key
    agent = by_key[("agent", "meeting-assistant")]
    assert [skill["name"] for skill in agent.snapshot["skills"]] == ["meeting-minutes"]
    assert agent.snapshot["agent"]["tool_keys"] == ["Glob", "Grep", "Read"]


def test_missing_root_is_rejected(tmp_path: Path) -> None:
    error = _load_error(tmp_path / "missing")
    assert "根目录不存在" in error.message


@pytest.mark.parametrize(
    ("agent_md", "expected"),
    [
        ("name: 示例助手\n\n你是示例助手。\n", "缺少 YAML frontmatter"),
        ("---\nname: [未闭合\n---\n\n正文\n", "不是合法的 YAML"),
        ("---\n- 列表\n---\n\n正文\n", "key: value 结构"),
        (_agent_md(name_line="title: 示例助手"), "name 字段缺失"),
        (_agent_md(name_line="name: 123"), "name 字段缺失"),
        (_agent_md(body=""), "正文为空"),
        (_agent_md(tools="Read"), "tools 字段必须是列表"),
        (_agent_md(skills="[1]"), "skills 字段只能包含非空字符串"),
        (_agent_md(skills="[missing-skill]"), "skills 字段引用的 Skill 不在 presets/skills 里"),
        (_agent_md().replace("category: office", "category: unknown"), "category 字段"),
    ],
    ids=[
        "no-frontmatter",
        "bad-yaml",
        "not-mapping",
        "missing-name",
        "name-not-string",
        "empty-body",
        "tools-not-list",
        "skills-not-string",
        "unknown-skill",
        "unknown-category",
    ],
)
def test_invalid_agent_preset_is_rejected(tmp_path: Path, agent_md: str, expected: str) -> None:
    _write_agent(tmp_path, "broken", agent_md)

    error = _load_error(tmp_path)

    # 错误里要指明是哪个预置。
    assert "agents/broken" in error.message
    assert expected in error.message


def test_agent_without_agent_md_is_rejected(tmp_path: Path) -> None:
    (tmp_path / "agents" / "empty").mkdir(parents=True)

    error = _load_error(tmp_path)

    assert "agents/empty" in error.message
    assert "缺少 AGENT.md" in error.message


def test_skill_dir_name_must_match_skill_name(tmp_path: Path) -> None:
    _write_skill(tmp_path, "notes", name="other-name")

    error = _load_error(tmp_path)

    assert "skills/notes" in error.message
    assert "目录名必须与 SKILL.md 里的 name 一致" in error.message


def test_skill_without_skill_md_is_rejected(tmp_path: Path) -> None:
    (tmp_path / "skills" / "empty").mkdir(parents=True)

    error = _load_error(tmp_path)

    assert "skills/empty" in error.message
    assert "缺少 SKILL.md" in error.message


def test_skill_preset_goes_through_skill_parser(tmp_path: Path) -> None:
    # 名称不合规由 skill 域的 parse_skill_zip 拒绝，错误里带上预置位置。
    _write_skill(tmp_path, "Bad_Name")

    error = _load_error(tmp_path)

    assert "skills/Bad_Name" in error.message
    assert "name 只能由小写字母" in error.message


def test_skill_preset_with_unknown_category_is_rejected(tmp_path: Path) -> None:
    _write_skill(tmp_path, "notes", category="unknown")

    error = _load_error(tmp_path)

    assert "skills/notes" in error.message
    assert "metadata.market_category" in error.message


def test_skill_preset_with_credential_file_is_rejected(tmp_path: Path) -> None:
    _write_skill(tmp_path, "notes", extra_files={"config/.env": "TOKEN=x\n"})

    error = _load_error(tmp_path)

    assert "skills/notes" in error.message
    assert "notes/config/.env" in error.message


class _FakeToolCatalog:
    """只实现 resolve_tool_keys 的工具目录替身：Read 可用，其余都不存在。"""

    def __init__(self) -> None:
        self.profiles: list[str] = []

    async def resolve_tool_keys(self, tool_keys: list[str], *, profile: str) -> ToolKeyResolution:
        self.profiles.append(profile)
        tool_ids: dict[str, int] = {}
        unavailable: dict[str, str] = {}
        for tool_key in tool_keys:
            if tool_key == "Read":
                tool_ids[tool_key] = 1
            else:
                unavailable[tool_key] = "not_found"
        return ToolKeyResolution(tool_ids=tool_ids, unavailable=unavailable)


@pytest.mark.anyio
async def test_check_preset_tools_rejects_unavailable_tool(tmp_path: Path) -> None:
    _write_agent(tmp_path, "ok", _agent_md(tools="[Read]"))
    _write_agent(tmp_path, "uses-missing", _agent_md(tools="[Read, NoSuchTool]"))
    presets = load_presets(tmp_path)
    catalog = _FakeToolCatalog()

    with pytest.raises(AgentException) as exc_info:
        await check_preset_tools(presets, catalog)

    message = exc_info.value.message
    assert "agents/uses-missing" in message
    assert "tools 字段" in message
    assert "NoSuchTool（工具不存在）" in message
    # 口径与市场安装一致：按自建 Agent 的档位解析。
    assert set(catalog.profiles) == {"user_agent"}


@pytest.mark.anyio
async def test_check_preset_tools_passes_when_all_tools_available(tmp_path: Path) -> None:
    _write_agent(tmp_path, "ok", _agent_md(tools="[Read]"))

    await check_preset_tools(load_presets(tmp_path), _FakeToolCatalog())
