"""加载期循环导入回归测试。

三层防线：
1. 静态：AST 收集全部一方模块的模块级 import 做 Tarjan SCC，断言跨模块环数为 0（毫秒级）；
2. 哨兵：历史上作为首个导入必炸的 8 个模块 + 主要入口逐个起子进程 ``python -c "import X"``；
3. 全量：全部一方模块并行子进程探测，覆盖哨兵清单之外的新模块。

失败信息一律携带模块名与子进程 stderr 末行，不吞异常。
"""

from __future__ import annotations

import ast
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent

# 非源码目录：测试自身、迁移脚本、文档与工具产物不参与探测。
_SKIP_TOP_DIRS = frozenset({"tests", "alembic", "docs"})
_SKIP_PATH_PARTS = frozenset({"__pycache__", "versions"})

# 修复前作为首个导入即炸的 8 个模块，加上主要入口；单独参数化便于失败时一眼定位。
FIRST_IMPORT_MODULES: tuple[str, ...] = (
    "session.repository",
    "session.service",
    "task.service",
    "team.service",
    "interaction.service",
    "runtime.subagent",
    "runtime.compact",
    "memory.jobs.extraction",
    "runtime.agent",
    "runtime.workflow",
    "tools.registry",
    "tools.builtin.agent",
    "database.registry",
    "api.router",
    "main",
)

_SUBPROCESS_TIMEOUT_SECONDS = 60
_PARALLEL_WORKERS = 8


def _first_party_module_files() -> dict[str, Path]:
    """枚举一方模块：顶层含 ``__init__.py`` 的包（递归）加 ``main``，``__init__`` 映射为包名。"""
    files: dict[str, Path] = {"main": PROJECT_ROOT / "main.py"}
    for pkg_dir in sorted(PROJECT_ROOT.iterdir()):
        if (
            not pkg_dir.is_dir()
            or pkg_dir.name in _SKIP_TOP_DIRS
            or pkg_dir.name.startswith(".")
            or not (pkg_dir / "__init__.py").exists()
        ):
            continue
        for path in sorted(pkg_dir.rglob("*.py")):
            relative = path.relative_to(PROJECT_ROOT)
            if _SKIP_PATH_PARTS & set(relative.parts):
                continue
            parts = list(relative.with_suffix("").parts)
            if parts[-1] == "__init__":
                parts.pop()
            files[".".join(parts)] = path
    return files


def _is_type_checking_guard(node: ast.If) -> bool:
    """识别 ``if TYPE_CHECKING:`` / ``if typing.TYPE_CHECKING:`` 守卫。"""
    test = node.test
    if isinstance(test, ast.Name):
        return test.id == "TYPE_CHECKING"
    return isinstance(test, ast.Attribute) and test.attr == "TYPE_CHECKING"


def _iter_load_time_imports(tree: ast.Module) -> list[ast.Import | ast.ImportFrom]:
    """只收集加载期会执行的 import：进入 if/try/with 与类体，跳过函数体与 TYPE_CHECKING 块。"""
    found: list[ast.Import | ast.ImportFrom] = []
    stack: list[ast.AST] = list(tree.body)
    while stack:
        node = stack.pop()
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            found.append(node)
            continue
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
            continue
        if isinstance(node, ast.If) and _is_type_checking_guard(node):
            stack.extend(node.orelse)
            continue
        stack.extend(ast.iter_child_nodes(node))
    return found


def _ancestors(module: str) -> set[str]:
    """``a.b.c`` -> ``{"a", "a.b"}``。"""
    parts = module.split(".")
    return {".".join(parts[:i]) for i in range(1, len(parts))}


def _imported_names(
    node: ast.Import | ast.ImportFrom,
    source_package: str,
    known: set[str],
) -> set[str]:
    """把一条 import 语句解析为绝对模块名：相对导入按所在包展开，第三方模块原样保留。"""
    if isinstance(node, ast.Import):
        return {alias.name for alias in node.names}
    base = node.module or ""
    if node.level:
        anchor = source_package.split(".")
        anchor = anchor[: len(anchor) - (node.level - 1)]
        base = ".".join([*anchor, base]) if base else ".".join(anchor)
    names: set[str] = set()
    for alias in node.names:
        candidate = f"{base}.{alias.name}" if base else alias.name
        names.add(candidate if candidate in known else base)
    return names


def _resolve_targets(
    node: ast.Import | ast.ImportFrom,
    source: str,
    source_package: str,
    known: set[str],
) -> set[str]:
    """把一条 import 语句解析为一方模块目标集合。

    目标的每一级祖先包都记为边（导入子模块必先执行祖先 ``__init__``），
    但 source 自身及其祖先不算：子模块回引父包时父包必已在 ``sys.modules``，
    这正是 plan/prompt 包内"自环"不构成加载期问题的原因。
    """
    raw = _imported_names(node, source_package, known)
    targets: set[str] = set()
    excluded = _ancestors(source) | {source}
    for name in raw:
        for candidate in {name, *_ancestors(name)}:
            if candidate in known and candidate not in excluded:
                targets.add(candidate)
    return targets


def _build_import_graph() -> dict[str, set[str]]:
    """构建一方模块的加载期导入图。"""
    files = _first_party_module_files()
    known = set(files)
    graph: dict[str, set[str]] = {}
    for module, path in files.items():
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        is_package = path.name == "__init__.py"
        source_package = module if is_package else module.rpartition(".")[0]
        edges: set[str] = set()
        for node in _iter_load_time_imports(tree):
            edges |= _resolve_targets(node, module, source_package, known)
        graph[module] = edges
    return graph


def _tarjan_scc(graph: dict[str, set[str]]) -> list[list[str]]:
    """迭代版 Tarjan，返回全部强连通分量。"""
    index_of: dict[str, int] = {}
    low: dict[str, int] = {}
    on_stack: set[str] = set()
    stack: list[str] = []
    components: list[list[str]] = []
    counter = 0

    for root, root_edges in graph.items():
        if root in index_of:
            continue
        work: list[tuple[str, list[str]]] = [(root, sorted(root_edges))]
        index_of[root] = low[root] = counter
        counter += 1
        stack.append(root)
        on_stack.add(root)
        while work:
            node, pending = work[-1]
            if pending:
                nxt = pending.pop()
                if nxt not in index_of:
                    index_of[nxt] = low[nxt] = counter
                    counter += 1
                    stack.append(nxt)
                    on_stack.add(nxt)
                    work.append((nxt, sorted(graph.get(nxt, set()))))
                elif nxt in on_stack:
                    low[node] = min(low[node], index_of[nxt])
                continue
            work.pop()
            if work:
                parent = work[-1][0]
                low[parent] = min(low[parent], low[node])
            if low[node] == index_of[node]:
                comp: list[str] = []
                while True:
                    member = stack.pop()
                    on_stack.discard(member)
                    comp.append(member)
                    if member == node:
                        break
                components.append(sorted(comp))
    return components


def _import_in_subprocess(module: str) -> tuple[str, int, str]:
    """在干净解释器中把 module 作为首个导入，返回 (模块名, 返回码, stderr 末行)。"""
    result = subprocess.run(
        [sys.executable, "-c", f"import {module}"],
        cwd=PROJECT_ROOT,
        capture_output=True,
        text=True,
        timeout=_SUBPROCESS_TIMEOUT_SECONDS,
        check=False,
    )
    lines = result.stderr.strip().splitlines()
    return module, result.returncode, lines[-1] if lines else ""


def test_no_module_level_import_cycles() -> None:
    """静态防线：一方模块的加载期导入图不得存在跨模块强连通分量。"""
    graph = _build_import_graph()
    assert len(graph) >= 100, f"模块枚举异常，仅发现 {len(graph)} 个一方模块"
    cycles = [comp for comp in _tarjan_scc(graph) if len(comp) > 1]
    detail = "\n".join(f"  - {' , '.join(comp)}" for comp in cycles)
    assert not cycles, f"发现 {len(cycles)} 个加载期循环导入分量:\n{detail}"


@pytest.mark.parametrize("module", FIRST_IMPORT_MODULES)
def test_module_imports_cleanly_as_first_import(module: str) -> None:
    """哨兵防线：历史踩坑模块与主要入口逐个作为首个导入必须成功。"""
    _, returncode, last_line = _import_in_subprocess(module)
    assert returncode == 0, (
        f"模块 {module} 作为首个导入失败（returncode={returncode}）: {last_line}"
    )


def test_all_first_party_modules_import_cleanly() -> None:
    """全量防线：全部一方模块并行子进程探测，覆盖哨兵清单之外的新模块。"""
    modules = sorted(_first_party_module_files())
    with ThreadPoolExecutor(max_workers=_PARALLEL_WORKERS) as pool:
        results = list(pool.map(_import_in_subprocess, modules))
    failures = [
        f"  - {module} (returncode={code}): {last_line}"
        for module, code, last_line in results
        if code != 0
    ]
    summary = f"{len(failures)}/{len(modules)} 个模块作为首个导入失败:"
    assert not failures, "\n".join([summary, *failures])


# 领域层（harness.domain）加载期允许触达的项目内模块：统一业务异常与纯值事件类型。
_DOMAIN_ALLOWED_FIRST_PARTY = frozenset({"core", "core.errors", "core.events"})
# 领域层加载期禁止导入的外部框架与 SDK（按顶层包名匹配）。
_DOMAIN_FORBIDDEN_THIRD_PARTY = frozenset({"sqlalchemy", "fastapi", "anthropic", "openai"})


def _domain_layer_violations(
    module: str,
    tree: ast.Module,
    is_package: bool,
    known: set[str],
) -> list[str]:
    """返回 harness.domain 下某模块违反分层规则的加载期导入名。

    禁止三类：harness.app / harness.adapters（含其余 harness 子包，领域层只认自身）、
    SQLAlchemy / FastAPI / 各家 SDK、core.errors 与 core.events 以外的项目内包。
    ``if TYPE_CHECKING:`` 块与函数体内的导入不在加载期执行，不计入。
    """
    source_package = module if is_package else module.rpartition(".")[0]
    first_party_tops = {name.split(".")[0] for name in known}
    violations: list[str] = []
    for node in _iter_load_time_imports(tree):
        for name in sorted(_imported_names(node, source_package, known)):
            top = name.split(".")[0]
            if top in _DOMAIN_FORBIDDEN_THIRD_PARTY:
                violations.append(name)
            elif top == "harness":
                if not (name == "harness" or name.startswith("harness.domain")):
                    violations.append(name)
            elif top in first_party_tops and name not in _DOMAIN_ALLOWED_FIRST_PARTY:
                violations.append(name)
    return violations


def test_domain_layer_imports_respect_layering() -> None:
    """分层防线：harness.domain 加载期只能依赖标准库、pydantic、core.errors 与 core.events。"""
    files = _first_party_module_files()
    known = set(files)
    domain_modules = sorted(
        m for m in files if m == "harness.domain" or m.startswith("harness.domain.")
    )
    assert {"harness.domain.messages", "harness.domain.context"} <= set(domain_modules), (
        f"模块枚举未覆盖 harness 领域层，仅发现: {domain_modules}"
    )
    failures: list[str] = []
    for module in domain_modules:
        path = files[module]
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        bad = _domain_layer_violations(module, tree, path.name == "__init__.py", known)
        failures.extend(f"  - {module} 导入了 {name}" for name in bad)
    assert not failures, "领域层存在违反分层规则的加载期导入:\n" + "\n".join(failures)


@pytest.mark.parametrize(
    ("source", "expected"),
    [
        ("from harness.app import ports", ["harness.app.ports"]),
        ("import harness.adapters.models", ["harness.adapters.models"]),
        ("from sqlalchemy.orm import Mapped", ["sqlalchemy.orm"]),
        ("import fastapi", ["fastapi"]),
        ("from anthropic.types import ToolUseBlock", ["anthropic.types"]),
        ("import openai", ["openai"]),
        ("from session.service import SessionService", ["session.service"]),
        ("from core.config import settings", ["core.config"]),
        ("from ..app import ports", ["harness.app.ports"]),
        ("from core.errors import AgentException", []),
        ("from core.events import ToolInfo", []),
        ("from harness.domain.messages import ToolCall", []),
        ("from typing import Any\nimport pydantic", []),
        ("from typing import TYPE_CHECKING\nif TYPE_CHECKING:\n    import anthropic", []),
        ("def f():\n    import sqlalchemy", []),
    ],
)
def test_domain_layer_checker_reports_violations(source: str, expected: list[str]) -> None:
    """检查器自测：合成模块的违规导入必须被报出，允许项不得误报。"""
    known = set(_first_party_module_files())
    tree = ast.parse(source)

    assert _domain_layer_violations("harness.domain.sample", tree, False, known) == expected


# 应用层（harness.app）加载期禁止导入的外部框架与 SDK（按顶层包名匹配）。
_APP_FORBIDDEN_THIRD_PARTY = frozenset({"sqlalchemy", "fastapi", "anthropic", "openai"})


def _app_layer_violations(
    module: str,
    tree: ast.Module,
    is_package: bool,
    known: set[str],
) -> list[str]:
    """返回 harness.app 下某模块违反分层规则的加载期导入名。

    禁止两类：harness.adapters（应用层只认端口，不认实现）、SQLAlchemy / FastAPI / 各家 SDK。
    ``if TYPE_CHECKING:`` 块与函数体内的导入不在加载期执行，不计入。
    """
    source_package = module if is_package else module.rpartition(".")[0]
    violations: list[str] = []
    for node in _iter_load_time_imports(tree):
        for name in sorted(_imported_names(node, source_package, known)):
            forbidden_sdk = name.split(".")[0] in _APP_FORBIDDEN_THIRD_PARTY
            adapter = name == "harness.adapters" or name.startswith("harness.adapters.")
            if forbidden_sdk or adapter:
                violations.append(name)
    return violations


def test_app_layer_imports_respect_layering() -> None:
    """分层防线：harness.app 加载期不得导入 harness.adapters、SQLAlchemy、FastAPI 与各家 SDK。"""
    files = _first_party_module_files()
    known = set(files)
    app_modules = sorted(m for m in files if m == "harness.app" or m.startswith("harness.app."))
    assert "harness.app.ports" in app_modules, (
        f"模块枚举未覆盖 harness 应用层，仅发现: {app_modules}"
    )
    failures: list[str] = []
    for module in app_modules:
        path = files[module]
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        bad = _app_layer_violations(module, tree, path.name == "__init__.py", known)
        failures.extend(f"  - {module} 导入了 {name}" for name in bad)
    assert not failures, "应用层存在违反分层规则的加载期导入:\n" + "\n".join(failures)


@pytest.mark.parametrize(
    ("source", "expected"),
    [
        (
            "from harness.adapters.models.factory import create_model_client",
            ["harness.adapters.models.factory"],
        ),
        ("from harness import adapters", ["harness.adapters"]),
        ("from ..adapters import models", ["harness.adapters.models"]),
        ("from sqlalchemy.ext.asyncio import AsyncSession", ["sqlalchemy.ext.asyncio"]),
        ("import fastapi", ["fastapi"]),
        ("import anthropic", ["anthropic"]),
        ("from openai.types.chat import ChatCompletionMessageParam", ["openai.types.chat"]),
        ("from harness.domain.messages import ToolCall", []),
        ("from core.config import settings", []),
        ("from session.service import SessionService", []),
        ("from typing import TYPE_CHECKING\nif TYPE_CHECKING:\n    import sqlalchemy", []),
        ("def f():\n    import anthropic", []),
    ],
)
def test_app_layer_checker_reports_violations(source: str, expected: list[str]) -> None:
    """检查器自测：合成模块的违规导入必须被报出，允许项不得误报。"""
    known = set(_first_party_module_files())
    tree = ast.parse(source)

    assert _app_layer_violations("harness.app.sample", tree, False, known) == expected


# 模型 SDK 顶层包名，以及唯一允许导入它们的生产代码包。
_MODEL_SDK_PACKAGES = frozenset({"anthropic", "openai"})
_MODEL_SDK_OWNER = "harness.adapters.models"


def _model_sdk_imports(tree: ast.Module) -> list[str]:
    """返回模块中任何位置导入 anthropic / openai SDK 的模块名。

    比分层检查更严：函数体与 ``if TYPE_CHECKING:`` 块内的导入同样计入，
    SDK 类型出现在适配层以外，意味着 provider wire 形状已越过中立协议。
    相对导入只能指向项目内模块，不计入。
    """
    found: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names = [alias.name for alias in node.names]
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            names = [node.module]
        else:
            continue
        found.extend(name for name in names if name.split(".")[0] in _MODEL_SDK_PACKAGES)
    return sorted(found)


def test_model_sdk_imported_only_by_model_adapters() -> None:
    """分层防线：除 harness.adapters.models 外，生产代码任何位置都不得导入 anthropic、openai。"""
    files = _first_party_module_files()
    owners = {m for m in files if m == _MODEL_SDK_OWNER or m.startswith(f"{_MODEL_SDK_OWNER}.")}
    assert "harness.adapters.models.anthropic_client" in owners, (
        f"模块枚举未覆盖模型适配器，仅发现: {sorted(owners)}"
    )
    failures: list[str] = []
    for module, path in sorted(files.items()):
        if module in owners:
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        failures.extend(f"  - {module} 导入了 {name}" for name in _model_sdk_imports(tree))
    assert not failures, "模型适配器以外的生产代码导入了模型 SDK:\n" + "\n".join(failures)


@pytest.mark.parametrize(
    ("source", "expected"),
    [
        ("import anthropic", ["anthropic"]),
        ("from openai import AsyncOpenAI", ["openai"]),
        ("from anthropic.types import MessageParam", ["anthropic.types"]),
        ("import json, openai.types", ["openai.types"]),
        ("def f():\n    import openai", ["openai"]),
        (
            "from typing import TYPE_CHECKING\nif TYPE_CHECKING:\n    from anthropic import X",
            ["anthropic"],
        ),
        ("from harness.adapters.models import anthropic_client", []),
        ("from .openai_client import OpenAIClient", []),
        ("import anthropic_compat", []),
    ],
)
def test_model_sdk_checker_reports_imports(source: str, expected: list[str]) -> None:
    """检查器自测：任何位置的 SDK 导入都必须被报出，项目内同名模块与相似包名不得误报。"""
    assert _model_sdk_imports(ast.parse(source)) == expected
