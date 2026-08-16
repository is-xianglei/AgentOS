# Research: 工具链可行性核实（FastAPI 动态路由 / import-linter 契约）

- **Query**: 核实 design.md 6.4（路由动态挂卸）与 3.3（import-linter 三契约）两处技术断言
- **Scope**: mixed（官方文档 + 仓库内已安装版本源码精读 + 本地可复现实测）
- **Date**: 2026-08-15

## 版本基线（仓库实测）

| 组件 | 版本 | 来源 |
|---|---|---|
| fastapi | 0.136.3 | `uv.lock` + `.venv/.../fastapi` 实装 |
| starlette | 1.3.0 | 同上（fastapi 传递依赖） |
| Python | 3.14 | `.venv/lib/python3.14` |
| import-linter | **项目未安装**；PyPI 最新 2.13 | `uv.lock` 无记录；PyPI JSON API |

import-linter 2.13 的 classifiers 明确支持 Python 3.10–3.14（官方自 2.5.1 起支持 3.14），
本机 `uvx --python 3.14 --from import-linter lint-imports` 实测运行正常。

---

## 结论一：路由随插件挂载/卸载在 FastAPI 上**可行**（附 4 条硬约束）

实测方式：用项目 venv 内的 fastapi 0.136.3 跑了 13 项断言（TestClient 启动 lifespan 后动态
挂载、验证 OpenAPI 缓存、按身份卸载、url_path_for 反查），**全部 PASS**。以下逐问回答。

### 1. 启动后调用 `app.include_router(...)` 是否生效

**生效（实测 PASS）**。挂载不需要手工操作 `app.router.routes`，直接调公开 API 即可：

- 源码路径（fastapi 0.136.3）：`FastAPI.include_router` → `APIRouter.include_router`
  （`fastapi/routing.py`）→ 对每个源 Route 调 `add_api_route` → `self.routes.append(...)`。
  全程没有任何「应用已启动」守卫。
- 对照组：中间件有守卫——`add_middleware` 在启动后抛
  `RuntimeError("Cannot add middleware after an application has started")`
  （starlette 1.3.0 `applications.py:98-101`）。路由没有对应限制，是有意的不对称。
- **官方文档定位**：FastAPI/Starlette 官方文档均未明文承诺「启动后可挂载」，也未禁止。
  此结论属于「公开 API + 源码保证 + 实测」，非官方书面承诺（推断依据：路由匹配是每请求
  线性扫描 list，见第 4 问，因此追加天然即时生效）。

两个源码级陷阱（design.md 未提，必须写进设计）：

- **include_router 是复制不是引用**：它为每个源 Route 新建 `APIRoute` 对象
  （`route_class_override=type(route)`），实测 `added[0] is not plugin_router.routes[0]`。
  所以「按对象身份卸载」必须在挂载时对 `app.router.routes` 做前后快照 diff 拿到新对象，
  拿插件自己 `router.routes` 里的对象去 remove 永远删不掉。
- **lifespan/on_startup 合并不可逆且不执行**：`include_router` 无条件把子 router 的
  `on_startup/on_shutdown` 追加进 app 并 `_merge_lifespan_context` 包一层。启动后挂载时
  这些回调不会运行（实测 PASS：`startup_ran == []`），卸载也无法「反合并」，反复挂卸会让
  lifespan 包装链单调增长。约束：**插件 router 禁止携带 lifespan/on_startup/on_shutdown**，
  初始化由插件管理器自己做。

### 2. `app.openapi_schema` 缓存机制

**design.md 的断言正确**：置 `None` 即强制重算，且这是与官方文档一致的做法。

- **官方文档说的**（fastapi.tiangolo.com/how-to/extending-openapi/，官方仓库
  `docs/en/docs/how-to/extending-openapi.md` 原文）：
  > By default, what the method `.openapi()` does is check the property `.openapi_schema`
  > to see if it has contents and return them. If it doesn't, it generates them...
- **源码**（fastapi 0.136.3 `applications.py:1082`）：`if not self.openapi_schema:` 才重新
  `get_openapi(...)`。`not` 判断保证置 `None`（或空 dict）后下一次访问必然重算——
  「置 None 失效」是官方文档化缓存语义的直接推论 + 源码事实，官方没有原话写「set None」。
- **没有别的服务端缓存层**（源码 + 实测）：
  - `/openapi.json` 的 handler（`setup()`，`applications.py:1104-1116`）每次请求调
    `self.openapi()`，直接 `JSONResponse(schema)`，无缓存头；`root_path_in_servers`
    分支做的浅拷贝是每请求派生，不落缓存。
  - `/docs` 与 `/redoc` 返回的是静态 HTML 壳，schema 由浏览器端再去拉 `/openapi.json`，
    服务端不缓存 schema（实测：卸载后 `/docs` 200 且 HTML 只含 openapi.json 引用）。
- 实测序列：挂载后不清缓存 → `/openapi.json` 是旧的（不含新路由）；置 `None` → 新路由出现；
  卸载 + 置 `None` → 新路由消失。三步全 PASS。

### 3. 移除路由：无官方 API；直接删 list 的副作用逐项核查

**FastAPI 与 Starlette 都没有移除路由的官方 API**（精读 starlette 1.3.0 `routing.py` 全文
确认：`Router` 只有 `add_route/add_websocket_route/mount/host` 这些追加方法）。直接操作
`app.router.routes`（普通 `list`，`routing.py:578`）的副作用逐项核查如下：

| 关注点 | 结论 | 依据 |
|---|---|---|
| 路由匹配 | 删除立即生效，无残留 | 每请求线性扫 `for route in self.routes`（`routing.py:674`），无索引/缓存 |
| `url_path_for`/`url_for` | 立即失效，**不存在任何缓存** | 每次调用线性遍历（`routing.py:621-627`）；实测删除后抛 `NoMatchFound` |
| 中间件 | 不受影响，无需重建 | `middleware_stack` 首请求惰性构建、包裹的是 Router **对象引用**，routes 变化直接可见 |
| 依赖缓存 | 无跨请求残留 | FastAPI 依赖缓存是 per-request（`solve_dependencies` 的 `dependency_cache`） |
| 挂载的子应用 | `Mount` 也是 routes 里的一个元素 | 删除该元素即整棵子树摘除，同样规则 |
| 405 partial 匹配 / 斜杠重定向 | 同一循环内同一张表 | `routing.py:682-708`，无独立缓存 |

design.md 说的「清 `url_path_for` 相关缓存」**没有对应物**——该缓存在 starlette 1.3.0 中
不存在（全包 grep 无 `lru_cache`/`functools.cache`），这半句应删除。

另一个值得写明的坑：`Route.__eq__` 是**值相等**（path/endpoint/methods 相同即相等，
`routing.py:278-284`），所以卸载不要用 `list.remove(route)`（有误删同值路由的理论风险），
应按 `id()` 集合过滤重建整表。

### 4. 匹配有没有编译期缓存

**没有**。每个 `Route` 的正则在 `__init__` 里由 `compile_path` 编译一次，存在**实例属性**
`self.path_regex` 上（`routing.py:236`）；不存在全局正则表、trie 或 LRU（grep 实证）。
删除 Route 对象即连同其正则一起消失，不会残留。模块级的 `PARAM_REGEX` 只用于解析路径
模板本身，与请求匹配无关。

### 5. 并发安全（本节全部为源码推断，官方无说明）

- **单进程内基本安全，推荐 copy-on-write**。推断依据：匹配循环（`routing.py:674-691`）
  内没有 `await`——`route.matches()` 是同步调用，命中后 `await route.handle()` 随即
  `return`，循环不会在挂起后继续迭代。因此在 asyncio 单事件循环里，扫描相对其他协程是
  原子的；从 async 端点里同步地改表不会与任何进行中的扫描交错。
- **在途请求不受卸载影响**：请求一旦匹配，handler 持有 Route 对象引用，从 list 移除
  不影响其完成（Python 引用计数保证对象存活）。若需「卸载前排空在途请求」，要另做
  计数（路由表本身不提供）。
- **仍要防两个场景**：变更操作来自线程池（sync def 端点）时与事件循环里的扫描构成真数据
  竞争；Python 3.14 free-threaded 构建下 list 迭代中原地变异可能跳读。两者都用同一个办法
  消除：**不做原地 remove，整表重建后一次性重绑 `app.router.routes = new_list`**（引用
  赋值原子），并用 `asyncio.Lock` 串行化挂/卸自身。
- **多 worker 是真正的硬约束**：`uvicorn --workers N` 下每个进程有独立 app 实例，动态
  挂载是**单进程状态**，管理面请求只会落在一个 worker。设计必须三选一：单 worker；
  控制面广播（如 Redis pub/sub，各 worker 收到后各自执行挂/卸）；或挂载决策全部放启动期。

### 6. 最小可靠实现骨架（实测验证过的操作序列）

```python
import asyncio

from fastapi import APIRouter, FastAPI


class PluginRouteManager:
    """插件路由挂卸。约束：仅在事件循环内调用；多 worker 部署需每个进程各自执行。"""

    def __init__(self, app: FastAPI) -> None:
        self._app = app
        self._lock = asyncio.Lock()
        # plugin_id -> 该插件贡献的 Route 对象 id 集合（include_router 会复制 Route，
        # 必须在挂载时快照 diff，事后无法从插件 router 反查）
        self._owned: dict[str, frozenset[int]] = {}

    async def mount(self, plugin_id: str, router: APIRouter, prefix: str = "") -> None:
        async with self._lock:
            if plugin_id in self._owned:
                raise RuntimeError(f"插件 {plugin_id} 的路由已挂载")  # 防重复挂载产生重复路由
            before = len(self._app.router.routes)
            self._app.include_router(router, prefix=prefix)  # 只会向表尾追加
            added = self._app.router.routes[before:]
            self._owned[plugin_id] = frozenset(id(r) for r in added)
            self._app.openapi_schema = None  # 迫使下一次 /openapi.json 重算

    async def unmount(self, plugin_id: str) -> None:
        async with self._lock:
            owned = self._owned.pop(plugin_id, frozenset())
            # copy-on-write 整表重绑：不原地 remove（Route.__eq__ 是值相等，且避免迭代中变异）
            self._app.router.routes = [
                r for r in self._app.router.routes if id(r) not in owned
            ]
            self._app.openapi_schema = None
```

配套约束（骨架之外，必须写进设计）：

1. 插件 router 禁止携带 lifespan / on_startup / on_shutdown（不执行、不可逆，见第 1 问）。
2. 插件禁止动态注册中间件与全局异常处理器：`add_middleware` 启动后直接抛 RuntimeError；
   `add_exception_handler` 启动后改的是已被固化进 middleware_stack 的 dict 快照，静默无效。
3. 路由 path/operation_id 冲突要在 mount 前自查（重复挂载是重复路由，先到先匹配，
   OpenAPI 生成时报 duplicate operation id 警告）。
4. 替代方案（若最终不想改 routes 表）：启动时固定挂一个 `Mount("/plugins", dispatcher)`，
   dispatcher 是自持 `Router` 的 ASGI 应用，挂卸只替换 dispatcher 内部表。代价：这些路由
   进不了主应用的 OpenAPI（Mount 子树不参与父 schema 聚合），需要自行合并文档。鉴于直改
   routes 已被证实无残留，此方案仅作备选。

---

## 结论二：import-linter **能表达三条契约**，但不是 design.md 设想的写法

实测方式：在 /tmp 搭了与设计同名的顶层包树（kernel、agent_loop、shell、shell_local、
llm、llm_anthropic、tool_bash、composition、tests），种入 4 处违规，用
`uvx --python 3.14 --from import-linter lint-imports`（装到的是 2.13）跑通：
4 处违规全部命中、合法导入全部放行、修复后 4 条契约全 KEPT。

### 1. 当前契约类型（2.13 官方文档）

| 类型 | 语义 | 关键选项 |
|---|---|---|
| `forbidden` | 源模块集不得 import 禁止模块集（默认含**间接**链） | `allow_indirect_imports` 放宽为只禁直接；`as_packages` |
| `protected` | **任何模块**不得**直接** import 受保护模块，白名单 `allowed_importers` 除外 | 2.5（2025-09）新增，正对本任务需求 |
| `layers` | 分层：上层可依赖下层，反向禁止（含间接） | `containers`、同层多元素（竖线/冒号分隔）、`exhaustive` |
| `independence` | 一组模块两两互不依赖（含间接） | — |
| `acyclic_siblings` | 兄弟包之间禁止依赖环 | — |
| 自定义 | 见第 5 点 | `contract_types` 注册 |

来源：github.com/seddonym/import-linter（main 分支 docs/，对应 2.13）
`docs/contract_types/*.md`、`docs/release_notes.md`。

### 2. 三条契约的表达（实测通过的完整配置）

关键结论：**契约 1 与契约 2 用 `protected` 而非 `forbidden`**。protected 天生就是
「白名单 importer」语义，不需要把「所有其他包」列成 source；且它只查**直接** import，
恰好符合意图——任何间接链的最后一跳必然是某个包直接 import 了 provider，逃不掉。

```toml
[tool.importlinter]
root_packages = [
    "kernel", "agent_loop", "shell", "shell_local",
    "llm", "llm_anthropic", "tool_bash", "composition", "tests",
]
include_external_packages = true   # 契约 3 若要管外部依赖则必须开

# 契约 2：agent_loop 只能被组合根导入（若要绝对封禁，allowed_importers = [] 也合法，已实测）
[[tool.importlinter.contracts]]
id = "loop-protected"
name = "agent_loop 只能被组合根导入"
type = "protected"
protected_modules = ["agent_loop"]
allowed_importers = ["composition"]

# 契约 1：每个 provider 一条，白名单 = 组合根 + 该 provider 自己的测试
[[tool.importlinter.contracts]]
id = "provider-shell-local"
name = "shell_local 只能被组合根与自身测试导入"
type = "protected"
protected_modules = ["shell_local"]
allowed_importers = ["composition", "tests.shell_local"]

[[tool.importlinter.contracts]]
id = "provider-llm-anthropic"
name = "llm_anthropic 只能被组合根与自身测试导入"
type = "protected"
protected_modules = ["llm_anthropic"]
allowed_importers = ["composition", "tests.llm_anthropic"]

# 契约 3：kernel 零内部依赖（内部包穷举；外部白名单见第 5 点自定义契约）
[[tool.importlinter.contracts]]
id = "kernel-pure"
name = "kernel 零内部依赖"
type = "forbidden"
source_modules = ["kernel"]
forbidden_modules = [
    "agent_loop", "shell", "shell_local",
    "llm", "llm_anthropic", "tool_bash", "composition",
]
```

实测输出摘录（种入违规时）：

```
agent_loop 只能被组合根导入 BROKEN      - shell.loop_user -> agent_loop (l.1)
shell_local 只能被组合根与自身测试导入 BROKEN
    - tests.llm_anthropic -> shell_local (l.1)   ← 别的 provider 的测试越权，被拒
    - tool_bash -> shell_local (l.2)
kernel 零内部依赖 BROKEN                - kernel.bad -> shell (l.1)
```

`composition -> shell_local` 与 `tests.shell_local -> shell_local` 正确放行。
**「只有组合文件和该 provider 自己的测试可以 import 它」可以精确表达**，粒度按
「每 provider 一条契约」展开，契约数量与 provider 数量线性相关。

### 3. 通配符边界：`*_local` 这类模式**不支持**

- **官方文档原文**（`docs/contract_types/index.md#wildcards`）：`*` 顶替**一个完整的**
  模块名、`**` 顶替任意多级；并明确举了反例——
  > `mypackage.foo*`: not a valid expression. (The wildcard must replace a whole module name.)
- **实测**：`protected_modules = ["*_local"]` 直接配置报错
  `A wildcard can only replace a whole module.`（lint 不运行）。
- 时间线：forbidden/independence 契约通配符自 2.1（2024-10），`ignore_imports` 通配符
  更早（1.2.5 起，递归通配 1.10 起）。
- **替代写法**（按推荐顺序）：
  1. **穷举 provider 包名**（本报告采用）：包总量可控（个位数到十几个），配合一条
     「新增顶层包必须同步登记契约」的评审规则或生成脚本（列目录 diff 契约清单）即可。
  2. 命名收敛：providers 收进公共命名空间（如 `providers.shell_local`），即可用
     `providers.*` 一条通配。代价是推翻「每个 seam 角色独立顶层包」的 3.2 决策，不建议
     仅为省配置而改。
  3. 自定义契约类型：用正则匹配 `*_local`/`*_sandbox` 后缀，见第 5 点，约 40-80 行。

### 4. 例外的表达

- `protected.allowed_importers`：importer 白名单，支持通配符（如 `tests.shell_local.**`），
  空列表合法（= 任何人不得直接 import，已实测 KEPT/BROKEN 两态）。
- `ignore_imports`（所有内置契约通用）：按「`importer -> imported`」逐条豁免具体导入边，
  支持通配符；`unmatched_ignore_imports_alerting = error/warn/none` 控制「豁免规则没匹配到
  任何真实导入」时的告警（默认 error，防豁免规则腐化）。
- 精确到「该 provider 自己的测试」的前提：tests 必须进导入图，即 `tests/` 及其子目录补
  `__init__.py` 并列入 `root_packages`（root package 必须可导入，cwd 下即可）。若不把
  tests 纳入图，所有测试代码自动豁免——比目标宽松，作为过渡也可接受。
- 本项目现状注意：`tests/` 目前是无 `__init__.py` 的顶层目录（pytest `testpaths` +
  `pythonpath=["."]`），要启用「测试也受契约管」需补包化，这是一个需要在设计里明确的
  小改动。

### 5. 自定义契约：官方支持，成本低

官方文档 `docs/custom_contract_types.md` 给出完整扩展点：继承 `importlinter.Contract`、
声明 fields（有现成的 `ModuleExpressionField` 可复用官方通配符解析）、实现
`check(graph, verbose) -> ContractCheck` 与 `render_broken_contract`，在配置里
`contract_types = ["kernel_pure: yourpkg.contracts.PureKernelContract"]` 注册。
graph 是 grimp 的 `ImportGraph`，API 文档齐全。成本评估：40-80 行、无第三方依赖。

「kernel 允许依赖集合为空（除标准库与 Pydantic）」的**白名单语义**内置契约表达不了
（forbidden 是黑名单制，无法写「除 pydantic 外的一切外部包」），自定义契约骨架：

```python
import sys

from importlinter import Contract, ContractCheck, fields, output


class PureKernelContract(Contract):
    """kernel 的 import 白名单：仅标准库 + 显式列出的外部包。需 include_external_packages。"""

    kernel_package = fields.StringField()
    allowed_external = fields.SetField(subfield=fields.StringField())

    def check(self, graph, verbose):
        violations = []
        modules = {self.kernel_package} | graph.find_descendants(self.kernel_package)
        for module in modules:
            for imported in graph.find_modules_directly_imported_by(module):
                top = imported.split(".")[0]
                if top == self.kernel_package:
                    continue
                if top in sys.stdlib_module_names or top in self.allowed_external:
                    continue
                violations.append(f"{module} -> {imported}")
        return ContractCheck(kept=not violations, metadata={"violations": violations})

    def render_broken_contract(self, check):
        for v in check.metadata["violations"]:
            output.print_error(v)
```

注：此骨架样式与官方文档示例同构（官方示例即用 `graph.get_import_details` 写 forbidden
变体），`sys.stdlib_module_names` 是 3.10+ 标准库能力。**本骨架未实测运行**，标注为
「官方扩展点 + 推断」；若采用，落地时按官方示例补一条最小自测。

### 6. 与 uv / pyproject.toml / CI 的集成

- 配置文件：官方支持 `setup.cfg` / `.importlinter`（INI）/ **`pyproject.toml`**
  （`[tool.importlinter]`，本报告片段即该格式），三选一，放 pyproject 与项目现状最贴合。
- 安装：`uv add --dev import-linter`（项目 uv.lock 目前**没有**它，需新增 dev 依赖；
  锁定 `>=2.5.1` 以获得 protected 契约 + Python 3.14 官方支持）。
- 运行：`uv run lint-imports`（入口脚本名就叫 `lint-imports`，别名 `import-linter lint`）。
  常用参数：`--contract <id>` 只跑单条、`--no-cache` / `--cache-dir`（默认缓存目录
  `.import_linter_cache`，应加入 .gitignore）、`--verbose`、`--show-timings`。
- CI 即一行：`uv run lint-imports`（如需干净结果加 `--no-cache`）。root packages 必须
  可导入——在仓库根目录执行即满足（包在 cwd 下）。

### 7. 替代工具（结论：不必换）

以下对比除 import-linter 外均为训练知识 + 简要推断，未逐一联网核实 2026 年现状：

| 工具 | 模型 | 与本任务的匹配度 |
|---|---|---|
| import-linter 2.13 | 契约制（黑名单 forbidden + 白名单 protected + 分层），INI/TOML 配置 | 三条契约全部可表达（已实测），唯一缺口是外部依赖白名单需自定义契约 |
| tach | **白名单制**：每模块声明 `depends_on`，未声明即禁止 | 天然表达契约 3；但引入另一套配置模型（tach.toml），且成熟度/维护节奏需另行调研 |
| pytestarch | 在 pytest 里写架构断言 | 灵活但规则散在测试代码里，CI 报告不如契约清单直观 |
| grimp 直接用 | import-linter 的底层图库，自己写断言 | 等价于全部自定义契约，失去统一 CLI/报告，无必要 |

import-linter 已满足需求且是此类工具中最成熟的（protected 契约 2025-09 新增正好补齐
白名单场景），**不建议换**。

---

## 对 design.md 的修正建议

### 6.4（路由动态挂卸）

- 「`app.openapi_schema` 挂载/卸载后必须置 `None` 强制重算」——**正确，无需修改**。
  可补一句：`/docs`、`/openapi.json` 无其他服务端缓存层（HTML 壳 + 每请求重读缓存属性）。
- 「从 `app.router.routes` 按对象身份移除该 router 贡献的全部 Route，并清 `url_path_for`
  相关缓存」——**前半句改写、后半句删除**。建议改为：
  > 卸载：挂载时对 `app.router.routes` 做前后快照 diff 记下新增 Route 对象
  > （include_router 会为源 Route 复制新对象，事后无法从插件 router 反查），
  > 卸载时按 `id()` 过滤重建整表并一次性重绑（copy-on-write，不用 `list.remove`——
  > `Route.__eq__` 是值相等）。`url_path_for` 每次线性遍历路由表，没有缓存需要清。
- 建议在 6.4 末尾**新增四条硬约束**（原文缺失，均有源码/实测依据）：
  1. 插件 router 禁止携带 lifespan/on_startup/on_shutdown——启动后挂载不会执行，
     且合并不可逆，反复挂卸使 lifespan 包装链增长；
  2. 插件禁止动态注册中间件与全局异常处理器——前者启动后抛 RuntimeError，后者静默无效；
  3. 挂/卸需 `asyncio.Lock` 串行化并只在事件循环内执行；
  4. 多 worker 部署下动态挂载是单进程状态，需明确选择：单 worker / 控制面广播 /
     启动期装配。

### 3.3（import-linter 三契约）

- 「禁止任何包 import 具体 provider 包……例外是组合文件与该 provider 自己的测试」——
  **可表达，但不要写成 forbidden + `*_local` 通配**（部分名通配符不合法，实测报错）。
  改为：**每 provider 一条 `protected` 契约**（`protected_modules=[shell_local]`,
  `allowed_importers=["composition", "tests.shell_local"]`），并补一条维护规则：
  新增 provider 顶层包必须同步登记契约（评审 checklist 或目录 diff 脚本）。
- 「禁止任何包 import agent_loop」——一条 `protected` 契约即可；若组合根也不允许
  （纯动态加载），`allowed_importers = []` 合法。
- 「kernel 的允许依赖集合为空（除标准库与 Pydantic）」——拆成两半表达：内部依赖用
  `forbidden`（source=kernel，forbidden=其余顶层包穷举，已实测）；外部依赖白名单内置
  契约表达不了，二选一并在设计中写明：a) 黑名单枚举重点外部依赖（fastapi/sqlalchemy/
  anthropic 等，务实但不完备）；b) 自定义契约精确白名单（约 40-80 行，本报告有骨架）。
- 需明确的配套决策：tests 是否纳入导入图（补 `__init__.py` + 列入 root_packages）；
  import-linter 加入 dev 依赖并锁 `>=2.5.1`；`.import_linter_cache` 入 .gitignore。

## 证据清单

- 官方文档：FastAPI `docs/en/docs/how-to/extending-openapi.md`（fastapi/fastapi main）；
  import-linter docs main 分支（对应 2.13）：`contract_types/{index,forbidden,protected,
  layers,independence}.md`、`custom_contract_types.md`、`get_started/{configure,run}.md`、
  `release_notes.md`（protected 于 2.5 新增、3.14 支持自 2.5.1、通配符自 2.1）。
- 仓库内源码精读：`.venv/.../starlette/routing.py`（1.3.0 全文：578/621-627/674-691/236 行）、
  `starlette/applications.py`（98-101 行）、`fastapi/applications.py`（0.136.3：1068-1116 行）、
  `fastapi/routing.py`（include_router 全体）。
- 本地实测：`/tmp/fastapi_dyn_test.py`（13 项断言全 PASS，项目 venv）；`/tmp/il_test`
  （import-linter 2.13 + Python 3.14：4 处种入违规全命中、修复后 4 契约 KEPT、
  `*_local` 配置报错原文、`allowed_importers=[]` 合法性）。
- PyPI JSON API：import-linter 最新 2.13，classifiers 支持 Python 3.10–3.14。

## Caveats / Not Found

- 「启动后可挂载路由」与「并发安全分析」没有官方书面承诺，属源码推断 + 实测，已逐处标注。
- 自定义契约骨架未实测运行（官方扩展点文档完备，风险低）。
- 替代工具（tach 等）现状为训练知识，未联网核实；因 import-linter 已满足需求，未深入。
- 本子代理中 Context7 MCP 工具未注册（调用报 No such tool available），改用 GitHub 官方
  仓库原始文档 + PyPI JSON + 本地实测双路径核实，证据等级不低于原计划。
