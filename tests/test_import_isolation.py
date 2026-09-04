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


def _resolve_targets(
    node: ast.Import | ast.ImportFrom,
    source: str,
    source_package: str,
    known: set[str],
) -> set[str]:
    """把一条 import 语句解析为一方模块目标集合。

    目标的每一级祖先包都记为边（导入子模块必先执行祖先 ``__init__``），
    但 source 自身及其祖先不算：子模块回引父包时父包必已在 ``sys.modules``，
    这正是 llm/plan/prompt 包内"自环"不构成加载期问题的原因。
    """
    raw: set[str] = set()
    if isinstance(node, ast.Import):
        raw.update(alias.name for alias in node.names)
    else:
        base = node.module or ""
        if node.level:
            anchor = source_package.split(".")
            anchor = anchor[: len(anchor) - (node.level - 1)]
            base = ".".join([*anchor, base]) if base else ".".join(anchor)
        for alias in node.names:
            candidate = f"{base}.{alias.name}" if base else alias.name
            raw.add(candidate if candidate in known else base)
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
