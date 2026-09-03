# Workflow 编排引擎 — 执行计划

依据 `prd.md`（需求/验收）与 `design.md`（技术方案）。按序执行，每步跑校验后再进下一步。

## 校验命令

```bash
uv run ruff check .          # line-length 100, target py314（pyproject.toml:34-36）
uv run ruff format --check .
uv run pytest -q             # testpaths=["tests"]（pyproject.toml:26-28）
uv run pytest tests/test_logging_chain.py tests/test_neutral_context.py -q   # 子代理链路回归
uv run python -c "from core.config import settings; print(settings.llm_provider)"  # Settings 可加载
uv run alembic upgrade head && uv run alembic downgrade -1 && uv run alembic upgrade head  # 迁移双向可跑
```

数据库连接直接用 `.env` 中已配置的值，不改地址、不自建服务。若外部服务连不上，
先报告具体错误与配置项名，不擅自安装或覆盖配置。

## 步骤

### S1 桥接 SubAgentSpec（先做，因为后续都依赖它）
1. `tools/subagents/definition.py`：`SubAgentSpec` 加 `display_name: str = ""`，
   加 `runtime_name` 只读属性（design §3.2），并把 `:129` 的
   `self.agent_type.value` 换成 `self.runtime_name`。
2. `runtime/subagent.py`：把 `:103`、`:303`、`:425`、`:427` 的
   `spec.agent_type.value` 换成 `spec.runtime_name`（`:110/:170/:182` 用的是
   已赋值的 `member_name`，随 `:103` 一并生效）。
3. 另有两处 `agent_type.value` **不要动**：`tools/subagents/registry.py:191`
   只列注册表里的内置 spec（workflow 的 spec 是即时构造、不入注册表），
   `tools/builtin/agent.py:90` 读的是工具入参枚举而非 spec，二者都与本改造无关。
4. **校验重点**：4 个内置 spec 不传 `display_name` 时行为必须逐字节不变。
   跑 `tests/test_logging_chain.py` 回归；补一条断言
   `get_subagent_spec(AgentType.EXPLORE).runtime_name == "Explore"`。

风险文件：`runtime/subagent.py`（团队路径与 Agent 工具共用）。回滚点：本步可独立还原。

### S2 agent 域补两个字段
`agent/service.py:32` 的 `AgentRuntimeConfig` 末尾加 `name: str` 与
`description: str | None`，在 `get_runtime_config()`（`:187`）中填充（design §3.4）。
关键字构造，不影响既有调用点。

### S3 workflow 域建模
1. 新建 `workflow/models.py`：`WorkflowMode` 枚举 + `WorkflowRecord` +
   `WorkflowAgentRecord`（design §2.1–2.3）。继承 `database.base.Base` 白得审计四列
   （`database/base.py:16-44`）；查询侧有全局软删过滤器（`database/engine.py:35-57`），
   Repository **不要**手写 `is_deleted == False`。
2. `database/registry.py` 按字母序加一行 `from workflow.models import ...  # noqa: F401`。
3. 新建 `alembic/versions/0022_create_workflows.py`，
   `down_revision = "0021_message_llm_attribution"`。照
   `alembic/versions/0019_add_agent_tool_catalog.py:19-106` 的惯例：本地
   `_audit_columns()` helper、每列与表都写中文 `comment=`、索引显式命名
   `ix_表_列`、partial unique 用 `postgresql_where`、FK 命名 `fk_...` 且带 ondelete、
   downgrade 逆序清理。同一迁移内加 `sessions.workflow_id`（design §2.4）。

### S4 workflow 域 CRUD（照抄 agent 域形状）
- `workflow/repository.py` ← `agent/repository.py:14-145`（`__init__(db)`、`select()`、
  add/flush/refresh、不自行 commit）。
- `workflow/schemas.py` ← `agent/schemas.py:37-115`（Request 带 `extra: forbid`，
  Response 带 `from_attributes`，分页 `items/total/limit/offset`）。
- `workflow/service.py` ← `agent/service.py:23-49`（frozen dataclass 分页 +
  `__init__(db)` 装配 repo 与协作 Service）。含 AC1 的重复绑定校验、
  AC6 的保存期「至少 1 个成员」校验。
- `workflow/api.py` ← `agent/api.py:17-54`（就地定义三个 `Annotated` Depends 别名、
  `ok(data, request)` 外壳）。**`/modes` 路由必须写在 `/{id}` 之前**（design §6）。
- `api/router.py` 加一行 include_router（照 `:20`）。

### S5 新增 workflow LLM 场景
`llm/factory.py:18` 的 `_KNOWN_SCENARIOS` 加 `"workflow"`；`core/config.py` 加
`anthropic_workflow_model` / `openai_workflow_model`（照 `:36-47` / `:58-67`）；
`.env` 与 `.env.example` 各加两个 `AGENTOS_*_WORKFLOW_MODEL=` 键。
`.env.example` 只写空占位，禁止真实密钥。四处缺一即启动失败
（`core/config.py:20` 的 `extra="forbid"`）。

### S6 编排运行时
新建 `runtime/workflow.py`：`WorkflowRuntime` + 三个编排器（design §4）。
- 主流程照 `runtime/agent.py:159-252`，commit 点与中文注释理由对齐 `:187-188` 与 `:819-820`。
- **Parallel 每个成员必须 `async with AsyncSessionLocal() as sub_db`**
  （照 `runtime/agent.py:1802-1815`），共享 `self.bus`。这是最容易写错的一处。
- 失败语义按 D5 逐模式实现，一律 `AgentException.message`；失败详情只进
  `logger.exception`，不拼进提示词、不透出响应。
- 编排器 `__init__` 接受 `llm: BaseLLMClient | None = None`，未注入时
  `create_llm_client(scenario="workflow")`。

### S7 接入发消息入口
`session/schemas.py:8` 加 `workflow_id`；`session/service.py:325-354` 的
`prepare_for_message` 加 design §5.2 的四条规则；`session/api.py:46` 加分发判断。

两个必须做对的点（design §5.2 / §5.3）：
- 409 与互斥校验要**同时**补在 `session/api.py:69` 与 `session/service.py:349`
  两处，仓库里「已有会话不能切换Agent」本就是双写，只补一处会让 API 先用错
  的分派把 Service 的校验绕过去。
- 分发判据是 `payload.workflow_id or session.workflow_id`，不是只看请求体。
  只看请求体会让第二轮起静默退回普通 Agent 路径（首轮正确、后续降级且不报错）。

**完全不涉及 workflow 的请求不得进入任何新分支**（AC8）。

### S8 测试
`tests/test_workflow_orchestration.py`，照 `tests/test_logging_chain.py:56-109` 的
Fake/monkeypatch 风格（本仓库无数据库 fixture，`tests/conftest.py:11-14` 只有
`anyio_backend`；异步用例打 `pytestmark = pytest.mark.anyio`）：
- AC3 Sequential 输入形状：断言第 2 个成员收到的 prompt 同时含原始提问与上一步输出。
- AC4 Parallel：monkeypatch `runtime.workflow.SubAgentRunner`，断言并发且汇总
  LLM 替身被调用、入参含全部成员结果。
- AC5 Routing：断言只有被选中成员的执行器被调用（调用次数断言）。
- AC6/AC7：成员数不足、成员停用各抛中文异常。
- AC8 两条：①首轮带 workflow_id 走编排；②第二轮只带 session_id 仍走编排
  （防静默降级）；③纯 Agent 请求路径无变化。
- AC10：三种模式的失败语义各一例。
- AC12：`Settings.model_fields` 含两个 workflow 场景字段；
  `_KNOWN_SCENARIOS` 含 `"workflow"`。

## 提交前检查

- [ ] 三处配置同步（`.env` / `.env.example` / `Settings`），`.env.example` 无真实值
- [ ] `database/registry.py` 已登记，`alembic upgrade/downgrade` 双向跑通
- [ ] 内置 4 个 SubAgent 的 `runtime_name` 与改造前一致（回归未破）
- [ ] 不带 `workflow_id` 的发消息路径无行为变化
- [ ] workflow 会话的第二轮（不带 workflow_id）仍走编排，未静默降级
- [ ] 全量 `uv run pytest -q` 通过，如实报告命令与结果
- [ ] 不自动 `git push`
