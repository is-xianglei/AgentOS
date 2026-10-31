"""SkillService 业务规则单元测试。

用替身替换 Repository、WorkspaceService 与执行器,只验证本域编排逻辑;
save 的启用与复活规则、JSONB 往返、软删过滤与 HTTP 路由由 tests/test_skill_pg.py
在真实 PostgreSQL 上验证。
"""

import io
import os
import zipfile
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest
from sqlalchemy.exc import IntegrityError

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
    workspace_id: int = 1,
    is_deleted: bool = False,
) -> SkillRecord:
    bundle = SkillBundle.from_content(content or _skill_md(), extra_files or {})
    return SkillRecord(
        id=record_id,
        workspace_id=workspace_id,
        name=bundle.name,
        description=bundle.description,
        frontmatter=bundle.parsed.frontmatter,
        version=bundle.version,
        skill_hash=bundle.skill_hash,
        scope="workspace",
        content=bundle.content,
        extra_files=bundle.extra_files,
        is_enabled=is_enabled,
        is_deleted=is_deleted,
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
    """按 ID 存放记录;查询与真实 Repository 一样带工作区条件,并默认过滤软删行。"""

    def __init__(self, *records: SkillRecord) -> None:
        self.records = {record.id: record for record in records}
        self.saves: list[tuple[SkillRecord | None, dict]] = []
        self.save_error: Exception | None = None
        self.enabled_calls: list[tuple[int, bool]] = []
        self.soft_deleted: list[int] = []

    def _active(self, workspace_id):
        return [
            r for r in self.records.values()
            if r.workspace_id == workspace_id and not r.is_deleted
        ]

    async def get_by_id(self, workspace_id, skill_id):
        return next((r for r in self._active(workspace_id) if r.id == skill_id), None)

    async def get_by_name(self, workspace_id, name):
        return next((r for r in self._active(workspace_id) if r.name == name), None)

    async def lock_by_name(self, workspace_id, name):
        return next(
            (
                r for r in self.records.values()
                if r.workspace_id == workspace_id and r.name == name
            ),
            None,
        )

    async def list_enabled(self, workspace_id, skill_ids=None):
        return [
            r
            for r in self._active(workspace_id)
            if r.is_enabled and (skill_ids is None or r.id in skill_ids)
        ]

    async def list_all(self, workspace_id):
        return self._active(workspace_id)

    async def list_by_ids(self, workspace_id, skill_ids):
        return [r for r in self._active(workspace_id) if r.id in skill_ids]

    async def save(self, record, **fields):
        if self.save_error is not None:
            raise self.save_error
        self.saves.append((record, fields))
        return SimpleNamespace(id=record.id if record is not None else 100, **fields)

    async def set_enabled(self, record, enabled):
        self.enabled_calls.append((record.id, enabled))
        record.is_enabled = enabled
        return record

    async def soft_delete(self, record):
        self.soft_deleted.append(record.id)


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


_NO_OPTIONAL = {"version": None, "compatibility": None, "metadata": None}


def _create_via(entry: str, service: SkillService, workspace_id: int = 1):
    """两条新建入口共用的调用方式,产出同名 demo。"""
    if entry == "zip":
        return service.create_from_bundle(workspace_id, 9, _zip({"SKILL.md": _skill_md()}))
    return service.create_from_fields(
        workspace_id, 9, name="demo", description="演示技能", body="正文", **_NO_OPTIONAL,
    )


def _update(service: SkillService, skill_id: int = 1, **overrides):
    fields = {"name": None, "description": "新描述", "body": "新正文", **_NO_OPTIONAL}
    fields.update(overrides)
    return service.update_from_fields(1, skill_id, 9, **fields)


async def test_create_from_bundle_creates_new_skill_with_extra_files():
    repo = _FakeRepo()
    data = _zip({"demo/SKILL.md": _skill_md(), "demo/scripts/run.py": "print(1)"})

    await _service(repo).create_from_bundle(1, 9, data)

    ((record, fields),) = repo.saves
    assert record is None
    assert fields["name"] == "demo"
    assert fields["actor_user_id"] == 9
    assert fields["extra_files"] == {"scripts/run.py": encode_file(b"print(1)")}
    assert fields["skill_hash"] == parse_skill_zip(data).skill_hash
    assert _FakeWorkspaceService.calls == [(1, 9, {"owner", "admin"})]


async def test_create_from_bundle_requires_write_role():
    _FakeWorkspaceService.denied = True
    repo = _FakeRepo()

    with pytest.raises(AgentException) as exc_info:
        await _service(repo).create_from_bundle(1, 9, _zip({"SKILL.md": _skill_md()}))

    assert exc_info.value.status_code == 403
    assert repo.saves == []


async def test_create_from_fields_creates_new_skill_without_extra_files():
    repo = _FakeRepo()

    await _service(repo).create_from_fields(
        1, 9, name="new-skill", description="d", body="正文", version=None,
        compatibility="需要 Python 3", metadata={"作者": "张三"},
    )

    ((record, fields),) = repo.saves
    assert record is None
    assert fields["name"] == "new-skill"
    assert fields["extra_files"] == {}
    assert fields["frontmatter"] == {
        "name": "new-skill",
        "description": "d",
        "compatibility": "需要 Python 3",
        "metadata": {"作者": "张三"},
    }


async def test_create_from_fields_rejects_invalid_name():
    repo = _FakeRepo()

    with pytest.raises(AgentException) as exc_info:
        await _service(repo).create_from_fields(
            1, 9, name="Bad_Name", description="d", body="正文", **_NO_OPTIONAL,
        )

    assert exc_info.value.status_code == 400
    assert repo.saves == []


@pytest.mark.parametrize("entry", ["zip", "json"])
async def test_create_rejects_active_same_name_with_409(entry):
    repo = _FakeRepo(_record(7))

    with pytest.raises(AgentException) as exc_info:
        await _create_via(entry, _service(repo))

    assert exc_info.value.status_code == 409
    assert exc_info.value.message == "skill 名称已存在: demo"
    assert exc_info.value.details == {"name": "demo"}
    assert repo.saves == []


@pytest.mark.parametrize("entry", ["zip", "json"])
async def test_create_revives_soft_deleted_same_name_keeping_id(entry):
    # 内容与软删行完全一致时也必须走复活写入,不能被哈希判重短路成返回已删除记录。
    deleted = _record(7, is_deleted=True)
    repo = _FakeRepo(deleted)

    result = await _create_via(entry, _service(repo))

    ((record, fields),) = repo.saves
    assert record is deleted
    assert result.id == 7
    assert fields["name"] == "demo"


@pytest.mark.parametrize("entry", ["zip", "json"])
async def test_create_maps_unique_violation_to_409(entry):
    repo = _FakeRepo()
    repo.save_error = IntegrityError("INSERT INTO skills", {}, Exception("uq_skills_workspace_name"))

    with pytest.raises(AgentException) as exc_info:
        await _create_via(entry, _service(repo))

    assert exc_info.value.status_code == 409
    assert exc_info.value.message == "skill 名称已存在: demo"
    assert isinstance(exc_info.value.__cause__, IntegrityError)


async def test_create_ignores_same_name_in_other_workspace():
    repo = _FakeRepo(_record(7, workspace_id=2))

    await _create_via("json", _service(repo))

    ((record, _),) = repo.saves
    assert record is None


async def test_update_from_fields_keeps_extra_files_and_unmanaged_frontmatter():
    extra = {"scripts/run.py": encode_file(b"print(1)")}
    existing = _record(
        content=_skill_md(extra="license: MIT\nallowed-tools: Bash Read\ncompatibility: old\n"),
        extra_files=extra,
    )
    repo = _FakeRepo(existing)

    await _update(_service(repo), version="2.0")

    ((record, fields),) = repo.saves
    assert record is existing
    assert fields["extra_files"] == extra
    assert fields["frontmatter"] == {
        "name": "demo",
        "description": "新描述",
        "license": "MIT",
        "allowed-tools": "Bash Read",
        "compatibility": "old",
        "version": "2.0",
    }
    assert fields["content"].endswith("---\n\n新正文\n")
    assert fields["version"] == "2.0"


async def test_update_from_fields_keeps_optional_fields_when_omitted():
    existing = _record(
        content=_skill_md(
            extra="version: 1.2.0\ncompatibility: 需要 Python 3\nmetadata:\n  作者: 张三\n"
        )
    )
    repo = _FakeRepo(existing)
    service = _service(repo)

    await _update(service)
    await _update(service, metadata={"作者": "李四"})

    (_, kept), (_, replaced) = repo.saves
    assert kept["version"] == "1.2.0"
    assert {key: kept["frontmatter"][key] for key in ("version", "compatibility", "metadata")} == {
        "version": "1.2.0",
        "compatibility": "需要 Python 3",
        "metadata": {"作者": "张三"},
    }
    assert replaced["frontmatter"]["metadata"] == {"作者": "李四"}
    assert replaced["frontmatter"]["version"] == "1.2.0"


async def test_update_from_fields_accepts_same_name_and_rejects_rename():
    repo = _FakeRepo(_record())
    service = _service(repo)

    await _update(service, name="demo")
    with pytest.raises(AgentException) as exc_info:
        await _update(service, name="renamed")

    assert len(repo.saves) == 1
    assert exc_info.value.status_code == 400
    assert "demo" in exc_info.value.message and "renamed" in exc_info.value.message


async def test_update_from_fields_restores_legacy_record_with_empty_content():
    # 改 PG 存储前的旧记录 content 为空串,JSON 更新不解析旧正文,可借此恢复内容。
    legacy = _record()
    legacy.content = ""
    legacy.skill_hash = None
    repo = _FakeRepo(legacy)

    await _update(_service(repo))

    ((record, fields),) = repo.saves
    assert record is legacy
    assert fields["content"].startswith("---\nname: demo\n")


async def test_update_from_fields_is_idempotent_for_same_content():
    repo = _FakeRepo(_record())
    service = _service(repo)
    await _update(service)
    _, fields = repo.saves.pop()
    repo.records[1] = _record(content=fields["content"])

    result = await _update(service)

    assert result is repo.records[1]
    assert repo.saves == []


async def test_replace_bundle_skips_save_when_hash_unchanged():
    existing = _record(3)
    repo = _FakeRepo(existing)

    result = await _service(repo).replace_bundle(1, 3, 9, _zip({"SKILL.md": _skill_md()}))

    assert result is existing
    assert repo.saves == []
    assert _FakeWorkspaceService.calls == [(1, 9, {"owner", "admin"})]


async def test_replace_bundle_overwrites_target_record():
    existing = _record(3)
    repo = _FakeRepo(existing)
    data = _zip(
        {"demo/SKILL.md": _skill_md(description="新描述"), "demo/scripts/run.py": "print(1)"}
    )

    await _service(repo).replace_bundle(1, 3, 9, data)

    ((record, fields),) = repo.saves
    assert record is existing
    assert fields["description"] == "新描述"
    assert fields["extra_files"] == {"scripts/run.py": encode_file(b"print(1)")}


async def test_replace_bundle_rejects_name_mismatch():
    repo = _FakeRepo(_record(3))

    with pytest.raises(AgentException) as exc_info:
        await _service(repo).replace_bundle(1, 3, 9, _zip({"SKILL.md": _skill_md(name="other")}))

    assert exc_info.value.status_code == 400
    assert exc_info.value.details == {"name": "demo", "bundle_name": "other"}
    assert repo.saves == []


async def test_id_entries_hide_other_workspace_skill_as_404():
    repo = _FakeRepo(_record(7, workspace_id=2))
    service = _service(repo)
    calls = [
        service.get_detail(1, 7),
        service.export_bundle(1, 7),
        service.set_enabled(1, 7, 9, False),
        _update(service, 7),
        service.replace_bundle(1, 7, 9, _zip({"SKILL.md": _skill_md()})),
    ]

    for call in calls:
        with pytest.raises(AgentException) as exc_info:
            await call
        assert exc_info.value.status_code == 404
        assert exc_info.value.message == "skill 不存在: 7"
    assert await service.delete_skill(1, 7, 9) is False
    assert (repo.saves, repo.enabled_calls, repo.soft_deleted) == ([], [], [])


async def test_set_enabled_toggles_and_skips_noop():
    repo = _FakeRepo(_record())
    service = _service(repo)

    disabled = await service.set_enabled(1, 1, 9, False)
    await service.set_enabled(1, 1, 9, False)

    assert disabled.is_enabled is False
    assert repo.enabled_calls == [(1, False)]


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

    detail = await service.get_detail(1, 1)
    archive = await service.export_bundle(1, 1)
    exported = parse_skill_zip(archive.data)

    assert archive.name == "demo"
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

    assert await service.delete_skill(1, 5, 9) is True
    assert await service.delete_skill(1, 99, 9) is False
    assert repo.soft_deleted == [5]
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
