"""skill/parser.py 的纯函数测试:frontmatter 规则、zip 解析、序列化往返与哈希稳定性。"""

import io
import zipfile

import pytest

from core.errors import AgentException
from skill import parser
from skill.parser import (
    ENCODING_BASE64,
    ENCODING_UTF8,
    SkillBundle,
    build_skill_md,
    build_skill_zip,
    compute_skill_hash,
    normalize_relative_path,
    parse_skill_md,
    parse_skill_zip,
    validate_frontmatter,
)

SKILL_MD = "---\nname: demo\ndescription: 演示技能\nversion: 1.0.0\n---\n\n# 用法\n正文\n"


def _zip(files: dict[str, bytes | str], *, date_time=(2024, 1, 1, 0, 0, 0)) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        for path, data in files.items():
            raw = data.encode("utf-8") if isinstance(data, str) else data
            info = zipfile.ZipInfo(path, date_time=date_time)
            # writestr 传 ZipInfo 时按其 compress_type 压缩,不继承 ZipFile 的 compression。
            info.compress_type = zipfile.ZIP_DEFLATED
            zf.writestr(info, raw)
    return buffer.getvalue()


def _assert_invalid(exc_info: pytest.ExceptionInfo[AgentException], keyword: str) -> None:
    assert exc_info.value.status_code == 400
    assert keyword in exc_info.value.message


def test_parse_skill_md_splits_frontmatter_and_body():
    parsed = parse_skill_md(SKILL_MD)

    assert parsed.frontmatter == {"name": "demo", "description": "演示技能", "version": "1.0.0"}
    assert parsed.body == "# 用法\n正文"


@pytest.mark.parametrize(
    ("text", "keyword"),
    [
        ("# 无 frontmatter", "frontmatter"),
        ("---\nname: demo\n", "frontmatter"),
        ("---\nname: [demo\n---\n", "YAML"),
        ("---\n- a\n- b\n---\n", "映射"),
    ],
)
def test_parse_skill_md_rejects_invalid_frontmatter(text, keyword):
    with pytest.raises(AgentException) as exc_info:
        parse_skill_md(text)
    _assert_invalid(exc_info, keyword)


def test_parse_skill_md_converts_yaml_dates_to_json_safe_strings():
    parsed = parse_skill_md("---\nname: demo\ndescription: d\nreleased: 2024-01-02\n---\n")

    assert parsed.frontmatter["released"] == "2024-01-02"


@pytest.mark.parametrize("name", ["a", "pdf", "pdf-tools", "a1-b2-c3", "x" * 64])
def test_validate_frontmatter_accepts_spec_names(name):
    validate_frontmatter({"name": name, "description": "d"})


@pytest.mark.parametrize(
    "name",
    ["", "PDF", "pdf_tools", "-pdf", "pdf-", "pdf--tools", "pdf tools", "技能", "x" * 65, 123],
)
def test_validate_frontmatter_rejects_invalid_names(name):
    with pytest.raises(AgentException) as exc_info:
        validate_frontmatter({"name": name, "description": "d"})
    _assert_invalid(exc_info, "name")


@pytest.mark.parametrize(
    ("extra", "keyword"),
    [
        ({"description": ""}, "description"),
        ({"description": "x" * 1025}, "description"),
        ({"compatibility": "x" * 501}, "compatibility"),
        ({"compatibility": 1}, "compatibility"),
        ({"metadata": ["a"]}, "metadata"),
        ({"version": "1" * 33}, "version"),
    ],
)
def test_validate_frontmatter_rejects_field_violations(extra, keyword):
    fm = {"name": "demo", "description": "d", **extra}
    with pytest.raises(AgentException) as exc_info:
        validate_frontmatter(fm)
    _assert_invalid(exc_info, keyword)


def test_validate_frontmatter_keeps_unknown_fields():
    fm = {"name": "demo", "description": "d", "license": "MIT", "allowed-tools": "Bash"}

    validate_frontmatter(fm)

    assert SkillBundle.from_content(build_skill_md(fm, "正文")).parsed.frontmatter == fm


def test_parse_skill_zip_uses_shallowest_skill_md_as_root():
    data = _zip(
        {
            "pkg/SKILL.md": SKILL_MD,
            "pkg/scripts/run.py": "print('ok')\n",
            "pkg/nested/SKILL.md": SKILL_MD.replace("demo", "nested"),
            "other/readme.txt": "根外文件",
        }
    )

    bundle = parse_skill_zip(data)

    assert bundle.name == "demo"
    assert bundle.content == SKILL_MD
    assert sorted(bundle.extra_files) == ["nested/SKILL.md", "scripts/run.py"]
    assert bundle.extra_files["scripts/run.py"] == {
        "encoding": ENCODING_UTF8,
        "content": "print('ok')\n",
    }


def test_parse_skill_zip_accepts_root_level_skill_md():
    bundle = parse_skill_zip(_zip({"SKILL.md": SKILL_MD, "references/a.md": "# A"}))

    assert bundle.name == "demo"
    assert list(bundle.extra_files) == ["references/a.md"]


def test_parse_skill_zip_ignores_packaging_noise():
    data = _zip(
        {
            "pkg/SKILL.md": SKILL_MD,
            "__MACOSX/pkg/._SKILL.md": b"\x00\x05",
            "pkg/.DS_Store": b"\x00",
            "pkg/._run.py": b"\x00",
            "pkg/scripts/__pycache__/run.cpython-314.pyc": b"\x00",
            "pkg/.git/HEAD": "ref: refs/heads/main\n",
        }
    )

    assert parse_skill_zip(data).extra_files == {}


@pytest.mark.parametrize("evil", ["pkg/../evil.txt", "../SKILL.md", "pkg\\..\\evil.txt"])
def test_parse_skill_zip_rejects_path_traversal(evil):
    with pytest.raises(AgentException) as exc_info:
        parse_skill_zip(_zip({"pkg/SKILL.md": SKILL_MD, evil: "x"}))
    _assert_invalid(exc_info, "..")


@pytest.mark.parametrize(
    ("raw", "expected"),
    [("a.txt", "a.txt"), ("./a//b.txt", "a/b.txt"), ("scripts\\run.py", "scripts/run.py")],
)
def test_normalize_relative_path_folds_separators(raw, expected):
    assert normalize_relative_path(raw) == expected


@pytest.mark.parametrize("raw", ["", ".", "a\x00b", "/etc/passwd", "C:/x.txt", "c:x", "a/../b"])
def test_normalize_relative_path_rejects_unsafe_paths(raw):
    with pytest.raises(AgentException) as exc_info:
        normalize_relative_path(raw)
    _assert_invalid(exc_info, "路径")


def test_parse_skill_zip_rejects_duplicate_paths_after_normalization():
    data = _zip({"pkg/SKILL.md": SKILL_MD, "pkg/a.txt": "1", "pkg/./a.txt": "2"})

    with pytest.raises(AgentException) as exc_info:
        parse_skill_zip(data)
    _assert_invalid(exc_info, "重复路径")


def test_parse_skill_zip_rejects_file_dir_conflict():
    data = _zip({"pkg/SKILL.md": SKILL_MD, "pkg/a": "1", "pkg/a/b.txt": "2"})

    with pytest.raises(AgentException) as exc_info:
        parse_skill_zip(data)
    _assert_invalid(exc_info, "路径冲突")


def test_parse_skill_zip_stores_binary_and_nul_text_as_base64():
    png = b"\x89PNG\r\n\x1a\n\xff\xfe"
    data = _zip({"pkg/SKILL.md": SKILL_MD, "pkg/assets/logo.png": png, "pkg/n.txt": "a\x00b"})

    bundle = parse_skill_zip(data)

    assert bundle.extra_files["assets/logo.png"]["encoding"] == ENCODING_BASE64
    assert parser.decode_file(bundle.extra_files["assets/logo.png"]) == png
    assert bundle.extra_files["n.txt"]["encoding"] == ENCODING_BASE64


def test_parse_skill_zip_requires_skill_md():
    with pytest.raises(AgentException) as exc_info:
        parse_skill_zip(_zip({"pkg/readme.md": "# x"}))
    _assert_invalid(exc_info, "缺少 SKILL.md")


def test_parse_skill_zip_rejects_non_utf8_skill_md():
    with pytest.raises(AgentException) as exc_info:
        parse_skill_zip(_zip({"SKILL.md": SKILL_MD.encode("gbk")}))
    _assert_invalid(exc_info, "UTF-8")


def test_parse_skill_zip_strips_utf8_bom():
    bundle = parse_skill_zip(_zip({"SKILL.md": b"\xef\xbb\xbf" + SKILL_MD.encode("utf-8")}))

    assert bundle.content == SKILL_MD


def test_parse_skill_zip_enforces_uncompressed_size_by_actual_bytes(monkeypatch):
    monkeypatch.setattr(parser, "MAX_UNCOMPRESSED_BYTES", 1024)
    # 全零内容压缩率极高,模拟 zip 炸弹:压缩包很小,解压后超限。
    data = _zip({"SKILL.md": SKILL_MD, "bomb.bin": b"\x00" * 4096})

    assert len(data) < 1024
    with pytest.raises(AgentException) as exc_info:
        parse_skill_zip(data)
    _assert_invalid(exc_info, "总大小")


def test_parse_skill_zip_enforces_file_count(monkeypatch):
    monkeypatch.setattr(parser, "MAX_BUNDLE_FILES", 3)
    files: dict[str, bytes | str] = {"SKILL.md": SKILL_MD}
    files.update({f"r/{i}.txt": "x" for i in range(3)})

    with pytest.raises(AgentException) as exc_info:
        parse_skill_zip(_zip(files))
    _assert_invalid(exc_info, "文件数")


def test_parse_skill_zip_rejects_non_zip_bytes():
    with pytest.raises(AgentException) as exc_info:
        parse_skill_zip(b"not a zip")
    _assert_invalid(exc_info, "zip")


def test_build_skill_md_round_trips_through_parser():
    fm = {
        "name": "demo",
        "description": "包含中文与: 冒号的描述",
        "version": "2.0",
        "metadata": {"作者": "张三", "tags": ["a", "b"]},
    }

    content = build_skill_md(fm, "\n# 标题\n正文\n\n")
    bundle = SkillBundle.from_content(content)

    assert bundle.parsed.frontmatter == fm
    assert bundle.parsed.body == "# 标题\n正文"
    assert "张三" in content


def test_build_skill_md_output_is_validated_by_bundle():
    content = build_skill_md({"name": "Bad_Name", "description": "d"}, "正文")

    with pytest.raises(AgentException) as exc_info:
        SkillBundle.from_content(content)
    _assert_invalid(exc_info, "name")


def test_skill_bundle_rejects_nul_in_skill_md():
    with pytest.raises(AgentException) as exc_info:
        SkillBundle.from_content(SKILL_MD + "\x00")
    _assert_invalid(exc_info, "NUL")


def test_skill_hash_ignores_zip_timestamps_and_tracks_content():
    files: dict[str, bytes | str] = {"pkg/SKILL.md": SKILL_MD, "pkg/scripts/run.py": "print(1)"}
    first = parse_skill_zip(_zip(files, date_time=(2024, 1, 1, 0, 0, 0)))
    second = parse_skill_zip(_zip(files, date_time=(2025, 6, 1, 12, 0, 0)))
    changed = parse_skill_zip(_zip({**files, "pkg/scripts/run.py": "print(2)"}))

    assert first.skill_hash == second.skill_hash
    assert first.skill_hash != changed.skill_hash
    assert first.skill_hash == compute_skill_hash(first.content, dict(first.extra_files))


def test_build_skill_zip_round_trips_to_same_bundle():
    original = parse_skill_zip(
        _zip({"SKILL.md": SKILL_MD, "assets/a.bin": b"\xff\x00", "references/r.md": "# r"})
    )

    exported = build_skill_zip(original)
    with zipfile.ZipFile(io.BytesIO(exported)) as zf:
        names = sorted(zf.namelist())
    reparsed = parse_skill_zip(exported)

    assert names == ["demo/SKILL.md", "demo/assets/a.bin", "demo/references/r.md"]
    assert reparsed.content == original.content
    assert reparsed.extra_files == original.extra_files
    assert reparsed.skill_hash == original.skill_hash
