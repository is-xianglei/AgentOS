# 解除 session / task / tools 循环导入

## 目标

让仓库内任意一方模块都能作为进程首个导入模块被干净地 `import`，彻底消除加载期循环导入；
补一条子进程级回归测试防止退化；清理为规避环而付出的局部导入与"预热导入"绕行点。

## 背景与复现

上一任务（09-04 workflow 进度事件）在 `tests/test_events_actor.py:199-202` 被迫把
`from runtime.subagent import SubAgentRunner` 塞进函数体内，注释写明原因是
"runtime.subagent 作为首个被导入的模块会触发存量循环导入"。
`tests/test_neutral_context.py:14-19` 也用 `import tools  # noqa: F401` 预热来规避同一环。

在干净解释器中复现（`uv run python -c "import runtime.subagent"`），traceback 摘要：

```
runtime/subagent.py:34        from team.service import TeamService
team/service.py:5             from session.service import SessionService
session/service.py:9          from session.repository import SessionRepository
session/repository.py:20      from tools.models import ToolCallRecord      <- 触发 tools/__init__
tools/__init__.py:1           from tools.registry import ToolRegistry, build_tool_registry
tools/registry.py:8           from tools.builtin import (...)
tools/builtin/__init__.py:11  from tools.builtin.task import (...)
tools/builtin/task.py:7       from task.service import TaskService
task/service.py:6             from session.service import SessionService
ImportError: cannot import name 'SessionService' from partially initialized module
'session.service' (most likely due to a circular import)
```

全仓 168 个一方模块逐个起子进程 `import`（脚本见 design.md 附录），**8 个**作为首个导入即炸：
`session.repository`、`session.service`、`task.service`、`team.service`、`interaction.service`、
`runtime.subagent`、`runtime.compact`、`memory.jobs.extraction`。
`tools`、`runtime.agent`、`runtime.workflow`、`main` 不炸——它们都恰好"从 tools 侧进入"环，
`main.py:20` 先 `from tools.service import ...` 就是这个顺序，生产启动因此没暴露问题。

## 范围

### 范围内

- 解开唯一的跨包加载期环（详见 F 系列），使 168 个模块作为首个导入全部通过。
- 新增 `tests/test_import_isolation.py`：子进程逐模块 import 的回归测试 + 静态 AST 环检测。
- 恢复 `tests/test_events_actor.py`、`tests/test_neutral_context.py` 的绕行写法为顶层导入。
- 清理因本环存在而无法提升、现在可以安全提升的函数内局部导入（见 AC3 清单）。

### 范围外

- 不重构 `tools/builtin/task.py`、`tools/builtin/team.py` 对 Service 的依赖方式（方案 B）。
- 不改 `database/registry.py` 的登记方式，不动 Alembic。
- 不处理"改顶层仍会成环"的局部导入（`skill.service:420`、`workspace.service:306`、
  `session.service:345/349/367/371`、`tools/builtin/agent.py:129`），它们属真实的 Service
  互相引用，需要独立的依赖倒置任务；本任务只登记并说明保留理由。
- 不引入 import-linter 等新依赖，检测用标准库 `ast` + `subprocess` 自写。

## 已确认事实（代码证据）

### F1 全仓只有一个跨包加载期环，且唯一的"放大器"是 `tools/__init__.py`

用 AST 只统计模块级 import（排除函数内局部导入与 `TYPE_CHECKING` 块）做 Tarjan SCC，
跨包强连通分量只有一个：

```
session.repository -> tools -> tools.registry -> tools.builtin -> tools.builtin.task
    -> task.service -> session.service -> session.repository
session.repository -> tools -> tools.registry -> tools.builtin -> tools.builtin.team
    -> team.service -> session.service -> session.repository
session.repository -> tools -> ... -> tools.builtin.team -> team.service -> task.service
    -> session.service -> session.repository
```

三条简单环共用同一段前缀 `session.repository -> tools -> tools.registry -> tools.builtin`，
而 `session.repository` 只想要 `tools.models.ToolCallRecord`（`session/repository.py:20`），
是 `tools/__init__.py:1` 的再导出把"导入一个 ORM 类"放大成"导入整套工具及其 Service 依赖"：

```python
# tools/__init__.py（全文 3 行）
from tools.registry import ToolRegistry, build_tool_registry
__all__ = ["ToolRegistry", "build_tool_registry"]
```

全仓 grep 确认**没有任何模块消费这个再导出**：`from tools import` 只出现在
`api/router.py:15` 的 `from tools import api as tools_api`（子模块导入，不依赖 `__all__`）；
`ToolRegistry`/`build_tool_registry` 的四处消费者全部直接 `from tools.registry import`
（`tools/service.py:16`、`runtime/subagent.py:36`、`runtime/agent.py:53`、
`tools/subagents/definition.py:23` 后者在 `TYPE_CHECKING` 下）。

模拟删掉 `tools -> tools.registry` 这一条边后重跑 SCC：跨包环数 = 0（同包内 `llm`、`plan`、
`prompt` 的 `__init__` 再导出形成的"自环"不构成加载期问题，Python 对包内子模块回引父包时
父包已在 `sys.modules`，实测三者作为首个导入均通过）。

### F2 环上其余各边都是"运行时构造 Service 实例"，不可改为 TYPE_CHECKING

| 边 | 符号 | 用途 | 位置 |
|---|---|---|---|
| `session.service -> session.repository` | `SessionRepository` | `__init__` 构造 | `session/service.py:9,17` |
| `task.service -> session.service` | `SessionService` | `__init__` 构造 + 6 处 `get_required`/`require_read_access` | `task/service.py:6,22,39,49,61,89,126,137` |
| `team.service -> session.service` | `SessionService` | `__init__` 构造 + 15 处校验调用 | `team/service.py:5,35,39-244` |
| `team.service -> task.service` | `TaskService` | `__init__` 构造，跨域认领转发 | `team/service.py:7,34,267,271` |
| `tools.builtin.task -> task.service` | `TaskService` | 4 个工具 `run()` 内 `TaskService(ctx.db, bus=ctx.bus)` | `tools/builtin/task.py:7,53,78,125,153` |
| `tools.builtin.team -> team.service` | `TeamService` | 6 个工具 `run()` 内 `TeamService(ctx.db)` | `tools/builtin/team.py:18,53-223` |
| `session.repository -> tools.models` | `ToolCallRecord` | 软删除级联表元组 `_SESSION_CHILD_MODELS` | `session/repository.py:20,28,182-187` |

这些边都符合分层规约（Service 调 Service、Repository 引 ORM），方向本身没有反。
"反"的只有 `tools/__init__.py` 这一条隐式边：它让 ORM 层的 `tools.models` 在加载时
连带拉起 Service 层，等价于"ORM 依赖 Service"。

### F3 全仓函数内局部导入共 19 处，其中 8 处改顶层会成环、11 处只是"顺手写的"

对每一处局部导入，把它假想成顶层边后在加载期图上找回路：

| 位置 | 目标符号 | 现状改顶层是否成环 | 解环后是否成环 |
|---|---|---|---|
| `session/service.py:89,146,223` | `DepartmentService` | 否 | 否 |
| `session/service.py:90,156,224` | `GroupService` | 否 | 否 |
| `session/service.py:345,367` | `AgentService` | 是 | **仍成环**（`agent.service -> tools.service -> tools.registry -> tools.builtin.task -> task.service -> session.service`） |
| `session/service.py:349,371` | `WorkflowService` | 是 | **仍成环**（经 `workflow.service -> agent.service -> ...` 同上） |
| `auth/service.py:58,102` | `WorkspaceService` | 否 | 否（且 `auth/service.py:12` 已有同名顶层导入，局部导入纯冗余） |
| `runtime/agent.py:1805-1807` | `AsyncSessionLocal`、`SubAgentRunner`、`TeamService` | 否 | 否（`runtime.workflow:36,40` 已顶层导入前两者，`runtime.agent` 加载时 `database.engine`、`team.service` 实测已在 `sys.modules`） |
| `skill/service.py:420` | `AgentService` | 是 | **仍成环**（`agent.service:10 -> skill.service`，真实双向依赖） |
| `workspace/service.py:306` | `GroupService` | 是 | **仍成环**（`group.service:11 -> workspace.service`，真实双向依赖） |
| `tools/builtin/agent.py:128` | `AsyncSessionLocal` | 是 | 否（但与 `:129` 同一处，一并保留） |
| `tools/builtin/agent.py:129` | `SubAgentRunner` | 是 | **仍成环**（`runtime.subagent:36 -> tools.registry -> tools.builtin.agent`，工具反向依赖运行时，属方案 B 范畴） |

测试侧绕行点 2 处：`tests/test_events_actor.py:202`（函数内局部导入）、
`tests/test_neutral_context.py:19`（`import tools  # noqa: F401` 预热）。
`tests/test_logging_chain.py:116-154`、`tests/test_llm_thinking.py:383`、
`tests/test_workflow_orchestration.py:1286` 的局部导入与本环无关，不动。

### F4 副本实验：推荐方案已在 /tmp 拷贝上验证通过

在源码副本（不含 `.git`/`.venv`）上执行推荐方案后：
168 模块子进程探测 0 失败；静态 SCC = 0；`pytest -q` 176 passed（与主仓库基线 176 passed 一致）；
把原型回归测试放回未修复的主仓库运行，9 failed / 7 passed，8 个危险模块逐个命中，
证明测试确实能捕获当前缺陷。相关改动行差异合计约 43 行、7 个文件。

### F5 提升 `session/service.py` 局部导入会暴露两处 `SIM102`

`pyproject.toml:34` 只设 `line-length`/`target-version`，但本机 ruff 0.16.6 默认规则集含
`SIM102`（`--show-settings` 可见）。`session/service.py:144-159` 两个 `if a: if await X(): return True`
在局部导入删除后变成可折叠的嵌套 `if`，ruff 会报错（主仓库现状因中间夹着 import 语句而不触发）。
需顺手折叠成 `if a and await X(): return True`，语义不变。

### F6 测试 monkeypatch 目标依赖"局部导入从源模块取名"

`tests/test_logging_chain.py:107-109` 对 `team.service.TeamService`、`database.engine.AsyncSessionLocal`、
`runtime.subagent.SubAgentRunner` 打桩；若把 `runtime/agent.py:1805-1807` 提升为顶层导入，
名字会在模块加载时绑定到 `runtime.agent` 命名空间，这三处 patch 目标必须同步改成
`runtime.agent.TeamService` 等（副本已验证改后 176 passed）。
`tests/test_workflow_orchestration.py:965-966` 对 `workflow.service.WorkflowService`、
`agent.service.AgentService` 的 patch 依赖 `session/service.py:345-371` 保持局部导入，
这四处本来就属"仍成环、保留"项，不受影响。

## 需求

### R1 解环
删除 `tools/__init__.py` 对 `tools.registry` 的再导出，让 `tools` 包入口不再触发任何工具或
Service 的加载；包 docstring 说明"刻意留空"的原因，防止后人再加回去。

### R2 回归测试
新增 `tests/test_import_isolation.py`，两层防线：
- 静态：AST 解析全部一方模块的模块级 import 做 SCC，断言跨模块环数为 0（毫秒级，每次都跑）；
- 动态：对一组哨兵模块逐个起子进程 `python -c "import X"`，断言 returncode == 0，
  并有一个全量遍历 168 模块的并行兜底用例。

### R3 清理绕行点
- `tests/test_events_actor.py:202` 恢复为顶层导入，删除 `:199-200` 的解释注释；
- `tests/test_neutral_context.py:14-19` 删除预热 `import tools` 及注释；
- `session/service.py:89-90,146,156,223-224` 六处 `DepartmentService`/`GroupService` 提升为顶层；
- `auth/service.py:58,102` 两处冗余局部导入删除（顶层已有）；
- `runtime/agent.py:1805-1807` 三处提升为顶层，`tests/test_logging_chain.py:107-109` patch 目标同步。

### R4 保留项登记
`skill/service.py:420`、`workspace/service.py:306`、`session/service.py:345/349/367/371`、
`tools/builtin/agent.py:128-129` 共 8 处保留，每处加一行简体中文注释说明成环路径；
本任务不做依赖倒置。

## 验收标准

- [ ] AC1 168 个一方模块中任意一个作为进程首个导入都不报 ImportError：
  `uv run pytest -q tests/test_import_isolation.py` 全部通过；
  在修复前的代码上同一测试必须失败（原型已验证 9 failed）。
- [ ] AC2 静态 SCC 用例断言跨模块加载期环数为 0，并把 `llm`/`plan`/`prompt` 的包内自环
  显式排除在"环"的定义外（子模块回引祖先包不算边）。
- [ ] AC3 分层规约不破坏：改动不新增任何"跨域调 Repository"、不新增按层目录，
  `directory-structure.md:22-25` 与 `quality-guidelines.md:50-56` 的规则逐条对照通过；
  `tools/__init__.py` 留空与 `directory-structure.md:155`"包导出 Service 和主要 Schema"的
  描述冲突之处在该 spec 中补一句例外说明（tools 是基础设施包，非 feature 域）。
- [ ] AC4 函数内局部导入计数：源码 19 处降到 8 处，测试侧 2 处绕行点归零；
  保留的 8 处逐一列在 design.md 处置表里并有注释说明成环路径。
- [ ] AC5 `tests/test_events_actor.py` 顶层 `from runtime.subagent import SubAgentRunner`，
  `tests/test_neutral_context.py` 无 `import tools` 预热，两文件单独运行通过。
- [ ] AC6 `uv run pytest -q` 全量通过（基线 176 passed，新增用例后 ≥ 192）；
  `uv run --with ruff ruff check <改动文件>` 无报错（含 F5 的 SIM102）。
- [ ] AC7 无静默兜底：回归测试失败信息必须包含模块名与 stderr 末行，不吞异常。

## 风险

| 风险 | 影响 | 缓解 |
|---|---|---|
| 子进程探测慢：哨兵 15 模块串行约 14s，全量 168 并行（8 线程）约 19s | 全量测试时长从 6s 涨到 ~40s | 静态 SCC 用例秒级先跑；全量子进程用例可加 `@pytest.mark.slow` 由 CI 跑，本地 `-m "not slow"` 跳过（待定问题 Q1） |
| 有人误以为 `tools/__init__.py` 空文件是遗漏又加回再导出 | 环复活 | 包 docstring 说明 + 静态 SCC 用例即时报错 |
| 提升 `runtime/agent.py` 局部导入改变 patch 目标 | `test_logging_chain` 两个用例失败 | R3 已列同步项，副本验证通过 |
| 提升 `session/service.py` 局部导入触发 SIM102 | ruff 报错 | F5 已列折叠方案 |
| `runtime.agent` 顶层导入 `runtime.subagent` 后两者顺序耦合 | 无：`runtime.subagent:28` 只依赖 `runtime.stream_assembler`，不回引 `runtime.agent` | 静态 SCC 用例守护 |

## 待定问题

- Q1 全量 168 模块子进程用例是否默认跑？推荐：**默认跑**，但用 `ThreadPoolExecutor(8)` 并行，
  实测 19s；若用户嫌慢再加 `slow` 标记。理由：这条测试恰恰是"未来不再退化"的唯一硬保证，
  跳过会让 8 个哨兵之外的新模块失去覆盖。
- Q2 保留的 8 处"仍成环"局部导入是否要在本任务顺带做依赖倒置（方案 B）？推荐：**不做**，
  另开任务。理由：它们涉及 `agent ↔ skill`、`workspace ↔ group` 两对真实双向 Service 依赖和
  `tools.builtin.agent -> runtime.subagent` 工具反向依赖运行时，需要设计回调/协议，
  改动面与本任务"解加载期环"目标不同，混做会让回归范围失控。
