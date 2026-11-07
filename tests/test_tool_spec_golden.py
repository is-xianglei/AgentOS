"""工具描述与工具策略黄金测试：固化发给模型的内置工具描述，以及每个内置工具的能力策略。

字面量在 tests/fixtures/tool_spec_golden.py，由阶段 3a 重构前的代码实际产出生成；
此后只允许改动本文件「驱动」一节（如何取得内置工具集、如何读取策略），字面量一个都不许改。
工具描述连同字典键序一起比对，并用 SHA-256 再校验一遍序列化字节。
"""

import hashlib
import json
from dataclasses import fields
from types import SimpleNamespace
from typing import Any, get_args

import pytest

from harness.adapters.tools.builtin_provider import BuiltinToolProvider
from harness.app.tool_set import ToolSet
from harness.domain.tool import (
    ToolPolicy,
    ToolProfile,
    requires_skill_allowlist,
    tool_policy,
)
from tests.fixtures.model_request_golden import EXPECTED as MODEL_REQUEST_EXPECTED
from tests.fixtures.tool_spec_golden import (
    EXPECTED_DEFAULT_POLICY,
    EXPECTED_POLICIES,
    EXPECTED_POLICY_FIELDS,
    EXPECTED_PROFILES,
    EXPECTED_SKILL_ALLOWLIST_PROFILES,
    EXPECTED_SPECS,
    EXPECTED_SPECS_SHA256,
    EXPECTED_UNKNOWN_POLICY,
)
from tools.service import ToolCatalogService

pytestmark = pytest.mark.anyio


# ---------- 驱动：如何取得内置工具集与策略（重构时只改这一节） ----------


def _builtin_specs() -> list[Any]:
    """内置工具集发给模型的工具描述，按工具集顺序。"""
    return ToolSet.from_providers(BuiltinToolProvider()).to_specs()


def _builtin_policies() -> dict[str, ToolPolicy]:
    """每个内置工具自带的能力策略，键为工具名。"""
    return {tool.name: tool.policy for tool in BuiltinToolProvider().list_tools()}


def _policy_by_name(name: str) -> ToolPolicy:
    """只有工具名时按名向工具集查询策略。"""
    return ToolSet.from_providers(BuiltinToolProvider()).policy_for(name)


def _default_policy() -> ToolPolicy:
    """只给名称与类别、其余取默认值时得到的策略。"""
    return tool_policy("NewTool", "leaf")


def _policy_type() -> type:
    return ToolPolicy


def _profiles() -> tuple[str, ...]:
    return get_args(ToolProfile)


def _requires_skill_allowlist(profile: str) -> bool:
    return requires_skill_allowlist(profile)


# ---------- 断言（字面量与断言都不许改） ----------


def _spec_dict(spec: Any) -> dict[str, Any]:
    return {
        "name": spec.name,
        "description": spec.description,
        "input_schema": spec.input_schema,
    }


def _policy_dict(policy: Any) -> dict[str, Any]:
    return {
        "name": policy.name,
        "category": policy.category,
        "bindable_profiles": sorted(policy.bindable_profiles),
        "callable_profiles": sorted(policy.callable_profiles),
        "requires_approval": policy.requires_approval,
        "requires_bound_skill": policy.requires_bound_skill,
    }


def _ordered(value: Any) -> Any:
    """连同字典键序一起比较：键序决定 SDK 序列化出的请求体字节。"""
    if isinstance(value, dict):
        return ("dict", tuple((key, _ordered(item)) for key, item in value.items()))
    if isinstance(value, list):
        return ("list", tuple(_ordered(item) for item in value))
    return value


def _sha256(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, ensure_ascii=False).encode("utf-8")).hexdigest()


def test_golden_spec_literal_matches_its_digest() -> None:
    """字面量自身与摘要一致，防止字面量被悄悄改动。"""
    assert _sha256(EXPECTED_SPECS) == EXPECTED_SPECS_SHA256


def test_builtin_tool_specs_match_golden() -> None:
    """工具名、描述、JSON Schema 与工具顺序逐字节不变（含字典键序）。"""
    actual = [_spec_dict(spec) for spec in _builtin_specs()]

    assert [item["name"] for item in actual] == [item["name"] for item in EXPECTED_SPECS]
    assert actual == EXPECTED_SPECS
    assert _ordered(actual) == _ordered(EXPECTED_SPECS), "取值相同但字典键序不同"
    assert _sha256(actual) == EXPECTED_SPECS_SHA256


def test_echo_spec_cross_checks_model_request_golden() -> None:
    """echo 工具的描述与模型请求黄金矩阵中发往 SDK 的工具参数一致。"""
    calls = MODEL_REQUEST_EXPECTED["client/db_anthropic_budget/fast/stream"]["calls"]
    sent_tools = calls[1]["kwargs"]["tools"]
    echo = next(item for item in EXPECTED_SPECS if item["name"] == "echo")

    assert sent_tools == [echo]
    assert _ordered(sent_tools) == _ordered([echo])


def test_policy_fields_match_golden() -> None:
    assert [field.name for field in fields(_policy_type())] == EXPECTED_POLICY_FIELDS


def test_builtin_tool_policies_match_golden() -> None:
    """每个内置工具都带策略，6 个字段取值与重构前的策略表逐项一致。"""
    actual = {name: _policy_dict(policy) for name, policy in _builtin_policies().items()}

    assert set(actual) == {item["name"] for item in EXPECTED_SPECS}
    assert actual == EXPECTED_POLICIES


@pytest.mark.parametrize("tool_name", sorted(EXPECTED_POLICIES))
def test_policy_lookup_by_name_matches_golden(tool_name: str) -> None:
    """只有工具名的调用方按名查到的策略，与工具自带的策略一致。"""
    assert _policy_dict(_policy_by_name(tool_name)) == EXPECTED_POLICIES[tool_name]


def test_default_policy_excludes_delegate() -> None:
    """沿用默认档位的新工具不会自动对被委派的自建 Agent 开放。"""
    assert _policy_dict(_default_policy()) == EXPECTED_DEFAULT_POLICY


def test_unknown_tool_policy_cannot_bind_or_call() -> None:
    assert _policy_dict(_policy_by_name("NoSuchTool")) == EXPECTED_UNKNOWN_POLICY


def test_profiles_and_skill_allowlist_match_golden() -> None:
    profiles = list(_profiles())

    assert profiles == EXPECTED_PROFILES
    assert [p for p in profiles if _requires_skill_allowlist(p)] == (
        EXPECTED_SKILL_ALLOWLIST_PROFILES
    )


class _RecordingCatalogRepository:
    def __init__(self) -> None:
        self.synced: list[dict[str, Any]] = []
        self.kept_names: set[str] | None = None

    async def sync_builtin(self, **kwargs: Any) -> None:
        self.synced.append(kwargs)

    async def mark_missing_deleted(self, runtime_names: set[str]) -> None:
        self.kept_names = runtime_names


async def test_catalog_sync_writes_golden_metadata() -> None:
    """tools 表同步写入的 tool_key 与元数据，与发给模型的工具描述同源且顺序一致。"""

    async def _flush() -> None:
        return None

    service = ToolCatalogService.__new__(ToolCatalogService)
    service.db = SimpleNamespace(flush=_flush)
    repo = _RecordingCatalogRepository()
    service.repo = repo

    await service.sync_builtins()

    expected = [
        {
            "tool_key": item["name"],
            "name": item["name"],
            "description": item["description"],
            "runtime_name": item["name"],
            "input_schema": item["input_schema"],
        }
        for item in EXPECTED_SPECS
    ]
    assert repo.synced == expected
    assert _ordered(repo.synced) == _ordered(expected)
    assert repo.kept_names == {item["name"] for item in EXPECTED_SPECS}
