"""market 域纯函数的单元测试：分类、快照哈希与大小上限、凭据文件拦截、状态流转检查、展示字段，
以及请求 Schema 的校验。不连接数据库。
"""

import hashlib
import json

import pytest
from pydantic import ValidationError

import database.registry  # noqa: F401  构造 ORM 实例前需全部映射在册
from core.errors import AgentException
from market import service as market_service
from market.models import MarketListingRecord, MarketVersionRecord
from market.schemas import MarketApproveRequest, MarketReasonRequest, MarketSubmitRequest
from market.service import (
    MARKET_CATEGORIES,
    MAX_SNAPSHOT_BYTES,
    check_can_delist,
    check_can_relist,
    check_can_remove,
    check_category,
    check_credential_files,
    check_listing_installable,
    check_snapshot_size,
    check_version_pending,
    compute_snapshot_hash,
    dump_snapshot,
    is_credential_file,
    snapshot_display_fields,
)


def _skill(name: str, extra_paths: tuple[str, ...] = ()) -> dict:
    extra_files = {}
    for path in extra_paths:
        extra_files[path] = {"encoding": "utf-8", "content": "x"}
    content = f"---\nname: {name}\ndescription: {name} 的描述\n---\n\n正文\n"
    return {"name": name, "content": content, "extra_files": extra_files, "skill_hash": "h"}


def _listing(status: str, *, is_official: bool = False) -> MarketListingRecord:
    return MarketListingRecord(kind="skill", status=status, is_official=is_official)


def _version(status: str) -> MarketVersionRecord:
    return MarketVersionRecord(status=status)


# ---------- 分类 ----------


def test_categories_match_design() -> None:
    assert MARKET_CATEGORIES == {
        "general": "通用",
        "coding": "编程开发",
        "data": "数据分析",
        "writing": "写作创作",
        "office": "办公效率",
        "education": "学习教育",
        "other": "其他",
    }


def test_check_category_rejects_unknown_with_400() -> None:
    check_category("data")

    with pytest.raises(AgentException) as exc_info:
        check_category("games")

    assert exc_info.value.status_code == 400
    assert exc_info.value.details["category"] == "games"


# ---------- 快照哈希与大小 ----------


def test_snapshot_hash_uses_canonical_json() -> None:
    snapshot = {"skill": {"name": "数据清洗", "extra_files": {}, "content": "正文"}}
    reordered = {"skill": {"content": "正文", "extra_files": {}, "name": "数据清洗"}}

    canonical = '{"skill":{"content":"正文","extra_files":{},"name":"数据清洗"}}'
    assert dump_snapshot(snapshot) == canonical.encode("utf-8")
    assert compute_snapshot_hash(snapshot) == hashlib.sha256(canonical.encode()).hexdigest()
    # 键顺序不影响哈希，内容变了哈希就变。
    assert compute_snapshot_hash(reordered) == compute_snapshot_hash(snapshot)
    changed = json.loads(json.dumps(snapshot))
    changed["skill"]["content"] = "新正文"
    assert compute_snapshot_hash(changed) != compute_snapshot_hash(snapshot)


def test_snapshot_size_limit_is_32mb() -> None:
    assert MAX_SNAPSHOT_BYTES == 32 * 1024 * 1024
    oversized = {"skill": {"content": "a" * MAX_SNAPSHOT_BYTES}}

    with pytest.raises(AgentException) as exc_info:
        check_snapshot_size(oversized)

    assert exc_info.value.status_code == 400
    assert exc_info.value.details["max_size"] == MAX_SNAPSHOT_BYTES


def test_snapshot_size_boundary(monkeypatch: pytest.MonkeyPatch) -> None:
    snapshot = {"skill": {"content": "正文"}}
    size = len(dump_snapshot(snapshot))

    monkeypatch.setattr(market_service, "MAX_SNAPSHOT_BYTES", size)
    check_snapshot_size(snapshot)

    monkeypatch.setattr(market_service, "MAX_SNAPSHOT_BYTES", size - 1)
    with pytest.raises(AgentException) as exc_info:
        check_snapshot_size(snapshot)
    assert exc_info.value.status_code == 400


# ---------- 凭据文件拦截 ----------


@pytest.mark.parametrize(
    "path",
    [
        ".env",
        "config/.env",
        ".env.local",
        "deploy/.env.production",
        "secrets.json",
        "conf/credentials.json",
        "certs/server.pem",
        "certs/SERVER.PEM",
        "keys/id_rsa",
        "id_ed25519",
    ],
)
def test_credential_file_names_are_detected(path: str) -> None:
    assert is_credential_file(path) is True


@pytest.mark.parametrize(
    "path",
    [
        "README.md",
        "env.example",
        ".envrc",
        "my.env",
        "secrets.yaml",
        "keys/id_rsa.pub",
        "pem/readme.txt",
        "scripts/run.py",
    ],
)
def test_ordinary_file_names_pass(path: str) -> None:
    assert is_credential_file(path) is False


def test_check_credential_files_lists_every_hit_with_skill_name() -> None:
    agent_snapshot = {
        "agent": {"name": "a", "description": None, "system_prompt": "p", "tool_keys": []},
        "skills": [
            _skill("alpha", ("scripts/run.py", "config/.env")),
            _skill("beta", ("keys/id_rsa",)),
        ],
    }

    with pytest.raises(AgentException) as exc_info:
        check_credential_files(agent_snapshot)

    assert exc_info.value.status_code == 400
    assert exc_info.value.details == {"paths": ["alpha/config/.env", "beta/keys/id_rsa"]}
    assert "alpha/config/.env" in exc_info.value.message


def test_check_credential_files_accepts_clean_skill_snapshot() -> None:
    check_credential_files({"skill": _skill("alpha", ("scripts/run.py",))})

    with pytest.raises(AgentException):
        check_credential_files({"skill": _skill("alpha", ("secrets.json",))})


# ---------- 状态流转 ----------


def test_only_pending_version_can_be_withdrawn_or_reviewed() -> None:
    check_version_pending(_version("pending"))
    for status in ("approved", "rejected", "withdrawn"):
        with pytest.raises(AgentException) as exc_info:
            check_version_pending(_version(status))
        assert exc_info.value.status_code == 409


def test_delist_only_from_listed_and_never_official() -> None:
    check_can_delist(_listing("listed"))
    for status in ("unlisted", "delisted", "removed"):
        with pytest.raises(AgentException) as exc_info:
            check_can_delist(_listing(status))
        assert exc_info.value.status_code == 409

    with pytest.raises(AgentException) as official:
        check_can_delist(_listing("listed", is_official=True))
    assert official.value.status_code == 409


def test_relist_only_from_delisted_and_removed_needs_new_version() -> None:
    check_can_relist(_listing("delisted"))
    for status in ("unlisted", "listed"):
        with pytest.raises(AgentException) as exc_info:
            check_can_relist(_listing(status))
        assert exc_info.value.status_code == 409

    with pytest.raises(AgentException) as removed:
        check_can_relist(_listing("removed"))
    assert removed.value.status_code == 409
    assert "提交新版本" in removed.value.message

    with pytest.raises(AgentException) as official:
        check_can_relist(_listing("delisted", is_official=True))
    assert official.value.status_code == 409


def test_remove_only_listed_or_delisted_community_listing() -> None:
    check_can_remove(_listing("listed"))
    check_can_remove(_listing("delisted"))
    for status in ("unlisted", "removed"):
        with pytest.raises(AgentException) as exc_info:
            check_can_remove(_listing(status))
        assert exc_info.value.status_code == 409

    with pytest.raises(AgentException) as official:
        check_can_remove(_listing("listed", is_official=True))
    assert official.value.status_code == 409
    assert official.value.message == "官方内容请从代码中移除"


def test_only_listed_listing_is_installable() -> None:
    check_listing_installable(_listing("listed"))
    for status in ("unlisted", "delisted", "removed"):
        with pytest.raises(AgentException) as exc_info:
            check_listing_installable(_listing(status))
        assert exc_info.value.status_code == 409


# ---------- 展示字段 ----------


def test_display_fields_from_agent_and_skill_snapshots() -> None:
    agent_snapshot = {
        "agent": {"name": "分析师", "description": "做分析", "system_prompt": "p", "tool_keys": []},
        "skills": [],
    }

    assert snapshot_display_fields("agent", agent_snapshot) == ("分析师", "做分析")
    assert snapshot_display_fields("skill", {"skill": _skill("alpha")}) == (
        "alpha",
        "alpha 的描述",
    )


# ---------- 请求 Schema ----------


def test_reason_is_required_and_blank_is_rejected() -> None:
    assert MarketReasonRequest(reason="  含违规脚本 ").reason == "含违规脚本"
    with pytest.raises(ValidationError):
        MarketReasonRequest(reason="   ")
    with pytest.raises(ValidationError):
        MarketReasonRequest.model_validate({})


def test_submit_request_normalizes_and_validates() -> None:
    request = MarketSubmitRequest(kind="agent", source_id=1, category="data", release_note="  ")
    assert request.release_note is None
    assert MarketApproveRequest(comment=" ").comment is None

    with pytest.raises(ValidationError):
        MarketSubmitRequest(kind="workflow", source_id=1, category="data")
    with pytest.raises(ValidationError):
        MarketSubmitRequest(kind="agent", source_id=0, category="data")
