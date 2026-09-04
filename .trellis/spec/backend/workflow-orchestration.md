# Workflow Orchestration Guidelines

> AgentOS Workflow 编排引擎规范：数据模型、运行入口分发判据、三种模式的失败语义、
> SubAgentSpec 桥接、`workflow_progress` 进度事件契约与新增 `workflow` LLM 场景

---

## 1. Scope / Trigger

**适用范围**：

- 新增或修改 `workflow/` 包（`models.py` / `schemas.py` / `repository.py` / `service.py` / `api.py`）
- 修改 `runtime/workflow.py`（`WorkflowRuntime`、三个编排器、成员标识与提示词模板）
- 修改 `POST /api/sessions/messages` 的分发逻辑或 `SessionService.prepare_for_message`
- 修改 `SubAgentSpec.display_name` / `agent_id` / `runtime_name`，或 `AgentRuntimeConfig` 的 `name` / `description`
- 修改 `core/events.py` 的 `Actor.agent_id`、`WorkflowProgressInfo` 或 `EventType.WORKFLOW_PROGRESS`（前端工作流图契约）
- 新增编排模式、调整 `workflow` LLM 场景配置

**强制触发条件**（本次全部命中）：`/api/workflows` 六个路由与 `send_message` 新增
`workflow_id` 字段（新 API）；迁移 `0022_create_workflows`（两张新表 + 改 `sessions`）；
API → SessionService → WorkflowRuntime → SubAgentRunner 的跨层契约。

**关键文件**：

- `workflow/models.py` — `WorkflowMode` 枚举、`WorkflowRecord` / `WorkflowAgentRecord`
- `workflow/service.py` — 配置态入口；`require_runnable()` / `get_run_members()` 供 session 与 runtime 复用
- `workflow/api.py` — `/api/workflows` 路由（`/modes` 必须先于 `/{workflow_id}` 声明）
- `runtime/workflow.py` — 执行态：`WorkflowRuntime`、`SequentialOrchestrator` /
  `ParallelOrchestrator` / `LlmRoutingOrchestrator`、`build_member_display_name()`、提示词模板、
  `_emit_progress()` / `_run_member_with_progress()` 进度事件 helper
- `core/events.py` — `EventType.WORKFLOW_PROGRESS`、`WorkflowPhase` / `WorkflowProgressStatus` /
  `WorkflowProgressInfo`、`Actor.agent_id`、`subagent_actor(agent_id=)`
- `session/api.py` `send_message`；`session/service.py` `prepare_for_message()` /
  `resolve_effective_workflow_id()`；`session/schemas.py` `SessionSendMessageRequest`
- `tools/subagents/definition.py` — `SubAgentSpec.display_name` / `agent_id` / `runtime_name`
- `runtime/subagent.py` — `SubAgentRunner.run()` 把 `spec.agent_id` 透传到运行记录与 actor
- `team/models.py` `SubAgentRunRecord.agent_id`；`team/subagent_run_repository.py` `create(agent_id=)`
- `agent/service.py` — `AgentRuntimeConfig.name` / `.description`、`require_bindable_agent_ids()`
- `alembic/versions/0022_create_workflows.py`、`alembic/versions/0023_subagent_run_agent_id.py`
- 设计背景：`.trellis/tasks/09-02-工作流编排引擎/design.md`（§3 桥接、§4 执行流、§5 入口、
  §9 取舍）与 `prd.md`（D1–D5）；进度事件与 `agent_id`：
  `.trellis/tasks/09-04-workflow进度事件/design.md`（§1 契约、§4 发事件点、§8 取舍）与 `prd.md`（D1–D5）

---

## 2. Signatures

### 2.1 API

| 方法 | 路径 | 请求 → 响应 | 说明 |
|---|---|---|---|
| POST | `/api/workflows` | `WorkflowCreateRequest` → `ApiResponse[WorkflowResponse]` | 需 owner / admin |
| GET | `/api/workflows` | query `keyword` / `is_enabled` / `limit(1..100)` / `offset` → `ApiResponse[WorkflowListResponse]` | 活跃成员可读 |
| GET | `/api/workflows/modes` | → `ApiResponse[list[WorkflowModeInfo]]` | 静态元数据不查库；**必须声明在 `/{workflow_id}` 之前**，否则 `modes` 被当成路径参数 |
| GET | `/api/workflows/{workflow_id}` | → `ApiResponse[WorkflowResponse]` | |
| PATCH | `/api/workflows/{workflow_id}` | `WorkflowUpdateRequest` → `ApiResponse[WorkflowResponse]` | `agents` 提供时整体替换成员集 |
| DELETE | `/api/workflows/{workflow_id}` | → `ApiResponse[WorkflowDeleteResult]` | 软删除，同事务软删成员绑定 |
| POST | `/api/sessions/messages` | `SessionSendMessageRequest`（新增 `workflow_id`）→ SSE | **唯一运行入口**（D1），没有独立的 `/workflows/{id}/run` |

### 2.2 Service / Runtime（`workflow/service.py`、`runtime/workflow.py`）

```python
class WorkflowService:
    async def require_runnable(self, workflow_id: int, workspace_id: int) -> WorkflowRecord
    """存在且 is_enabled；不存在 404、停用 409。session 域与 runtime 共用。"""
    async def get_run_members(self, workflow_id: int, workspace_id: int) -> tuple[WorkflowRunMember, ...]
    """内部先 require_runnable；成员按 (sort_order, agent_id) 升序，不校验成员 Agent 自身。"""
    @staticmethod
    def list_modes() -> tuple[WorkflowModeMeta, ...]

class SessionService:
    async def prepare_for_message(self, session_id, content, user_id, workspace_id,
                                  agent_id: int | None = None, workflow_id: int | None = None) -> SessionRecord
    @staticmethod
    def resolve_effective_workflow_id(requested_workflow_id: int | None,
                                      session: SessionRecord | None) -> int | None

class WorkflowRuntime:
    def __init__(self, db: AsyncSession, user_id: int | None = None,
                 workspace_id: int | None = None, llm: BaseLLMClient | None = None)
    def run(self, session_id: int | None, user_content: str, *, workflow_id: int) -> AsyncIterator[dict[str, Any]]

class BaseOrchestrator:
    def __init__(self, *, db, bus, user_id, workspace_id, session_id, turn_id, workflow_id, members)
    async def run(self, user_content: str) -> OrchestrationResult   # 子类实现
    async def run_member(self, db, agent_id: int, prompt: str, *,
                         ordinal: int | None = None, total: int | None = None) -> tuple[AgentRuntimeConfig, str]
    async def _emit_progress(self, phase: WorkflowPhase, status: WorkflowProgressStatus, *,
                             config: AgentRuntimeConfig | None = None,
                             ordinal: int | None = None, total: int | None = None) -> None
    """以 ORCHESTRATOR_ACTOR 发 workflow_progress;member 阶段从 config 取 agent_id 与 Agent 原名。"""
    async def _run_member_with_progress(self, db, config, prompt: str, *,
                                        ordinal: int | None = None, total: int | None = None) -> str
    """running → _execute_member → done / failed;CancelledError 不发 failed。三个编排器统一走它,不各自手写。"""

# Parallel / LlmRouting 额外接受 llm: BaseLLMClient | None，未注入回落 create_llm_client(scenario="workflow")
ORCHESTRATOR_CLASSES: dict[WorkflowMode, type[BaseOrchestrator]]   # 新模式必须在此登记

MEMBER_DISPLAY_NAME_PREFIX = "wf-"
MEMBER_DISPLAY_NAME_MAX_LENGTH = 120
def build_member_display_name(agent_name: str) -> str   # f"wf-{name}"[:120]
```

`OrchestrationResult(text, provider=None, model=None)`：只有编排层自己调 LLM 的产出
（并行汇总）才带归因；Sequential / Routing 的最终文本来自成员，`provider` / `model` 为空。

### 2.3 桥接签名（`tools/subagents/definition.py`、`agent/service.py`、`core/events.py`、`team/`）

```python
@dataclass(frozen=True)
class SubAgentSpec:
    agent_type: AgentType            # workflow 成员固定 GENERAL_PURPOSE：只是能力档位，不是标识
    display_name: str = ""           # 动态 spec 的运行标识；内置 spec 留空
    agent_id: int | None = None      # 数据库 Agent 主键；workflow 成员传 config.agent_id，内置 spec 留空
    @property
    def runtime_name(self) -> str:   # display_name or agent_type.value
        """落运行记录、发事件、判权限、打日志、TMP 目录名统一用它。"""

@dataclass(frozen=True)
class AgentRuntimeConfig:
    ...; agent_id: int; name: str; description: str | None   # name → display_name；description → 路由依据

# core/events.py
@dataclass(frozen=True)
class Actor:
    ...; agent_id: int | None = None   # to_dict_full 与 to_dict_compact 都在非 None 时输出该键
def subagent_actor(agent_type: str, run_id: str, task: str | None = None, *,
                   agent_id: int | None = None) -> Actor

class WorkflowPhase(str, Enum): MEMBER / ROUTING / MERGING
class WorkflowProgressStatus(str, Enum): RUNNING / DONE / FAILED
@dataclass(frozen=True)
class WorkflowProgressInfo:
    phase: WorkflowPhase; status: WorkflowProgressStatus
    agent_id: int | None = None; member_name: str | None = None
    ordinal: int | None = None; total: int | None = None
    def to_dict(self) -> dict[str, Any]   # phase/status 必出，其余非 None 才出
StreamEvent.workflow_progress(actor, session_id, progress: WorkflowProgressInfo) -> StreamEvent
StreamEvent.progress: WorkflowProgressInfo | None   # to_dict() 输出为 "progress" 键

# team/subagent_run_repository.py
class SubAgentRunRepository:
    async def create(self, session_id, member_name, prompt, *, agent_id: int | None = None) -> SubAgentRunRecord
```

`SubAgentRunner.run()`（`runtime/subagent.py`）把 `spec.agent_id` 同时透传给
`run_repo.create(agent_id=)` 与 `subagent_actor(agent_id=)`，因此 workflow 成员的全部事件
（`turn_start` / `block_*` / `text_delta` / `tool_*` / `error` / `turn_end`）的 `actor` 都带 `agent_id`。

### 2.4 DB（`alembic/versions/0022_create_workflows.py`、`0023_subagent_run_agent_id.py`）

```python
revision = "0022_create_workflows"
down_revision = "0021_message_llm_attribution"

revision = "0023_subagent_run_agent_id"
down_revision = "0022_create_workflows"
```

| 表 / 列 | 约束 | 说明 |
|---|---|---|
| `workflows.workspace_id` | FK `workspaces.id` **CASCADE** | 工作区隔离 |
| `workflows.name` | `String(128)`；部分唯一索引 `uq_workflows_active_workspace_name (workspace_id, name) WHERE is_deleted = false` | 软删后同名可重建 |
| `workflows.mode` | `String(32)`，取值 `sequential` / `parallel` / `llm_routing` | 由 `WorkflowMode(str, Enum)` 约束，库里存字符串 |
| `workflows.is_enabled` | `Boolean` 默认 true；复合索引 `ix_workflows_workspace_enabled` | 停用后 `require_runnable` 409 |
| `workflows.created_by_user_id` | FK `users.id` **RESTRICT** | 与 `agents` 一致 |
| `workflow_agents.workflow_id` | FK `workflows.id` **CASCADE** | 随 workflow 级联 |
| `workflow_agents.agent_id` | FK `agents.id` **RESTRICT** | 只拦物理删除；Agent 走软删，故运行期仍需逐成员 `require_runnable` |
| `workflow_agents.sort_order` | `Integer` 默认 0 | 执行/展示序；**无 role 列**（D3：角色差异全在编排器） |
| `workflow_agents` | `UniqueConstraint(workflow_id, agent_id)` `uq_workflow_agents_workflow_agent` | `replace_members()` 复活软删行而非新插 |
| `sessions.workflow_id` | FK `workflows.id` **SET NULL**，nullable，index | 会话锁定的 Workflow；workflow 会话的 `agent_id` 留空 |
| `subagent_runs.agent_id`（0023） | FK `agents.id` **SET NULL** `fk_subagent_runs_agent_id_agents`，nullable，索引 `ix_subagent_runs_agent_id` | 仅 workflow 成员填成员 Agent 主键；内置子代理与 team 路径 NULL；存量行不回填 |

两张新表均继承 `Base` 审计 / 软删字段，并已登记 `database/registry.py`；0023 只加列不加实体，registry 不改。

---

## 3. Contracts

### 3.1 `POST /api/sessions/messages` 的分发契约（最易踩）

```python
class SessionSendMessageRequest(BaseModel):
    session_id: int | None
    agent_id: int | None      # 与 workflow_id 互斥
    workflow_id: int | None   # 与 agent_id 互斥
    content: str
```

| 情形 | API 层（`session/api.py`） | Service 层（`prepare_for_message`） |
|---|---|---|
| `agent_id` 与 `workflow_id` 同时有值 | 400「同一次请求不能同时指定Agent和Workflow」 | 同文案再校验一次 |
| 新建会话 + `workflow_id` | **前置** `WorkflowService.require_runnable()` | 再 `require_runnable()` 后建会话并写 `sessions.workflow_id` |
| 已有会话 + `workflow_id != session.workflow_id` | 409「已有会话不能切换Workflow」 | 同文案 409 |
| 已有会话且 `session.workflow_id` 非空、请求不带 `workflow_id` | 不前置复检 | 每轮复检 `require_runnable(session.workflow_id)` |
| 不涉及 workflow | 不进入任何新分支 | 同 |

三条硬约束：

1. **分发判据是 `payload.workflow_id or session.workflow_id`**（`resolve_effective_workflow_id()`），
   不是只看请求体。会话锁定后前端不再带 `workflow_id`，只看请求体会让第二轮**静默**退回
   `AgentRuntime` 且不报错（D2）。见第 7 段第一对。
2. **409 在 API 与 Service 双写是刻意的**，与「已有会话不能切换Agent」同构：API 层在构造
   Runtime **之前**就已拿到 session 行；若只写 Service，API 会先按错误的 `workflow_id`
   分派出去，Service 的 409 永远轮不到执行（此时已进入 SSE 流）。
3. **新建会话前置 `require_runnable`** 让 404 / 409 走普通 JSON 错误外壳；否则错误发生在流式
   生产者内部，只剩服务端日志与一条空 SSE 流。Service 内的复检会再读一次，属有意重复。

### 3.2 三种模式的执行与失败语义（D4 / D5）

| 模式 | 最小成员 | 成员输入 | 是否调 LLM | 失败语义 |
|---|---|---|---|---|
| `sequential` | 1（保存期） | 第 1 个只收原始提问；第 N≥2 个收 `SEQUENTIAL_HANDOFF_PROMPT_TEMPLATE`（原始提问 + **仅上一步**输出，不累积） | 否，不建客户端 | **任一成员失败即中止**，后续不执行；错误前缀「顺序编排已中止：」 |
| `parallel` | 2（运行期） | 全部成员收同一原始提问 | 是：汇总走 `llm.stream()` + `TurnEventAssembler` 逐字推送 | **配置期**（成员停用 / 软删 / 不存在）任一失败即整体失败、此时零成员派发；**运行期**部分容忍，只汇总成功者；**全部失败**抛「并行编排的全部成员均执行失败」 |
| `llm_routing` | 2（运行期） | 被选中成员收原始提问 | 是：`complete_structured(_RoutingChoice)` | 决策失败、选出 `agent_id` 不在成员表 → 整体失败，**不回落第一个成员** |

- 成员执行统一经 `SubAgentRunner.run(...)`，成员的工具 / Skill / 模型来自 `AgentRuntimeConfig`；
  成员过程事件由 `SubAgentRunner` 自发（`actor` 带 `agent_id`），编排者（`ORCHESTRATOR_ACTOR`）发
  `turn_start` / `turn_end`、承载最终回答的**一个**文本块（index 0），以及 `workflow_progress`
  进度事件（节点 running / done / failed 与 routing / merging 阶段，契约见 3.8）。
- `SubAgentRunner` 抛出的任何异常被包成「Workflow成员执行失败」，details 只含
  `workflow_id` / `member_name`，堆栈只进 `logger.exception`，不透出到响应或汇总提示词。
  进度事件不改变任何失败语义：`failed` 发出后仍按上表抛错。

### 3.3 Parallel 的 Session 隔离（硬约束）

`AsyncSession` 非并发安全，而 `SubAgentRunner` 持有 `db`。`ParallelOrchestrator` 中：

- 配置期用主 `db` **串行**加载全部成员配置（不可运行者在派发前抛错）；
- 每个成员协程 `async with AsyncSessionLocal() as sub_db` 开**独立 Session**；
- 成员之间**只共享 `self.bus`**，让各成员增量流到同一条 SSE。

### 3.4 SubAgentSpec 桥接契约

| 项 | 契约 |
|---|---|
| 内置 spec | 不传 `display_name`，`runtime_name == agent_type.value`，事件 / 运行记录 / 权限 / 日志逐字节不变 |
| workflow 成员 | `display_name = build_member_display_name(config.name)` = `"wf-" + name` 截断到 **120**（`subagent_runs.member_name` 为 `String(120)`，`agents.name` 允许 128） |
| `agent_type=GENERAL_PURPOSE` | 仅能力档位；不得让成员上报 `general-purpose` 作标识，否则用户为该类型配的权限规则连带作用到全部成员且事件同名无法区分 |
| 权限 | `BUILTIN_AGENT_DEFAULTS` 只有 `("verification", "Bash"): "allow"` 一条；`wf-*` 命不中，落通用默认（危险工具 ask、其余 allow），**无权限提升** |
| 递归 | `GLOBAL_SUBAGENT_DENIED_TOOLS` 已禁 Agent / TeamCreate / TeamSpawn，成员不能再派生子代理 |
| 未桥接 | `max_rounds`（6）与 `shell_access`（`READ_ONLY`）用 spec 默认，`agents` 表无对应列 |

### 3.5 `workflow` LLM 场景（四处同步）

| 位置 | 内容 |
|---|---|
| `llm/factory.py` `_KNOWN_SCENARIOS` | 加 `"workflow"`（现为 8 个场景） |
| `core/config.py` `Settings` | `anthropic_workflow_model: str | None = None`、`openai_workflow_model: str | None = None` |
| `.env` | `AGENTOS_ANTHROPIC_WORKFLOW_MODEL=` / `AGENTOS_OPENAI_WORKFLOW_MODEL=`（本地实际值） |
| `.env.example` | 同键，空占位 |

未配置时回落 `{provider}_model`；两级皆空由工厂抛中文异常。`Settings` 的 `extra="forbid"`
会把「.env 有键、Settings 无字段」在启动期判失败。注入点：`ParallelOrchestrator.__init__` /
`LlmRoutingOrchestrator.__init__`（`llm or create_llm_client(scenario="workflow")`）；
`SequentialOrchestrator` 不建客户端。

### 3.6 运行时生命周期与落库

`WorkflowRuntime._produce()` 与 `AgentRuntime` 同构：`prepare_for_message(workflow_id=...)`
→ `start_turn` → **commit**（用户消息与 Turn 先可靠落库）→ `session_ready` → 编排 →
`add_message("assistant", build_assistant_blocks(result.text, []), provider, model)` →
`_complete_turn`（turn completed、session idle、**commit**）。异常路径 `_mark_abnormal_end`
先 rollback，再尽力把 turn 标 `failed` / `interrupted`、session 标 `failed` 并 commit，
最后 emit `error`（code `WORKFLOW_RUN_FAILED`，非 `AgentException` 统一文案「Workflow执行失败。」）。
分步 commit 的原因是流式执行跨越请求依赖的 yield 生命周期，与 `AgentRuntime` 相同。

### 3.7 已接受取舍（09-02 design.md §9、09-04 design.md §8）

- 编排阶段以 `workflow_progress` 事件上报节点状态（09-04 推翻 09-02 的「不新增事件类型」），
  但**不发路由理由**（`_RoutingChoice` 只有 `agent_id`）、**不带成员输出正文**、**进度事件不落库**
  （不进 `session_messages` / `session_snapshots`，历史回放范围外）。
- **配置期失败不发 `member/failed`**：`failed` 只与已发出的 `running` 配对；成员停用 / 软删 /
  不存在发生在 `running` 之前，整体失败由 `WorkflowRuntime._produce` 的 `error` 事件承担。
- **Parallel 全败不发 merging 事件**：没有进入汇总阶段就不上报该阶段。
- **`asyncio.CancelledError` 不发任何 `failed`**：取消不是节点失败，直接重抛保持既有取消语义。
- `Actor.agent_id` 在 `to_dict_full` 与 `to_dict_compact` **都**输出：前端靠增量事件归节点。
  内置子代理 / teammate / 主循环的 `agent_id` 恒 None，序列化不出键，事件形状逐字节不变。
- `WorkflowRuntime` **不接** prompt hooks、任务快照（`TaskService.emit_snapshot`）、记忆入队 / 提取。
- D2 每轮重跑**成员不接收历史消息**，只收本轮 `user_content`（顺序模式外加上一步输出）。
- Sequential / Routing 落库的 assistant 消息 `provider` / `model` 为空（文本来自成员，
  成员归因在其自身运行记录）；仅 Parallel 汇总带归因。
- 编排器放 `runtime/` 而非 `workflow/service.py`：编排是执行态，`workflow/service.py` 只管配置态。

### 3.8 `workflow_progress` 进度事件契约（前端工作流图）

`type = "workflow_progress"`，归 `StreamEvent`，actor 固定为编排者精简形态
`{"role": "orchestrator", "name": "orchestrator"}`，payload 键 `progress`：

| phase | 含义 | 允许的 status | 必带字段 | 可选字段 |
|---|---|---|---|---|
| `member` | 单个成员节点 | running / done / failed | `agent_id`、`member_name`（Agent 原名，非 `wf-` 标识） | `ordinal`、`total`（仅 Sequential，1 起算） |
| `routing` | LlmRouting 决策 | running / done / failed | — | done 时带选中的 `agent_id`、`member_name` |
| `merging` | Parallel 汇总 | running / done / failed | — | — |

每个 (phase, agent_id) 一轮内 `running` 恰一次、终态至多一次。三种模式的事件序（一次成功运行）：

- Sequential：`member/running(1/N)` → 成员 1 自发事件 → `member/done(1/N)` → … → `member/done(N/N)`
  → 编排者文本块。成员失败：`member/failed(k/N)` 后抛「顺序编排已中止」，后续成员零事件。
- Parallel：派发前 `member/running × N`（前端一次性点亮全部节点）→ 各成员自发事件交错 →
  `member/done` / `member/failed` 交错（各协程自发，彼此顺序不定）→ `merging/running` →
  编排者汇总文本块 → `merging/done`。汇总 LLM 无文本：`merging/failed` 后抛错。
- LlmRouting：`routing/running` → `routing/done(agent_id=X, member_name)` → `member/running(X)` →
  成员 X 自发事件 → `member/done(X)` → 编排者文本块。决策失败 / 选出成员不在列表：`routing/failed`，零条 member。

示例：

```json
{"type": "workflow_progress", "session_id": 42, "sequence": 17,
 "actor": {"role": "orchestrator", "name": "orchestrator"},
 "progress": {"phase": "member", "status": "running",
              "agent_id": 2, "member_name": "审稿人", "ordinal": 2, "total": 3}}
```

语义分层：成员自发的 `error` 是成员自述，`member/failed` 是编排者视角的节点状态，两者可同时出现。
`StreamBus` 单队列 FIFO，成员 `done` 必然排在该成员 `turn_end` 之后。`format_sse` 无 type 白名单，
新事件直接透传；本仓库无前端，前端需对未知 `type` 容错。

---

## 4. Validation & Error Matrix

一律 `AgentException.message`（简体中文）；未标注状态码的为默认 400。改代码须同步本表。

| # | 条件 | 位置 | 错误消息（原文） | 状态码 |
|---|---|---|---|---|
| 1 | `agent_id` 与 `workflow_id` 同时有值 | `session/api.py`、`prepare_for_message` | `同一次请求不能同时指定Agent和Workflow` | 400 |
| 2 | 已有会话且 `workflow_id != session.workflow_id` | 同上双写 | `已有会话不能切换Workflow` | 409 |
| 3 | Workflow 不存在 / 不属本工作区 | `WorkflowService._get_required` | `Workflow不存在` | 404 |
| 4 | Workflow `is_enabled = false` | `require_runnable` | `Workflow已停用` | 409 |
| 5 | 创建 / 改名撞名（预检或 `IntegrityError`） | `_require_name_available` / `create_workflow` / `update_workflow` | `Workflow名称已存在` | 409 |
| 6 | 成员为空 | `_require_bindable_members` | `Workflow至少需要绑定1个成员Agent` | 400 |
| 7 | 成员重复 | 同上（Schema 层另有 `成员Agent重复绑定: [...]`） | `同一Agent不能在Workflow中重复绑定` | 400 |
| 8 | 成员 Agent 不存在 / 不属本工作区 | `AgentService.require_bindable_agent_ids` | `绑定的Agent不存在` | 404 |
| 9 | 非 owner / admin 写操作 | `WorkspaceService.require_workspace_role` | `无权执行该工作区操作` | 403 |
| 10 | Parallel / Routing 成员数 < 2 | `_require_min_members` | `并行编排至少需要2个成员Agent` / `智能路由编排至少需要2个成员Agent` | 400 |
| 11 | 成员 Agent 停用 / 软删 / 不存在 | `_load_member_config` | `Workflow成员不可运行：{agent 域原消息}`，details 含 `agent_id` | 透传（409 / 404） |
| 12 | Sequential 某成员失败 | `SequentialOrchestrator.run` | `顺序编排已中止：{原消息}` | 透传 |
| 13 | 成员执行异常（`SubAgentRunner`） | `_execute_member` | `Workflow成员执行失败` | 400 |
| 14 | Parallel 全部成员失败 | `ParallelOrchestrator.run` | `并行编排的全部成员均执行失败` | 400 |
| 15 | Parallel 汇总 LLM 无文本 | `_merge` | `并行编排汇总失败：LLM 未产出汇总文本` | 400 |
| 16 | Routing `complete_structured` 异常 | `_route` | `智能路由决策失败` | 400 |
| 17 | Routing 选出的 `agent_id` 不在成员表 | `_route` | `智能路由选出的Agent不在Workflow成员列表中` | 400 |
| 18 | `workflows.mode` 非法 / 未登记编排器 | `_build_orchestrator` | `Workflow编排模式无效` / `Workflow编排模式尚未实现` | 400 |
| 19 | `scenario="workflow"` 未登记 | `llm/factory.py` | `未知的 LLM 场景：workflow，可选值：…` | 400 |

---

## 5. Good/Base/Bad Cases

### Base：首轮带 `workflow_id` 新建会话

请求 `{"content": "...", "workflow_id": 7}` → API 前置 `require_runnable(7)` → 分发
`WorkflowRuntime.run(None, content, workflow_id=7)` → `prepare_for_message` 建会话并写
`sessions.workflow_id = 7`、`agent_id = NULL` → SSE：`session_ready` → `turn_start` →
（Sequential）`workflow_progress member/running(1/N)` → 成员 subagent 事件（`actor.agent_id` 可归节点）→
`workflow_progress member/done(1/N)` → … → 编排者文本块 → `turn_end`。

### Good：第二轮只带 `session_id`

请求 `{"session_id": 12, "content": "..."}`，会话 12 已锁定 workflow 7 →
`resolve_effective_workflow_id(None, session) == 7` → 仍走 `WorkflowRuntime`；
API 层不再前置复检，`prepare_for_message` 内复检 `require_runnable(7)`。

### Good：Parallel 部分失败

三成员中一个 `SubAgentRunner` 抛错 → 该成员 `logger.exception` 后发 `member/failed`，被
`gather(return_exceptions=True)` 收集并跳过 → 其余两成员 `member/done` → `merging/running` →
汇总提示词只含两个成功成员输出 → `merging/done` → 正常返回。

### Bad（详见第 7 段，此处只列症状）

- 只按 `payload.workflow_id` 分发：第二轮静默退回 `AgentRuntime`，输出变了才看得出来。
- 切换 Workflow 的 409 只写 Service：API 已按错误 `workflow_id` 进入 SSE，客户端收到 error 帧而非 JSON 409。
- Parallel 成员共用 `self.db`：多个 `SubAgentRunner` 并发操作同一 `AsyncSession`，随机报错。
- 漏传 `display_name`：`runtime_name` 变成 `general-purpose`，权限规则串到全部成员、事件同名。
- Routing 选不中回落 `configs[0]`：静默兜底，用户看不出模型没做决策。

---

## 6. Tests Required

现有用例（改动相关代码时必须维持这些断言点）：

### 6.1 `tests/test_workflow_orchestration.py`（编排器 / Service / API / Runtime 四段）

| 用例 | 断言点 |
|---|---|
| `test_sequential_passes_original_question_and_previous_output` | 第 1 个 prompt 恰为原始提问；第 3 个含第 2 个输出、**不含**第 1 个；成员名序 `wf-分析师 / wf-审稿人 / wf-总结者`；过滤 `WORKFLOW_PROGRESS` 后 bus 事件恰为 `BLOCK_START, TEXT_DELTA, BLOCK_STOP` |
| `test_sequential_aborts_remaining_members_on_failure` | 第 2 个失败后第 3 个未执行；消息以「顺序编排已中止」开头；details 含 `member_name` / `workflow_id`；异常细节不出现在消息与 details |
| `test_sequential_aborts_when_member_agent_disabled` | `status_code == 409`、含「Agent已停用」、`details["agent_id"] == 2`、停用成员及后续未执行 |
| `test_parallel_runs_all_members_in_isolated_sessions_and_merges` | 三成员 `db` 实例 `id` 互不相同且均来自 `AsyncSessionLocal`；汇总 LLM 入参含全部成员输出 |
| `test_parallel_tolerates_partial_failure` / `..._raises_when_all_members_fail` / `..._requires_at_least_two_members` / `..._fails_whole_when_member_disabled` / `..._members_actually_overlap` | 矩阵 14 / 10 / 11；成员确实并发重叠 |
| `test_member_display_name_is_truncated_to_column_width` | 128 字名截断后 `len == 120` 且以 `wf-` 开头 |
| `test_routing_executes_only_selected_member` / `..._rejects_choice_outside_members` / `..._decision_failure_is_fatal` / `..._requires_at_least_two_members` | 只执行被选成员；矩阵 17 / 16 / 10 |
| `test_prepare_new_session_with_workflow_checks_and_locks` | `require_runnable` 被调；`create_from_first_message` 收到 `workflow_id` 且 `agent_id=None` |
| `test_prepare_existing_workflow_session_rechecks_without_workflow_id` / `test_effective_workflow_id_falls_back_to_locked_session` | 请求不带 `workflow_id` 仍复检 `session.workflow_id`；`resolve_effective_workflow_id(None, locked) == locked.workflow_id` |
| `test_prepare_plain_agent_request_never_touches_workflow` / `..._rejects_agent_and_workflow_together` / `..._rejects_switching_workflow_on_existing_session` | 零调用 WorkflowService；矩阵 1 / 2 |
| `test_send_message_first_round_with_workflow_dispatches_workflow_runtime` | 前置复检恰一次；`WorkflowRuntime` 构造 1 次、`AgentRuntime` 0 次；`run_kwargs == [{"workflow_id": ...}]` |
| `test_send_message_second_round_without_workflow_id_still_dispatches_workflow` | **防静默降级核心用例**：API 层 `checks == []` 且仍构造 `WorkflowRuntime` |
| `test_send_message_plain_agent_request_dispatches_agent_runtime` / `..._rejects_workflow_switch_and_mixing` | 纯 Agent 走 `AgentRuntime`；API 层 409 与互斥时两个 Runtime 均未构造 |
| `test_runtime_run_commits_user_message_then_final_state` | `commits >= 2`、`rollbacks == 0`；assistant 落 `[{"kind": "text", ...}]`；事件序 `session_ready, turn_start, turn_end`；bus 已关闭 |
| `test_runtime_run_marks_failed_and_emits_error_on_orchestrator_exception` | `_mark_abnormal_end(..., "failed")` 恰一次、`rollbacks == 1`、恰一个 `error` 事件 |
| `test_sequential_emits_member_progress_with_ordinal` / `..._emits_failed_before_abort` / `..._config_failure_emits_no_progress_for_that_member` | 09-04 AC1：三成员各 running/done，`ordinal` 1/2/3、`total` 3；running 先于执行器调用（`bus_len_at_call` 为 1/3/5）；失败成员 failed 无 done、后续零条；配置期失败零条 |
| `test_parallel_emits_running_for_all_before_dispatch` / `..._emits_failed_for_single_member_and_merging` / `..._all_failed_skips_merging` / `..._merge_failure_emits_merging_failed` | 09-04 AC2：前 3 条进度全为 running 且早于任何执行器调用；失败成员 failed、其余 done；`merging/running` 在全部终态后、汇总 LLM 前，`merging/done` 为最后一条；全败零条 merging；汇总无文本 `merging/failed` |
| `test_routing_emits_routing_then_only_selected_member` / `..._decision_failure_emits_routing_failed` / `..._choice_outside_members_emits_routing_failed` | 09-04 AC3：`routing/running` 是首条事件且早于 `complete_structured`；`routing/done` 带选中 `agent_id`/`member_name`；只有选中成员有 member 进度；决策失败 / 选出成员不在列表均 `routing/failed`、零条 member |
| `test_member_spec_carries_agent_id` / `test_progress_events_serialize_with_orchestrator_compact_actor` | 09-04 AC4/AC5：交给 `SubAgentRunner` 的 `spec.agent_id == config.agent_id`；进度事件 `to_dict()` 的 actor 为编排者精简形态、`progress` 只含非 None 字段 |

### 6.1b `tests/test_events_actor.py`（09-04 AC4 / AC5）

- `test_builtin_subagent_actor_has_no_agent_id_key` / `test_orchestrator_and_teammate_actor_shape_unchanged`：
  内置 / 主循环 / teammate actor 两种形态均无 `agent_id` 键（事件形状零变化）
- `test_workflow_member_actor_carries_agent_id_in_both_shapes` / `test_agent_id_flows_into_increment_event_actor`：
  `subagent_actor(agent_id=7)` 的 full 与 compact 都含 `"agent_id": 7`，`text_delta` 事件 actor 同样带
- `test_workflow_progress_event_serialization` / `test_workflow_progress_info_omits_none_fields`：payload 形状与 3.8 示例一致
- `test_runner_passes_spec_agent_id_to_run_record_and_actor` / `test_runner_keeps_builtin_spec_record_and_actor_unchanged`：
  跑真实 `SubAgentRunner.run`（ReAct 打桩），`run_repo.create` 收到 `agent_id=spec.agent_id`，
  `turn_start` / `turn_end` actor 带 `agent_id`；内置 spec 的运行记录 `agent_id=None`、actor 无该键

### 6.2 `tests/test_workflow_service.py`

- `test_create_rejects_empty_members` / `test_create_rejects_duplicated_agent` /
  `test_create_rejects_agent_outside_workspace`：矩阵 6 / 7 / 8
- `test_create_persists_mode_value_and_members`：库里存 `mode.value` 字符串、`replace_members` 收到 `{agent_id: sort_order}`
- `test_require_runnable_rejects_disabled_and_missing`：409 / 404
- `test_get_run_members_sorted_by_sort_order_then_agent_id`：`(sort_order, agent_id)` 复合序
- `test_list_modes_covers_all_enum_values`：`_MODE_METAS` 覆盖 `WorkflowMode` 全部枚举值

### 6.3 `tests/test_subagent_spec.py`

- `test_builtin_spec_runtime_name_falls_back_to_enum_value`（参数化四个内置类型）、
  `test_explore_spec_runtime_name_is_explore`：内置 spec 零行为变更，且 `agent_id is None`
- `test_display_name_overrides_runtime_name`：传 `display_name` 时 `runtime_name` 取其值
- `test_spec_agent_id_is_readable_when_provided`：传 `agent_id=7` 可读回

### 6.4 欠账（需真实 PostgreSQL，尚未编写）

- 迁移往返：`alembic upgrade` 到 `0022_create_workflows` 后两表、`sessions.workflow_id` 与部分唯一索引存在；`downgrade -1` 后全部消失
- 迁移 `0023_subagent_run_agent_id` 往返：`subagent_runs.agent_id` 列、`fk_subagent_runs_agent_id_agents`、`ix_subagent_runs_agent_id` 出现 / 消失（09-04 实施时已用命令验证，未沉淀为自动化用例）
- workflow 成员运行记录 `agent_id` 真实落库为成员主键，内置子代理与 team 路径为 NULL
- `replace_members` 复活软删绑定不触发 `uq_workflow_agents_workflow_agent`
- 删除被 workflow 绑定的 Agent 时 RESTRICT 只拦物理删除、软删仍成功
---

## 7. Wrong vs Correct

### 第一对：发消息入口的分发判据

```python
# ❌ Wrong：只看请求体。首轮正确，第二轮起（前端不再带 workflow_id）静默退回普通 Agent，
#    降级路径本身不报错，只有输出内容变了才看得出来
if payload.workflow_id is not None:
    events = WorkflowRuntime(db, ...).run(payload.session_id, payload.content,
                                          workflow_id=payload.workflow_id)
else:
    events = AgentRuntime(db, ...).run(payload.session_id, payload.content, agent_id=payload.agent_id)

# ✅ Correct：判据包含会话已锁定的状态
effective_workflow_id = SessionService.resolve_effective_workflow_id(payload.workflow_id, session)
if effective_workflow_id is not None:
    events = WorkflowRuntime(db, ...).run(payload.session_id, payload.content,
                                          workflow_id=effective_workflow_id)
```

### 第二对：Parallel 成员的 Session

```python
# ❌ Wrong：并发成员共用 self.db（AsyncSession 非并发安全）
results = await asyncio.gather(*(self._execute_member(self.db, c, prompt) for c in configs))

# ✅ Correct：每成员独立 AsyncSessionLocal()，只共享 bus
async with AsyncSessionLocal() as sub_db:
    output = await self._execute_member(sub_db, config, prompt)
```

### 第三对：成员运行标识

```python
# ❌ Wrong：把 agent_type 放宽成 str 或直接上报 "general-purpose"
SubAgentSpec(agent_type=AgentType.GENERAL_PURPOSE, system_prompt=cfg.system_prompt, ...)

# ✅ Correct：agent_type 仅作能力档位，display_name 才是标识，且截断到列宽
SubAgentSpec(
    agent_type=AgentType.GENERAL_PURPOSE,
    display_name=build_member_display_name(cfg.name),   # "wf-<name>"[:120]
    system_prompt=cfg.system_prompt,
    allowed_tools=cfg.tool_runtime_names,
    model=cfg.model_name,
)
```

