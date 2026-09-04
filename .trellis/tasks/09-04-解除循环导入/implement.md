# 解除循环导入 — 执行计划

> 行号以 `feature/unify-llm-client` HEAD `d25d600` 为准；动手前先 `git diff --stat` 确认工作区干净。

## 校验命令

```bash
# 复现（改动前应报 ImportError，改动后应静默通过）
uv run python -c "import runtime.subagent"
uv run python -c "import session.service"
uv run python -c "import memory.jobs.extraction"

# 新回归测试（S4 之后）：静态 SCC + 15 哨兵 + 168 全量
uv run pytest -q tests/test_import_isolation.py
# 只跑静态 SCC（秒级，改一处跑一次）
uv run pytest -q tests/test_import_isolation.py -k cycles

# 绕行点恢复后单独跑（首个导入即危险模块）
uv run pytest -q tests/test_events_actor.py
uv run pytest -q tests/test_neutral_context.py
uv run pytest -q tests/test_logging_chain.py

# 全量（基线 176 passed；完成后 ≥ 192）
uv run pytest -q

# ruff（含 SIM102）
uv run --with ruff ruff check tools/__init__.py session/service.py auth/service.py \
    runtime/agent.py skill/service.py workspace/service.py tools/builtin/agent.py \
    tests/test_import_isolation.py tests/test_events_actor.py \
    tests/test_neutral_context.py tests/test_logging_chain.py

# 局部导入计数（源码目标 8，测试目标 0）
grep -rn "^\s\+from [a-z_.]\+ import" --include="*.py" \
    session task team tools runtime agent workflow memory permission hooks \
    interaction plan skill prompt workspace auth database core llm api | wc -l
grep -n "^\s\+from runtime\|^import tools" tests/test_events_actor.py tests/test_neutral_context.py

# 任务文档校验
python3 .trellis/scripts/task.py validate 09-04-解除循环导入
```

## 步骤

### S1 先写回归测试，确认它在当前代码上失败

新建 `tests/test_import_isolation.py`（形状见 design.md §5）。运行：

```bash
uv run pytest -q tests/test_import_isolation.py
```

预期：静态 SCC 用例报 1 个分量（9 个模块）；哨兵用例 8 failed / 7 passed；全量用例 failed。
若此时全绿说明测试写错了，停下来修测试。

### S2 解环：`tools/__init__.py`

把 `:1-3` 整体替换为 design.md §4.1 的 docstring，不留任何 import 与 `__all__`。
运行 `uv run python -c "import runtime.subagent"` 等三条复现命令，应全部静默；
`uv run pytest -q tests/test_import_isolation.py` 应全绿。

### S3 恢复测试侧绕行点

1. `tests/test_events_actor.py`：`:202` 局部导入上移到顶层 import 块（放在 `:24`
   `from tools.subagents.definition` 之前，满足 I001 排序）；删除 `:199-200` 两行注释，
   `_stub_runner` docstring 只留 `:197` 第一句。
2. `tests/test_neutral_context.py`：删除 `:14-19`（4 行注释 + `import tools  # noqa: F401`）。
3. 单独运行两个文件确认通过（此时它们的首个项目导入分别是 `core.events` / `llm.types`，
   但 `runtime.subagent`、`memory.jobs.extraction` 已不再依赖导入顺序）。

### S4 提升可安全提升的局部导入

1. `session/service.py`：`:7` 后加 `from department.service import DepartmentService`、
   `from group.service import GroupService`；删除 `:89-90`、`:146`、`:156`、`:223-224` 六处局部导入；
   折叠 `:144-149`、`:152-158` 两个嵌套 `if` 为 `if a and await ...:`（design.md §4.2）。
2. `auth/service.py`：删除 `:58`、`:102` 两处冗余局部导入（`:12` 顶层已有）。
3. `runtime/agent.py`：把 `:1805-1807` 三行移到顶层 import 块对应分组
   （`database.engine` 在 `core.*` 之后、`runtime.subagent` 在 `runtime.stream_assembler` 附近、
   `team.service` 在 `task.service` 之后），删除函数内三行。
4. `tests/test_logging_chain.py:107-109`：三个 `monkeypatch.setattr` 目标改为
   `"runtime.agent.TeamService"`、`"runtime.agent.AsyncSessionLocal"`、`"runtime.agent.SubAgentRunner"`。
5. 运行 `uv run pytest -q tests/test_logging_chain.py` 与 ruff，确认 SIM102 与 I001 都干净。

### S5 保留项加注释

对 `skill/service.py:420`、`workspace/service.py:306`、`session/service.py:345/349/367/371`、
`tools/builtin/agent.py:128-129` 各加一行简体中文注释，格式统一为：
`# 局部导入：顶层会成环 <A> -> <B> -> ...，待依赖倒置任务处理。`
路径取 design.md §4.2 表中对应行。注意 `session/service.py` 的行号在 S4 删行后会前移约 8 行，
以搜索 `from agent.service import AgentService` 定位。

### S6 spec 回写

`.trellis/spec/backend/directory-structure.md`：
- `:155`"包导出"条目后补基础设施包不做再导出的例外说明；
- `:324-347` tools 段的目录树加 `__init__.py  # 刻意留空，见文件 docstring`。

### S7 全量验证

按"校验命令"块依次执行；如实记录每条命令的通过/失败数。
局部导入计数：源码 grep 结果应为 8（S5 保留项），测试侧两条 grep 应为空。

## 提交前检查

- [ ] `uv run pytest -q` 全绿，数量 ≥ 192，并贴出实际数字。
- [ ] `tests/test_import_isolation.py` 三个用例全绿；临时把 `tools/__init__.py` 加回
      `from tools.registry import ToolRegistry` 再跑一次静态 SCC 用例，必须变红，然后还原。
- [ ] ruff 对校验命令块列出的全部文件无报错。
- [ ] `git diff --stat` 只含：`tools/__init__.py`、`session/service.py`、`auth/service.py`、
      `runtime/agent.py`、`skill/service.py`、`workspace/service.py`、`tools/builtin/agent.py`、
      4 个测试文件、1 个 spec 文件、本任务目录；无 `.env`、无 Alembic。
- [ ] 提交信息简体中文，形如 `refactor: 删除 tools 包再导出解除加载期循环导入并补子进程回归测试`。
- [ ] 不执行 `git push`。
