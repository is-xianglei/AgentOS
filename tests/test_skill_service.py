"""SkillService 业务规则单元测试。

用替身替换 Repository、WorkspaceService 与执行器,只验证本域编排逻辑;
upsert 的启用规则、JSONB 往返与软删过滤由 tests/test_skill_pg.py 在真实 PostgreSQL 上验证。
"""

import io
import os
import zipfile
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest

import database.registry  # noqa: F401  构造 ORM 实例前需全部映射在册
from core.errors import AgentException
from core.executor import ExecResult, Executor
from skill import service as skill_service_module
from skill.models import SkillRecord
from skill.parser import SkillBundle, encode_file, parse_skill_zip
from skill.service import SkillService
from tools.base import ToolContext
from tools.builtin.skill import SkillResourceTool, SkillTool

pytestmark = pytest.mark.anyio


def _skill_md(name: str = "demo", description: str = "演示技能", extra: str = "") -> str:
    return f"---\nname: {name}\ndescription: {description}\n{extra}---\n\n# 用法\n正文\n"


def _record(
    record_id: int = 1,
    *,
    content: str | None = None,
    extra_files: dict[str, dict[str, str]] | None = None,
    is_enabled: bool = True,
) -> SkillRecord:
    bundle = SkillBundle.from_content(content or _skill_md(), extra_files or {})
    return SkillRecord(
        id=record_id,
        workspace_id=1,
        name=bundle.name,
        description=bundle.description,
        frontmatter=bundle.parsed.frontmatter,
        version=bundle.version,
        skill_hash=bundle.skill_hash,
        scope="workspace",
        content=bundle.content,
        extra_files=bundle.extra_files,
        is_enabled=is_enabled,
        created_at=datetime(2026, 1, 1, tzinfo=UTC),
        updated_at=datetime(2026, 1, 1, tzinfo=UTC),
    )


class _FakeWorkspaceService:
    calls: list[tuple[int, int, set[str]]] = []
    denied = False

    def __init__(self, db) -> None:
        pass

    async def require_workspace_role(self, workspace_id, user_id, roles):
        type(self).calls.append((workspace_id, user_id, set(roles)))
        if type(self).denied:
            raise AgentException.message("无权执行该工作区操作", status_code=403)


class _FakeRepo:
    def __init__(self, *records: SkillRecord) -> None:
        self.records = {record.name: record for record in records}
        self.upserts: list[dict] = []
        self.enabled_calls: list[tuple[str, bool]] = []
        self.soft_deleted: list[str] = []

    async def get_by_name(self, workspace_id, name, *, include_deleted=False):
        return self.records.get(name)

    async def list_enabled(self, workspace_id, skill_ids=None):
        return [
            r
            for r in self.records.values()
            if r.is_enabled and (skill_ids is None or r.id in skill_ids)
        ]

    async def list_all(self, workspace_id):
        return list(self.records.values())

    async def list_by_ids(self, workspace_id, skill_ids):
        return [r for r in self.records.values() if r.id in skill_ids]

    async def upsert(self, **fields):
        self.upserts.append(fields)
        return SimpleNamespace(**fields)

    async def set_enabled(self, record, enabled):
        self.enabled_calls.append((record.name, enabled))
        record.is_enabled = enabled
        return record

    async def soft_delete(self, record):
        self.soft_deleted.append(record.name)


class _FakeExecutor(Executor):
    def __init__(self) -> None:
        self.calls: list[dict] = []
        self.snapshot: dict[str, bytes] = {}

    async def run(self, *, workdir, argv, stdin, timeout, env) -> ExecResult:
        self.calls.append({"workdir": workdir, "argv": argv, "stdin": stdin, "env": env})
        for base, _, names in os.walk(workdir):
            for file_name in names:
                path = os.path.join(base, file_name)
                with open(path, "rb") as fp:
                    self.snapshot[os.path.relpath(path, workdir)] = fp.read()
        return ExecResult(exit_code=0, stdout="ok", stderr="", timed_out=False, truncated=False)


@pytest.fixture(autouse=True)
def _fake_workspace(monkeypatch):
    _FakeWorkspaceService.calls = []
    _FakeWorkspaceService.denied = False
    monkeypatch.setattr(skill_service_module, "WorkspaceService", _FakeWorkspaceService)


def _service(repo: _FakeRepo, executor: Executor | None = None) -> SkillService:
    service = SkillService(db=None, executor=executor)
    service.repo = repo
    return service


def _zip(files: dict[str, str | bytes]) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as zf:
        for path, data in files.items():
            zf.writestr(path, data)
    return buffer.getvalue()


async def test_upload_bundle_skips_upsert_when_hash_unchanged():
    existing = _record()
    repo = _FakeRepo(existing)

    result = await _service(repo).upload_bundle(_zip({"SKILL.md": _skill_md()}), 1, 9)

    assert result is existing
    assert repo.upserts == []
    assert _FakeWorkspaceService.calls == [(1, 9, {"owner", "admin"})]


async def test_upload_bundle_upserts_changed_bundle_with_extra_files():
    repo = _FakeRepo(_record())
    data = _zip({"demo/SKILL.md": _skill_md(), "demo/scripts/run.py": "print(1)"})

    await _service(repo).upload_bundle(data, 1, 9)

    (fields,) = repo.upserts
    assert fields["name"] == "demo"
    assert fields["actor_user_id"] == 9
    assert fields["extra_files"] == {"scripts/run.py": encode_file(b"print(1)")}
    assert fields["skill_hash"] == parse_skill_zip(data).skill_hash


async def test_upload_bundle_requires_write_role():
    _FakeWorkspaceService.denied = True
    repo = _FakeRepo()

    with pytest.raises(AgentException) as exc_info:
        await _service(repo).upload_bundle(_zip({"SKILL.md": _skill_md()}), 1, 9)

    assert exc_info.value.status_code == 403
    assert repo.upserts == []


async def test_upsert_from_fields_keeps_extra_files_and_unmanaged_frontmatter():
    extra = {"scripts/run.py": encode_file(b"print(1)")}
    existing = _record(
        content=_skill_md(extra="license: MIT\nallowed-tools: Bash Read\ncompatibility: old\n"),
        extra_files=extra,
    )
    repo = _FakeRepo(existing)

    await _service(repo).upsert_from_fields(
        1, 9, "demo", description="新描述", body="新正文", version="2.0",
        compatibility=None, metadata=None,
    )

    (fields,) = repo.upserts
    assert fields["extra_files"] == extra
    assert fields["frontmatter"] == {
        "license": "MIT",
        "allowed-tools": "Bash Read",
        "name": "demo",
        "description": "新描述",
        "version": "2.0",
    }
    assert fields["content"].endswith("---\n\n新正文\n")
    assert fields["version"] == "2.0"


async def test_upsert_from_fields_creates_new_skill_without_extra_files():
    repo = _FakeRepo()

    await _service(repo).upsert_from_fields(
        1, 9, "new-skill", description="d", body="正文", version=None,
        compatibility="需要 Python 3", metadata={"作者": "张三"},
    )

    (fields,) = repo.upserts
    assert fields["name"] == "new-skill"
    assert fields["extra_files"] == {}
    assert fields["frontmatter"]["metadata"] == {"作者": "张三"}
    assert fields["frontmatter"]["compatibility"] == "需要 Python 3"


async def test_upsert_from_fields_rejects_invalid_name_from_path():
    repo = _FakeRepo()

    with pytest.raises(AgentException) as exc_info:
        await _service(repo).upsert_from_fields(
            1, 9, "Bad_Name", description="d", body="正文", version=None,
            compatibility=None, metadata=None,
        )

    assert exc_info.value.status_code == 400
    assert repo.upserts == []


async def test_upsert_from_fields_is_idempotent_for_same_content():
    repo = _FakeRepo()
    service = _service(repo)
    kwargs = dict(description="d", body="正文", version=None, compatibility=None, metadata=None)
    await service.upsert_from_fields(1, 9, "demo", **kwargs)
    fields = repo.upserts.pop()
    repo.records["demo"] = _record(content=fields["content"])

    result = await service.upsert_from_fields(1, 9, "demo", **kwargs)

    assert result is repo.records["demo"]
    assert repo.upserts == []


async def test_set_enabled_toggles_and_skips_noop():
    repo = _FakeRepo(_record())
    service = _service(repo)

    disabled = await service.set_enabled(1, "demo", 9, False)
    await service.set_enabled(1, "demo", 9, False)

    assert disabled.is_enabled is False
    assert repo.enabled_calls == [("demo", False)]


async def test_catalog_lists_only_enabled_skills():
    repo = _FakeRepo(
        _record(1),
        _record(2, content=_skill_md("legacy", "已禁用"), is_enabled=False),
    )

    catalog = await _service(repo).get_catalog(1)

    assert catalog == "- demo: 演示技能"


async def test_runtime_access_rejects_disabled_skill():
    repo = _FakeRepo(_record(is_enabled=False))
    service = _service(repo, _FakeExecutor())
    calls = [
        service.load_body(1, "demo"),
        service.list_resources(1, "demo"),
        service.read_resource(1, "demo", "SKILL.md"),
        service.run_script(1, "demo", "scripts/run.py"),
    ]

    for call in calls:
        with pytest.raises(AgentException) as exc_info:
            await call
        assert exc_info.value.status_code == 404
        assert "不存在或已禁用" in exc_info.value.message


async def test_detail_and_export_remain_available_when_disabled():
    extra = {"assets/a.bin": encode_file(b"\xff\x00")}
    record = _record(extra_files=extra, is_enabled=False)
    service = _service(_FakeRepo(record))

    detail = await service.get_detail(1, "demo")
    exported = parse_skill_zip(await service.export_bundle(1, "demo"))

    assert detail.is_enabled is False
    assert detail.body == "# 用法\n正文"
    assert [(r.relative_path, r.size, r.encoding) for r in detail.resources] == [
        ("assets/a.bin", 2, "base64")
    ]
    assert exported.skill_hash == record.skill_hash


async def test_read_resource_inlines_text_and_describes_binary():
    extra = {
        "references/r.md": encode_file("# 参考".encode("utf-8")),
        "assets/a.bin": encode_file(b"\xff\x00\x01"),
    }
    service = _service(_FakeRepo(_record(extra_files=extra)))

    text = await service.read_resource(1, "demo", "references\\r.md")
    binary = await service.read_resource(1, "demo", "assets/a.bin")
    skill_md = await service.read_resource(1, "demo", "SKILL.md")

    assert (text.relative_path, text.text) == ("references/r.md", "# 参考")
    assert (binary.text, binary.size) == (None, 3)
    assert skill_md.text == _skill_md()
    with pytest.raises(AgentException) as exc_info:
        await service.read_resource(1, "demo", "missing.md")
    assert exc_info.value.status_code == 404
    with pytest.raises(AgentException):
        await service.read_resource(1, "demo", "../etc/passwd")


async def test_run_script_materializes_bundle_and_cleans_up(monkeypatch):
    monkeypatch.setattr(skill_service_module.settings, "skill_exec_enabled", True)
    monkeypatch.setenv("AGENTOS_FAKE_SECRET", "should-not-leak")
    extra = {
        "scripts/run.py": encode_file(b"print(1)"),
        "assets/a.bin": encode_file(b"\xff\x00"),
    }
    executor = _FakeExecutor()
    service = _service(_FakeRepo(_record(extra_files=extra)), executor)

    result = await service.run_script(1, "demo", "scripts/run.py", ["--x"], "输入")

    (call,) = executor.calls
    assert result.exit_code == 0
    assert executor.snapshot == {
        "SKILL.md": _skill_md().encode("utf-8"),
        "scripts/run.py": b"print(1)",
        "assets/a.bin": b"\xff\x00",
    }
    assert os.path.basename(call["workdir"]) == "demo"
    assert call["argv"] == ["python", os.path.join(call["workdir"], "scripts/run.py"), "--x"]
    assert call["stdin"] == "输入".encode("utf-8")
    assert set(call["env"]) == {"PATH", "LANG"}
    assert not os.path.exists(os.path.dirname(call["workdir"]))


@pytest.mark.parametrize(
    ("path", "keyword"),
    [("run.py", "scripts/"), ("scripts/run.rb", "白名单"), ("scripts/missing.py", "脚本不存在")],
)
async def test_run_script_rejects_invalid_targets(monkeypatch, path, keyword):
    monkeypatch.setattr(skill_service_module.settings, "skill_exec_enabled", True)
    executor = _FakeExecutor()
    service = _service(_FakeRepo(_record()), executor)

    with pytest.raises(AgentException) as exc_info:
        await service.run_script(1, "demo", path)

    assert keyword in exc_info.value.message
    assert executor.calls == []


async def test_run_script_respects_global_switch(monkeypatch):
    monkeypatch.setattr(skill_service_module.settings, "skill_exec_enabled", False)
    executor = _FakeExecutor()

    with pytest.raises(AgentException) as exc_info:
        await _service(_FakeRepo(_record()), executor).run_script(1, "demo", "scripts/a.py")

    assert "已被禁用" in exc_info.value.message
    assert executor.calls == []


async def test_enabled_names_filter_disabled_but_binding_allows_them():
    repo = _FakeRepo(
        _record(1),
        _record(2, content=_skill_md("legacy", "已禁用"), is_enabled=False),
        _record(3, content=_skill_md("third", "第三个")),
    )
    service = _service(repo)

    names = await service.get_enabled_names_by_ids(1, [3, 2, 1, 3])
    bindable = await service.require_bindable_skill_ids(1, [2, 2, 1])

    assert names == ("third", "demo")
    assert bindable == [2, 1]
    with pytest.raises(AgentException) as exc_info:
        await service.get_enabled_names_by_ids(1, [1, 99])
    assert exc_info.value.status_code == 404
    assert exc_info.value.details == {"skill_ids": [99]}


async def test_delete_skill_soft_deletes_and_clears_bindings(monkeypatch):
    import agent.service

    removed: list[tuple[int, int]] = []

    class _FakeAgentService:
        def __init__(self, db) -> None:
            pass

        async def remove_skill_bindings(self, workspace_id, skill_id):
            removed.append((workspace_id, skill_id))

    monkeypatch.setattr(agent.service, "AgentService", _FakeAgentService)
    repo = _FakeRepo(_record(5))
    service = _service(repo)

    assert await service.delete_skill(1, "demo", 9) is True
    assert await service.delete_skill(1, "missing", 9) is False
    assert repo.soft_deleted == ["demo"]
    assert removed == [(1, 5)]


def _tool_ctx() -> ToolContext:
    return ToolContext(session_id=1, db=None, workspace_id=1)


async def test_skill_tools_report_disabled_skill(monkeypatch):
    service = _service(_FakeRepo(_record(is_enabled=False)))
    monkeypatch.setattr("tools.builtin.skill.SkillService", lambda db: service)

    text = await SkillTool().run(SkillTool.input_model(name="demo"), _tool_ctx())

    assert text == "错误:skill 不存在或已禁用: demo"


async def test_skill_tools_render_body_and_resources(monkeypatch):
    extra = {
        "references/r.md": encode_file("# 参考".encode("utf-8")),
        "assets/a.bin": encode_file(b"\xff\x00"),
    }
    service = _service(_FakeRepo(_record(extra_files=extra)))
    monkeypatch.setattr("tools.builtin.skill.SkillService", lambda db: service)
    resource_input = SkillResourceTool.input_model

    loaded = await SkillTool().run(SkillTool.input_model(name="demo"), _tool_ctx())
    text = await SkillResourceTool().run(
        resource_input(skill_name="demo", relative_path="references/r.md"), _tool_ctx()
    )
    binary = await SkillResourceTool().run(
        resource_input(skill_name="demo", relative_path="assets/a.bin"), _tool_ctx()
    )

    assert "# 用法\n正文" in loaded
    assert "- assets/a.bin (application/octet-stream, 2B)" in loaded
    assert "SKILL.md" not in loaded.split("<skill_resources", 1)[1]
    assert text == "# 参考"
    assert binary == "[二进制资源 assets/a.bin,application/octet-stream,2 字节,暂不支持内联展示]"
