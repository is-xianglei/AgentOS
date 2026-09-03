# Workflow 编排引擎 — 技术设计

对应 `prd.md` 的 D1–D5 与 R1–R5。本文只写技术方案，需求与验收见 PRD。

## 1. 架构与边界

新增一个 feature 域 + 一个运行时模块，与既有 `agent/` + `runtime/agent.py` 的
分工完全同构：

```
workflow/            # 配置态：定义与 CRUD（新建，无命名冲突）
  models.py          # WorkflowRecord / WorkflowAgentRecord / WorkflowMode
  schemas.py         # *Request / *Response
  repository.py      # 持 AsyncSession，只 add/flush/refresh，不自行 commit
  service.py         # WorkflowService：CRUD + 取运行成员
  api.py             # /api/workflows 路由
runtime/workflow.py  # 执行态：WorkflowRuntime + 三个编排器（新建）
```

选择依据：`runtime/` 已是既有的执行态顶层包（agent.py、subagent.py），
编排属执行态而非 CRUD，放进 `workflow/service.py` 会让该文件同时承担
配置管理与 ReAct 编排两种职责。

依赖方向（严格遵守 Service→Service）：

```
session/api.py ──► WorkflowRuntime ──► WorkflowService     （本域 Service）
                        │            ──► AgentService       （跨域，取运行配置）
                        └──► SubAgentRunner ──► ReAct 执行单元（复用，见 F3）
```

- `runtime/workflow.py` **不得**直接使用 `AgentRepository` 或自行拼 `agents` 表 SQL。
  取 Agent 配置一律经 `AgentService.get_runtime_config()`（`agent/service.py:189`）。
- `sessions.workflow_id` 属 session 域，其校验与写入放 `SessionService`，
  `runtime/workflow.py` 只转发。

## 2. 数据模型

### 2.1 `workflows`（新表）
照抄 `agents`（`agent/models.py:20`）的形状，含 Base 的审计字段与软删除：

| 列 | 类型 | 说明 |
|---|---|---|
| workspace_id | FK workspaces.id, CASCADE | 工作区隔离 |
| name | String(128) | 工作区内唯一（软删除条件唯一索引） |
| description | Text, nullable | 工作流用途说明 |
| mode | String(32) | 受控枚举，见 2.3 |
| is_enabled | Boolean, default True | 停用后拒绝运行 |
| created_by_user_id | FK users.id, RESTRICT | 与 agents 一致 |

索引：`uq_workflows_active_workspace_name`（workspace_id, name，
`postgresql_where=is_deleted=false`）、`ix_workflows_workspace_enabled`。

### 2.2 `workflow_agents`（新表）
照抄 `agent_tools`（`agent/models.py:78`）：

| 列 | 类型 | 说明 |
|---|---|---|
| workflow_id | FK workflows.id, CASCADE | 随 workflow 级联 |
| agent_id | FK agents.id, **RESTRICT** | 与 agent_tools/agent_skills 一致 |
| sort_order | Integer, default 0 | 执行/展示序，无 role 列（D3） |

约束：`UniqueConstraint(workflow_id, agent_id)`——同一 Agent 不重复绑定（AC1）。

RESTRICT 只拦物理删除；Agent 走软删除，故运行期还需 AC7 的显式校验。

### 2.3 `WorkflowMode` 枚举
定义在 `workflow/models.py`，`class WorkflowMode(str, Enum)`：
`SEQUENTIAL = "sequential"` / `PARALLEL = "parallel"` / `LLM_ROUTING = "llm_routing"`。
Schema 直接标注该枚举类型，非法值由 Pydantic 拦下并转中文异常。

### 2.4 `sessions.workflow_id`（改既有表）
照抄 `sessions.agent_id`（`session/models.py:78`）：FK `workflows.id`，
`ondelete="SET NULL"`，nullable，index，中文 comment。

新增 ORM 需登记 `database/registry.py`，并提供显式 upgrade/downgrade 迁移（AC11）。

## 3. F4 桥接方案：把数据库 Agent 送进 SubAgentRunner

这是本任务唯一需要改动既有共享代码的地方，方案取**纯增量、零行为变更**。

### 3.1 问题
`SubAgentSpec.agent_type`（`tools/subagents/definition.py:64`）是 `AgentType` 枚举，
只有 4 个内置值（`definition.py:34`）；自定义 Agent 是数据库动态行，无枚举可填。
而该值在下游被当作**运行标识**使用，共 4 类用途：

| 用途 | 位置 |
|---|---|
| 运行记录 `subagent_runs.member_name` | `runtime/subagent.py:103,135` |
| 前端事件契约的 `agent_type` 字段 | `runtime/subagent.py:110,170,182` |
| **权限判定维度** | `runtime/subagent.py:303,309` → `permission/service.py:37` |
| 日志与 TMP_WRITABLE 临时目录 | `runtime/subagent.py:425` / `definition.py:129` |

### 3.2 方案：新增 `display_name` 字段 + `runtime_name` 属性
在 `SubAgentSpec` 上增加 `display_name: str = ""`，并加只读属性：

```python
@property
def runtime_name(self) -> str:
    """运行标识:落运行记录、发事件、判权限、打日志都用它。
    内置 SubAgent 留空即回落枚举值,行为与改造前完全一致。
    """
    return self.display_name or self.agent_type.value
```

再把 3.1 表格中的 `spec.agent_type.value` 全部替换为 `spec.runtime_name`
（`subagent.py` 5 处 + `definition.py:129` 1 处）。

Workflow 侧构造：

```python
SubAgentSpec(
    agent_type=AgentType.GENERAL_PURPOSE,      # 只作能力档位,不再作标识
    display_name=f"wf-{agent_name}",           # 真正的运行标识
    system_prompt=config.system_prompt,
    allowed_tools=config.tool_runtime_names,
    model=config.model_name,
)
```

### 3.3 为什么这样是安全的
- **零行为变更**：4 个内置 spec 不传 `display_name`，`runtime_name` 恒等于
  `agent_type.value`，既有事件、运行记录、权限判定、日志逐字节不变。
- **无权限提升**：`BUILTIN_AGENT_DEFAULTS`（`permission/service.py:27`）当前只有
  `("verification", "Bash"): "allow"` 一条。workflow 成员的 `runtime_name` 形如
  `wf-xxx`，命不中该条，落到
  `DEFAULT_ASK if tool_name in DANGEROUS_TOOLS else DEFAULT_ALLOW` 的通用默认，
  与任意未配置子代理同级——**不会**继承 general-purpose 的任何特权。
  `PermissionService.evaluate()` 的 `agent_type` 形参本就是 `str`，无需改签名。
- **不复用枚举值作标识**：若改让 workflow 成员上报 `general-purpose`，用户为
  general-purpose 配的权限规则会连带作用到全部 workflow 成员，且事件里所有成员
  同名，前端无法区分。`display_name` 同时解决这两点。
- **递归天然被禁**：`GLOBAL_SUBAGENT_DENIED_TOOLS`（`definition.py:45`）已禁掉
  Agent / TeamCreate / TeamSpawn，workflow 成员无法再派生子代理（F5）。

### 3.4 `AgentRuntimeConfig` 需补两个字段
`AgentRuntimeConfig`（`agent/service.py:32`）当前缺 `name` 与 `description`：
前者用于 `display_name`，后者是 Routing 的判断依据（D3）。
两个字段追加到该 frozen dataclass 末尾，由 `get_runtime_config()` 一并填充。
属 agent 域自有返回类型的扩展，符合「跨域读数据在数据所属域补方法」的规约；
现有构造点用关键字传参，追加字段不影响既有调用。

### 3.5 未桥接项（v1 明确不做）
`SubAgentSpec.max_rounds`（默认 6）与 `shell_access`（默认 `READ_ONLY`）在
`agents` 表中无对应列。v1 全部用 spec 默认值，不新增列、不做成可配项——
`READ_ONLY` 是更保守的一侧，不引入新风险。

## 4. 执行流

### 4.1 `WorkflowRuntime` 骨架
与 `AgentRuntime` 同构（生产者-消费者 + StreamBus），`run()` 签名对齐以便
`session/api.py` 无差别切换：

```python
async def run(self, session_id: int | None, user_content: str, *,
              workflow_id: int) -> AsyncIterator[dict[str, Any]]:
```

主流程顺序照抄 `runtime/agent.py:159-252` 的 `_produce`：
1. `SessionService.prepare_for_message(...)`（`session/service.py:325`）取/建会话；
2. `start_turn()`（`session/service.py:356`）建 Turn 并落 user 消息；
3. **立即 commit**——理由与 `runtime/agent.py:187-188` 的中文注释一致：
   流式执行跨越请求依赖的 yield 生命周期，原始输入必须先可靠提交；
4. emit `session_ready`（`core/events.py:381`）；
5. 按 mode 分发到编排器；
6. 落 assistant 最终消息（`SessionService.add_message`，底层
   `session/repository.py:210`），`_complete_turn` 后再 commit
   （对应 `runtime/agent.py:819-820` 的注释理由）；
7. `finally` 关闭 bus（对应 `runtime/agent.py:252`）。

事件不新增类型：成员执行本身由 `SubAgentRunner` 发既有 subagent 事件
（`runtime/subagent.py:110,170,182`），前端契约零改动。编排层只额外发
`EventType` 既有类型的文本事件说明当前阶段，不擅自扩 `core/events.py:28-40` 的枚举。

### 4.2 并发与 Session 隔离（Parallel 的硬约束）
`AsyncSession` 非并发安全，而 `SubAgentRunner` 持有 `self.db`。
**Parallel 模式下每个成员必须持有独立 Session**，照抄既有范式：

- `runtime/agent.py:1802-1815`：`# 每个队友用独立 AsyncSessionLocal,共享 self.bus`
  → `async with AsyncSessionLocal() as sub_db:`
- `tools/builtin/agent.py:128-131`：Agent 工具派生子代理同样如此。

即：**独立 Session、共享 bus**。`get_db` 的 docstring（`database/engine.py:60`）
已说明后台任务自行按步 commit、请求收尾 commit 是安全空操作，本方案未越界。

Sequential 与 LlmRouting 是串行的，可沿用 runtime 自身的 `self.db`。

### 4.3 三种编排器

**SequentialOrchestrator**
```
carry = None
for member in members(按 (sort_order, agent_id) 升序):
    prompt = 原始提问                      if carry is None
             原始提问 + 上一步输出模板      otherwise      # D4
    carry = await run_member(member, prompt)              # 失败即抛，中止后续  # D5
return carry
```
输入拼装用模块级中文常量模板，不做成可配项（D4）。

**ParallelOrchestrator**
```
results = await asyncio.gather(*[run_member_in_own_session(m, 原始提问) for m in members],
                               return_exceptions=True)     # 部分失败不中止其余  # D5
succeeded = [r for r in results if not isinstance(r, BaseException)]
if not succeeded: raise AgentException.message("并行编排的全部成员均执行失败", ...)
return await self._merge(user_content, succeeded)          # 内部调 LLM 汇总  # D3
```
失败成员只记服务端日志（`logger.exception`，含成员名与 workflow_id），
不把异常详情拼进汇总提示词，避免泄漏到模型输出与 API 响应。

失败语义分两段（实现时明确）：**配置期整体失败、运行期部分容忍**。
`get_runtime_config()` 对全部成员的加载在 `asyncio.gather` **之前**串行完成（用主
`self.db`），任一成员停用/软删/不存在即整体抛中文异常（AC7），此时尚未派发任何成员；
D5 的「部分失败不中止其余」只作用于运行期（`SubAgentRunner` 执行失败）。若把配置加载放进
成员协程里静默跳过，该成员不经过 `SubAgentRunner`，bus 上零事件，前端无从得知它被略过。

**LlmRoutingOrchestrator**
```
choice = await self._route(user_content, members)   # 依据各成员 agents.description  # D3
member = 按 choice 定位；定位不到直接抛中文异常，不回落第一个成员
return await run_member(member, 原始提问)                                        # D5
```
路由用 `complete_structured()` 拿受约束的结构化选择，不解析自由文本。

### 4.4 成员执行的公共步骤
```
config = await AgentService(db).get_runtime_config(workspace_id, agent_id)  # 含 F1 的停用校验 → AC7
spec   = SubAgentSpec(agent_type=AgentType.GENERAL_PURPOSE,
                      display_name=f"wf-{config.name}",                     # §3.2
                      system_prompt=config.system_prompt,
                      allowed_tools=config.tool_runtime_names,
                      model=config.model_name)
report = await SubAgentRunner(db, bus=self.bus).run(
             session_id, prompt, spec,
             parent_turn_id=turn_id, user_id=..., workspace_id=...,
             allowed_skill_names=frozenset(config.skill_names))
```
`get_runtime_config()` 内部已调 `require_runnable()`（`agent/service.py:180`），
停用/软删的成员在此抛 409，满足 AC7 且无需重复校验。

## 5. 运行入口与会话语义（D1 / D2 落点）

### 5.1 请求体扩展
`SessionSendMessageRequest`（`session/schemas.py:8`）追加：
```python
workflow_id: int | None = Field(default=None, description="使用的Workflow ID，与agent_id互斥")
```

### 5.2 校验落在 SessionService，不落 API
`sessions` 表属 session 域，故 D2 的两条规则写进
`SessionService.prepare_for_message`（`session/service.py:325-354`），
与该方法已有的三个校验分支并列，`session/api.py` 只多一个分发判断：

| 情形 | 行为 |
|---|---|
| `workflow_id` 与 `agent_id` 同时有值 | 抛中文异常（互斥，F7） |
| 新建会话且带 `workflow_id` | 校验 workflow 可运行后建会话，`agent_id` 留空 |
| 已有会话，传入的 `workflow_id` 与 `session.workflow_id` 不同 | 409，与 `session/service.py:349` 的「已有会话不能切换Agent」同构 |
| 已有会话且 `session.workflow_id` 非空 | 每轮复检 workflow 可运行（对齐 `:350-353` 对 Agent 的复检） |

跨域校验经 `WorkflowService`（局部导入避免环形依赖，照 `session/service.py:336` 的既有写法）。

注意「已有会话不能切换Agent」这条校验在仓库里是**双写**的：
`session/api.py:69` 与 `session/service.py:349` 各有一份，前者在构造 Runtime 之前
先跑（它那一分支已经通过 `require_write_access` 拿到了 session 行）。
workflow 的 409 规则须与之对称地补在两处，不能只补 Service——否则 API 层会先用
错误的 workflow 分派出去，Service 的 409 永远轮不到执行。

### 5.3 API 分发按「生效 workflow_id」而非请求体字段
`session/api.py:46` 的 `send_message` 内二选一构造 `WorkflowRuntime` 或
`AgentRuntime`，两者 `run()` 均产出 `AsyncIterator[dict]`，SSE 包装与 `format_sse`
完全复用。

**判据必须是「请求体的 workflow_id 或会话已锁定的 workflow_id」**：

```python
session = None
if payload.session_id:
    session = await SessionService(db).require_write_access(...)
    ...
# D2:会话一旦锁定 workflow,后续轮次前端不会再带 workflow_id,
# 只按 payload 判断会让第二轮悄悄退回普通 Agent 路径。
effective_workflow_id = payload.workflow_id or (session.workflow_id if session else None)
```

只看 `payload.workflow_id` 是本设计最容易踩的坑：首轮正确、第二轮起静默降级，
且降级路径本身不报错，只有输出内容变了才看得出来。AC8 的回归要同时覆盖
「首轮带 workflow_id」与「第二轮不带、仍走编排」两条。

## 6. 新增 API 契约（`/api/workflows`）

全部使用 `ApiResponse[T]` + `ok(data, request)`（`core/responses.py:20`，
导入 `from core.responses import ApiResponse, ok`），Depends 别名在 `workflow/api.py`
就地定义（照 `agent/api.py:22-24`，本仓库无公共 deps 别名模块）。

| 方法 | 路径 | 说明 |
|---|---|---|
| POST | `/api/workflows` | 创建，body 含 mode 与成员列表 |
| GET | `/api/workflows` | 分页列表，支持 keyword / is_enabled |
| GET | `/api/workflows/{id}` | 详情，含成员及其 sort_order |
| PATCH | `/api/workflows/{id}` | 改名/描述/mode/启停/成员（整体替换成员集） |
| DELETE | `/api/workflows/{id}` | 软删除 |
| GET | `/api/workflows/modes` | R4 模式元数据（静态，不查库） |

`/modes` 必须声明在 `/{id}` **之前**，否则 `modes` 会被路径参数吞掉。

Schema 命名照 `agent/schemas.py`：Request 带 `model_config = {"extra": "forbid"}`，
Response 带 `model_config = {"from_attributes": True}`；分页返回沿用
`items / total / limit / offset` 四字段形状。

路由挂载：`api/router.py` 加一行 `include_router(workflow_api.router,
prefix="/workflows", tags=["workflows"])`（照 `api/router.py:20`），`main.py:55` 无需改动。

## 7. 配置变更（R5）

新增 `workflow` 场景，四处同步：

1. `llm/factory.py:18` 的 `_KNOWN_SCENARIOS` 加 `"workflow"`；
2. `core/config.py` 加 `anthropic_workflow_model` 与 `openai_workflow_model`
   （照 `core/config.py:36-47` / `:58-67` 的字段族格式，`str | None = None`）；
3. `.env` 与 `.env.example` 各加 `AGENTOS_ANTHROPIC_WORKFLOW_MODEL=` /
   `AGENTOS_OPENAI_WORKFLOW_MODEL=`（`.env.example` 只留空占位，不写任何真实值）。

`Settings` 的 `SettingsConfigDict(extra="forbid")`（`core/config.py:20`）会在启动时
把「`.env` 有键但 Settings 无字段」直接判失败，是这次同步的天然守卫。
未配置场景模型时回落 provider 主模型，两者皆空由工厂抛中文异常，不硬编码模型名。

## 8. 兼容性与回归风险

| 风险 | 说明 | 缓解 |
|---|---|---|
| `SubAgentSpec` 改动波及全部子代理 | 4 处内置 spec + Agent 工具 + 团队路径共用 | 纯增量字段 + 属性回落，内置行为逐字节不变；用既有 subagent 测试兜底（§3.3） |
| 并发 Session 误用 | Parallel 共用一个 `AsyncSession` 会随机炸 | 强制 `AsyncSessionLocal()` per 成员（§4.2），代码评审重点项 |
| 现有发消息接口回归 | 所有会话都走这条路 | 不带 `workflow_id` 时不进任何新分支（AC8），保留既有测试 |
| Alembic 链断裂 | 当前 head 为 `0021_message_llm_attribution` | 新迁移 `down_revision = "0021_message_llm_attribution"`，id 用 `0022_短名` 人读格式 |
| 漏登 registry | autogenerate 会误判为删表 | `database/registry.py` 加一行导入（AC11） |

回滚：新增域可整体回退；唯二侵入既有代码的点是 `SubAgentSpec.runtime_name`
与 `sessions.workflow_id` 列，前者是加字段、后者有 downgrade 迁移，均可独立回滚。

## 9. 关键取舍记录

- **编排器放 `runtime/` 而非 `workflow/service.py`**：编排是执行态，与
  `runtime/agent.py`、`runtime/subagent.py` 同层；`workflow/service.py` 只管配置态。
- **不新增事件类型**：复用 `SubAgentRunner` 已有的 subagent 事件，前端零改动；
  代价是编排阶段的可观测性弱一些，后续需要再扩 `core/events.py`。
- **`display_name` 而非把 `agent_type` 放宽成 `str`**：后者要改 4 个文件、
  波及 `tools/builtin/agent.py` 与 `runtime/agent.py:1813` 的枚举转换，
  且会让权限维度语义漂移；前者是纯增量（§3.2/§3.3）。
- **v1 不做 `max_rounds` / `shell_access` 可配**：用 spec 默认值（§3.5），
  保守一侧，不引入新风险面。
- **编排阶段不额外发文本事件**：成员过程事件由 `SubAgentRunner` 自发，编排者
  （`ORCHESTRATOR_ACTOR`）只发 turn_start / turn_end 与承载最终回答的一个文本块
  （并行汇总走装配器逐字流式），不为「正在路由」「正在汇总」等阶段另发说明文本。
- **`WorkflowRuntime` 不接 prompt hooks / 任务快照 / 记忆提取**：`AgentRuntime._produce`
  中的 `_apply_user_prompt_hooks`、`TaskService.emit_snapshot`、Memory 入队与就地提取
  均不复制到 workflow 路径；`_complete_turn` 只标 turn completed、session idle 并 commit。
- **D2 每轮重跑时成员不接收历史消息**：成员只收到本轮 `user_content`（顺序模式外加
  上一步输出），不注入会话历史；追问依赖用户在提问中自带上下文。这是 v1 为控制成本与
  上下文膨胀的取舍，后续如需可在编排层按需注入。
