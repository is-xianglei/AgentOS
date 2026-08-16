# 研究：其余七域评估与 seam 清单查漏

- **Query**: 核实「memory 6752 行但改造最轻」断言；查明 jobs/schedule 两个 seam 的现有承载者；判定 group/interaction/workspace/department/team/task/plan 七域在 seam 化后的形态；全仓跨域直连与 import 期副作用盘点；对照 design 第四节 18 个 seam 查漏
- **Scope**: internal（纯静态阅读；工作区 `llm.client` import 断裂未触碰；未连接任何外部服务）
- **Date**: 2026-08-15
- **前置**: 建立在 `research/agentos-baseline.md` 之上；与 `research/cross-instance-events.md`、`research/loop-and-scheduling.md` 做了交叉核对，不重复其内容

## 零、行数核对

`wc -l` 实测：memory 域 17 个文件合计恰为 **6752 行**，design 第四节的数字准确。
七域实测与任务描述一致：group 1343 / interaction 1157 / workspace 1050 /
department 988 / team 809 / task 387 / plan 367（合计 6101 行，约占全仓 19%）。

## 一、Q1 核实「memory 改造最轻」：**断言不成立，低估了约一半工作量**

「不动业务规则」成立（提取/dream/retention/召回的领域算法确实不用动）；
但「仅改依赖获取方式与注册方式」不成立，理由见 1.2–1.4。

### 1.1 依赖获取方式（这一半 design 判断基本正确）

| 依赖 | 证据 | 说明 |
|---|---|---|
| db 构造注入 | `memory/service.py:123-124` | 标准五件套写法 |
| 全局 sessionmaker 默认参数 | `memory/service.py:98`、`memory/jobs/service.py:182`、`memory/jobs/retention.py:123` | 三个 Runner 均 `session_factory=AsyncSessionLocal`；注意其「持有 sessionmaker、每操作自开短事务」恰好就是 design 5.7 要求的目标形态，只需把来源换成 `ctx.require(DatabaseService)` |
| LLM 兜底直接 new | `memory/jobs/service.py:189`、`memory/recall/service.py:58` | `llm or LLMClient()`；断裂 import 4 处（`jobs/service.py:14`、`recall/service.py:13`、`jobs/dream.py:17`、`jobs/extraction.py:15`） |
| 全局 settings 直接 import | 8 个文件：`service.py:13`、`jobs/service.py:11`、`recall/service.py:12`、`jobs/dream.py:15`、`jobs/extraction.py:14`、`jobs/retention.py:7`、`rollout_service.py:5`、`jobs/worker.py:6` | memory 独占 39 个配置项（baseline 第七节） |
| 平台密钥隐藏依赖 | `memory/service.py:571-575, 585-589` | purge 确认令牌用 `settings.jwt_secret_key` 做 HMAC 签名 |
| **import 期冻结配置** | `memory/jobs/extraction.py:103-106` | `json_schema_extra={"maxItems": settings.memory_extractor_max_items}` 在**类定义期**读配置进 JSON Schema，与 `tools/subagents/registry.py:97` 同类；R-S-7「消灭 import 期读配置」清单没有列它 |

### 1.2 跨域直连（这一半推翻断言）

memory 对 session 域有三处直连，「改依赖获取方式」覆盖不了：

1. `memory/jobs/extraction.py:20,218` 直接 new session 域 `SessionRepository`，
   :228-234、:348-353 调 `get / get_turn / list_messages_for_extraction` —— 违反 CLAUDE.md
   第 8 条。且 `list_messages_for_extraction`（`session/repository.py:257-288`）本身就是
   session 仓库里为 memory 提取窗口定制的方法。
2. `memory/recall/repository.py:13,57-88` 直接 select session 域 `session_messages` /
   `session_turns` 取最近用户消息；`:252-262` 对 session 域 turn 行 `with_for_update`，
   **`:283` 直接写别域表列 `turn.memory_context_id = context.id`** —— 违反第 8 条与第 13 条
   （ORM 可跨域导入，但不得据此绕过 Service 改别域数据）。
3. `memory/repository.py:20,728-733,811-813` join / 子查询 `session_turns`。

后果：P4 把真源切到 `session_events` 后（design 5.4），`session_messages` 降级投影缓存，
上面三处的读取窗口、行锁与回写全部要换数据源重写。**memory 与「日志真源」改造强连动**，
不是可以独立收尾的轻改造。

### 1.3 与循环的耦合点（recall 与提取入队）

- 注入侧：`runtime/agent.py:112`（构造 `MemoryRecallService(db)`）、`:578-593`
  （每 turn 调 `get_or_create_context`；`rendered_catalog` 进 system prompt 的
  memory_catalog 段 —— `prompt/compose.py:36-40`；`rendered_memories` 进 messages ——
  `session/service.py:483-487` 把它拼接在该 turn 首条用户消息前）。
- 提取侧：`runtime/agent.py:718-735`（`_complete_turn` 在 SAVEPOINT 内
  `MemoryJobService(self.db).enqueue_extract`）、`:740-756`（inline 就地执行
  `MemoryJobRunner(...).run_once(job_id=...)`，开关 `settings.memory_extraction_inline`）。
- 即 memory 同时是：未来 systemPrompt seam 的 Consumer（catalog 段）、sessionLog 的
  Consumer（消息注入 + 提取窗口读取）、`turn/end` 的监听者（入队）、jobs seam 的双料
  用户（队列 owner + inline 执行者）。单一「槽」装不下这四个耦合面（详见 Q5 第 6 条）。

### 1.4 worker 进程模型

`memory/jobs/worker.py:98-104` 是独立进程入口（`python -m memory.jobs.worker`），
`:35-50` 主循环轮询认领。插件化后该 worker 进程也必须跑一棵组合树（否则 memory 插件在
worker 里拿不到 ctx / DatabaseService / LlmRuntime），design 全篇未提「非 Web 进程如何装配」。
`memory/jobs/__init__.py:12-17` docstring 明确列出三件事只有 Worker 会做（dream 认领 /
retention 清理 / inline 失败后的重试接管），没有 worker 进程这些能力就静默失效。

### 1.5 结论

真实工作量 = 依赖注入改造（8 文件 settings + 3 处 sessionmaker 默认参数 + 2 处 LLM 兜底
new + 1 处 jwt 密钥）**+ 三处 session 直连改走 seam（与 P4 连动）+ rendered_memories
注入路径重接（现横跨 session/service.py 与 runtime/agent.py 两端）+ worker 进程宿主化
+ import 期 schema 冻结解除**。相对 tools（3134 行拆 24 包）确实较轻，
但四节表里「仅改依赖获取方式与注册方式，不动业务规则」应改写为上述清单。

## 二、Q2 `jobs` 与 `schedule` 的现有承载者

design 四节这两行的词汇（`after`/`at`/`every`、`followup()`、「与循环并行存在」）
是 dsh 形状的移植（`research/loop-and-scheduling.md:135-141`，dsh 的 schedule 包 +
`packages/jobs`）。AgentOS 现状与之对应关系如下。

### 2.1 jobs：有一个成熟承载者 —— memory/jobs 的 PostgreSQL 队列

- 认领：`memory/jobs/repository.py:227-315`，`FOR UPDATE SKIP LOCKED`（:308）+
  `lease_until` 租约（:311-315）+ `require_owned_lease` 归属校验（:329 起）。
- 重试：退避序列 `_RETRY_DELAYS_SECONDS=(5,30,120,600,1800)`（`jobs/service.py:37`），
  退避基于 DB 时钟 `func.now()`（:490-497 注释「与认领条件使用同一时间源」），
  `max_attempts` 上限，幂等键 `extract:{session}:{turn}:{prompt_version}`（:77-81）。
- 双执行模式：独立 Worker 进程（`worker.py:31-95`，轮询 `memory_worker_poll_seconds`，
  SIGINT/SIGTERM 优雅停止 :89-95）+ 请求内 inline（`runtime/agent.py:740-756`）。
- 除此以外全仓没有第二个后台任务系统：`main.py:22-34` lifespan 只建表 + 同步工具目录，
  无后台任务；`asyncio.create_task` 全仓仅 `runtime/agent.py:135,249`（每请求的
  producer task，不是任务系统）。
- **结论**：design 把 `jobs` 标「Definition 全新」只对了一半 —— provider 素材
  （lease / SKIP LOCKED / 退避 / 幂等 / 优雅退出）已在 memory/jobs 完整存在。
  且存在一处内在矛盾：把这套基建泛化为通用 jobs seam，本身就是对 memory 域的结构性
  拆解，与「memory 改造最轻」互斥 —— 两行必须择一改写。

### 2.2 schedule：现状不存在；「不是 cron」的说法与现状相容

- 全仓无 cron / 调度器 / 定时框架（baseline 第九节；本次复核无 apscheduler / celery /
  crontab 类 import）。
- 现存最接近 schedule 语义的两个碎片都埋在 memory 里：
  - `after` 雏形：失败重试 `available_at = func.now() + delay`（`jobs/service.py:496-497`）；
  - `every` 雏形：retention 的单调时钟节流（`worker.py:55-62`，
    `memory_retention_interval_seconds`）。
- 「到期只 `followup()`、绝不打断当前轮次」在现状没有任何等价物。队友唤醒是请求内
  同步单步（`runtime/agent.py:1666-1669` docstring 自述「B1 请求内单步唤醒……B2 后台
  自动唤醒后续再加」）——**B2 就是 schedule + jobs 的第一个真实业务消费者，目前未实现**。
- 风险：首批 18 个 seam 里 schedule 当前零承载者、零消费者，A3 要求的「两个 provider
  跑同一组断言」没有现实素材（详见 Q5 第 7 条）。

## 三、Q3 七域在 seam 化后的形态

| 域 | 行数 | 归类 | 一句话 |
|---|---|---|---|
| group | 1343 | (a) | 纯业务域，约 20 处机械改动 |
| interaction | 1157 | (c)，且受 5.4/5.5 结构性冲击 | approval seam 的 provider 载体；`RuntimeSuspensionRecord` 被日志吸收后半个域要重设计 |
| workspace | 1050 | (a) + 特殊身份 | 纯业务域；同时是 L3 租户 realm 的锚点、被依赖最广的 Service |
| department | 988 | (a) | 纯业务域，约 13 处机械改动 |
| team | 809 | (a)+(c) 混合 | 业务 CRUD + `turn/end` 唤醒插件；收件箱归属是 design 空白 |
| task | 387 | (a)+(c) | 纯业务域 + 4 个工具的后端 + `task_snapshot` 事件发射者 |
| plan | 367 | (c) | 整域就是未来 plan_mode Consumer 插件的内容物 |

### 3.1 group —— (a)

- 依赖形状：`GroupService(db)` 内 new 本域两个 Repo + `WorkspaceService` / `UserService`
  （`group/service.py:15-20`），跨域全走 Service，合规；无 settings、无 LLM、无事件、
  无循环耦合。
- 实例化点 19 处：`group/api.py` 15 处、`session/service.py:89,157,229`（会话共享可见性，
  函数内延迟 import）、`workspace/service.py:306-308`（移除成员级联，有归属注释）。
- seam 化改动：实例化点 db 来源改造 + `api.py` 路由改经 api 能力挂载。**合计约 20 处、
  全部机械替换**；无需为它定义任何 Definition/ABC（无第二实现动机）。

### 3.2 interaction —— (c)，但改造量被低估

- design 四节 approval 行把它列为 provider，正确：它是唯一的暂停/恢复实现
  （`interaction/service.py:29-283` 的状态机 pending→resuming→resolved/cancelled）。
- 但 design 5.5 明说「`RuntimeSuspensionRecord` 整个概念被 5.4 吸收」。该表定义在
  interaction 域（`interaction/models.py`，`database/registry.py:10` 登记）。吸收后
  interaction 一半的模型与状态机代码（`create_suspension_with_requests` /
  `continue_suspension_with_request` / `mark_suspension_resolved` 等）会瓦解为
  「日志里未配对的 approval 事件」。四节表只写了「权限裁决从循环搬出」，
  没写 interaction 自身这笔拆迁。
- 消费者：`runtime/agent.py:29-31,110`（循环挂起点）、`interaction/api.py`（HTTP 应答
  入口，也是 SSE 恢复端点之一）、`tools/builtin/ask_user_question.py:4`（只用 schema）。
- 结论：P3 期间按 (c) 做 approval provider 可行；**P4 时 interaction 是七域中唯一必须与
  sessionLog 一起重设计的域**，改造量应定性为「中」，不是「原样保留」。

### 3.3 workspace —— (a) + L3 锚点

- 纯业务域（`workspace/service.py:24-28`）；一处延迟 import `GroupService`
  （`:305-308`，注释了归属原因）。
- 特殊身份一：design 7.1 的「租户/workspace 级」realm 即以 workspace 为隔离 key；
  6.2 第 4 层租户 patch 的 DB 载体大概率挂 workspace 实体。**workspace 域是 L3 组合层的
  数据 provider，但不是能力 seam** —— 它提供的是「租户是谁」，不是可替换能力。
- 特殊身份二：被依赖最广。7 个域直接 new `WorkspaceService`：`agent/service.py:46`、
  `api/deps.py`、`auth/service.py`（5 处）、`department/service.py:35`、
  `group/service.py:19`、`memory/service.py:110`、`session/service.py`（4 处）、
  `skill/service.py:175,410`、`user/api.py`、`workspace/api.py`（11 处），合计 24 处。
  docstring 自称「工作区角色与成员归属的唯一业务入口」（`workspace/service.py:22`），
  事实上已是共享服务；ctx 服务化的省手收益最大，但保持具体类即可，不需要 ABC。
- 改动点：约 24 处实例化 + api 挂载。

### 3.4 department —— (a)

- 与 group 同构（`department/service.py:32-35`：db + 本域 Repo + WorkspaceService）；
  无 settings / LLM / 事件。
- 实例化 13 处：`department/api.py` 10 处、`session/service.py:91,147,225`。
- 改动点：约 13 处 + api 挂载。

### 3.5 team —— (a)+(c) 混合；四节表零出现，但 5.2 表有它一行

- 业务部分 (a)：成员/消息 CRUD（`team/service.py`）。跨域模范生：任务认领委托
  task 域 Service 并双向注释归属（`team/service.py:33-34,262-263`、
  `task/service.py:121-122`）—— CLAUDE.md 第 10 条的例子已落实。
- 插件部分 (c)：
  - 队友唤醒 = 未来 team 插件挂 `turn/end`（`runtime/agent.py:1661-1699` 函数内延迟
    import `AsyncSessionLocal` + `SubAgentRunner` + `TeamService`，design 5.2 已列）；
    调用点 `:216,500`。
  - `runtime/subagent.py:31,67,121,145,154` 直接使用 team 域 `SubAgentRunRepository`
    （违规，见 Q4 #3）。subagents seam 改造时需一并处理：run 记录要么走 TeamService
    公开方法，要么该表改随 subagents seam 归属。
  - `tools/builtin/team.py` 6 处工具（:53,82,108,157,203,223）是 tools seam 的 Consumer。
- **design 空白**：`team_messages` 收件箱（`read_inbox`/`peek_inbox` +
  `consumer_instance_id` 调试隔离，`team/service.py:128-156`、`team/isolation.py`）与
  5.4 事件日志的关系没人回答。dsh 的收件箱是事件溯源的（design 5.5 自引），AgentOS 的
  收件箱是独立表轮询；P4 后二者并存会出现两套「待办投递」真源。必须明确：
  收件箱保留独立表（team 插件私有存储）还是并入 `session_events`。
- 其它：`team/isolation.py:6` import 期进程常量 `_PROCESS_INSTANCE_ID` + contextvar；
  settings 依赖 2 处（:14,37）。B2 后台自动唤醒（`runtime/agent.py:1669` 注释）是
  jobs/schedule 的第一个排队消费者。
- 改动点：唤醒逻辑搬入插件（1 处大搬迁）+ subagent run 记录归属（5 处）+
  6 个工具随 tools seam 拆包 + api 3 处。

### 3.6 task —— (a)+(c)

- 纯业务域 + 事件发射：`TaskService(db, bus)`（`task/service.py:15-23`，bus 可选注入，
  已是半只脚踏进插件形态）；`emit_snapshot` 发 `RuntimeEvent.task_snapshot`（:25-35）。
- **注意**：design 5.1 的新事件词汇表没有 `task_snapshot` 对应行。`RuntimeEvent` 族迁移
  时要按 5.6 引用的 `research/agentos-event-contract.md` 逐发射点对照，别漏这一支。
- Consumer 身份：`tools/builtin/task.py` 4 个工具（:53,78,125,153）。
- 实例化 7 处（tools 4 + `runtime/agent.py` 1 + `task/api.py` 1 + `team/service.py:34` 1）。
- 改动点：7 处实例化 + 事件发射点随新词汇改 + api 挂载。

### 3.7 plan —— (c)

- 整域 = 未来 plan_mode 插件的内容物：`SessionPlanRecord` 状态 + 生命周期 Service
  （enter/write/approve_exit/reject_exit，`plan/service.py:32-80`）+ 提示词
  （`plan/prompts.py:1-27`，被 `runtime/agent.py:42` 直接 import）+ 只读 API
  （`plan/api.py:37`）。
- design 5.2 把 `PLAN_MODE_ALLOWED_TOOLS` 白名单拆给 plan_mode 插件挂
  `tools/pre-execute`；该插件的持久化与提示词就是 plan 域现有代码，包结构天然对齐
  3.2「Consumer 包五件套 + plugin.py」。
- 实例化 3 处（`plan/api.py:37`、`runtime/agent.py:111`、`tools/builtin/plan_mode.py:79`）。
- 改动点：3 处实例化 + prompts 从循环直 import 改为挂 systemPrompt section + api 挂载。

## 四、Q4 跨域直连与 import 期副作用盘点

### 4.1 A 域直接用 B 域 Repository（违反 CLAUDE.md 第 8 条）

| # | 位置 | 内容 |
|---|---|---|
| 1 | `auth/service.py:10,20` | AuthService 直接持有 user 域 `UserRepository`（auth 无自己的表） |
| 2 | `memory/jobs/extraction.py:20,218` | 直接 new session 域 `SessionRepository` |
| 3 | `runtime/subagent.py:31,67` | 直接 new team 域 `SubAgentRunRepository` |
| 4 | `runtime/compact.py:8` | 直接 new session 域 `SessionRepository`（runtime 属循环基础设施而非 feature 域，但同样绕过了 Service） |

### 4.2 自拼别域表 SQL（违反第 8 条，#6 兼违第 13 条）

| # | 位置 | 内容 |
|---|---|---|
| 5 | `memory/recall/repository.py:57-88` | select `session_messages`/`session_turns` 取最近用户消息 |
| 6 | `memory/recall/repository.py:252-262,283-284` | 锁 session 域 turn 行并回写 `turn.memory_context_id` —— 直接改别域数据 |
| 7 | `memory/repository.py:728-733,811-813` | join / 子查询 `session_turns` |
| 8 | `session/repository.py:24-38,164-192` | 反向大户：`_SESSION_CHILD_MODELS` 对 10 张别域表（task / team×3 / SubAgentRun / tools / permission / interaction×2 / plan）批量 UPDATE 级联软删除，另 :171-179 单独处理 memory 的 `TurnMemoryContextRecord`；:22-23 有动机注释 |

#8 的 seam 化后果最重：插件自带的带 `session_id` 的表不会出现在这个手工清单里，
会话软删除将**静默漏删插件数据** —— 与 R-X-1「插件自带表」直接冲突。需要
sessionPersistence seam 提供「级联登记」扩展点（插件注册自己的子表或清理回调），
或软删除改为事件驱动、各域自行监听。design 第八节只解决了迁移链，没覆盖这个运行期级联。

### 4.3 import 期副作用（已知 `tools/subagents/registry.py:97` 之外）

| # | 位置 | 内容 | seam 化时的麻烦 |
|---|---|---|---|
| 9 | `main.py:60-61` | **模块导入即 `create_app()`**（含 `setup_logging` + `register_builtin_hooks` 写全局单例） | 与「Loader 驱动装配」直接冲突，P5 必改入口 |
| 10 | `database/engine.py:14,16,20-32` | import 期读 `settings.database_url`、`create_async_engine`、副作用导入全量 ORM registry | R-S-7 已列 engine；副作用导入 registry 是有意设计（CLAUDE.md 架构第 7 条），DatabaseService 化时必须保留「拿到 session 即全部映射在册」的等价保证 |
| 11 | `core/config.py:143` | `settings = Settings()` import 期读 .env | R-S-7 已列 |
| 12 | `memory/jobs/extraction.py:103-106` | Pydantic `json_schema_extra` 类定义期冻结 `memory_extractor_max_items` | 租户级配置差异化对它无效；R-S-7 应补列 |
| 13 | `team/isolation.py:6` | `_PROCESS_INSTANCE_ID = f"process-{uuid4().hex}"` | 轻；进程级常量可接受 |
| 14 | `hooks/__init__.py:138` | `_registry = HookRegistry()` 全局单例 | R-S-7 已列 |
| 15 | `core/storage.py:152-160` | `_storage_instance` 惰性单例（首用期冻结） | 四节 storage 行已写「消灭单例工厂」，但 R-S-7 文字只点名 `_executor_instance`，应补 `_storage_instance` |
| 16 | `tools/registry.py` 顶部 | 静态 import 24 个工具模块 | 已在 tools seam 范围内 |

## 五、Q5 seam 清单查漏

### 5.1 应进表但缺席的

1. **`api`（HTTP 路由挂载）** —— design 6.4 原话「因此 `api` 能力做成 seam：Definition
   提供 `ctx.http.mount(caller, router, prefix)`」，但**第四节 18 行表与 prd R-S-6 清单
   都没有它**，正文与表自相矛盾。它三角色齐全：Definition（mount + 卸载 + OpenAPI 缓存
   失效）、Provider（FastAPI 宿主）、Consumer（15 个域的 api.py，静态聚合在
   `api/router.py:1-38`）。不进表则「消灭静态聚合」没有 A3 验收归属。
2. **`eventTransport`（跨实例事件流）** —— R-T-5/A11 已有专项研究
   `research/cross-instance-events.md`（Redis Streams、SSE `id:`=seq、写者互斥归
   advisory lock、StreamBus 拆写侧/读侧），**结论成熟但没有 seam 表载体**：现状
   StreamBus 是会话级进程内 `asyncio.Queue`（`core/event_bus.py:11-50`），sessionLog
   解决持久真源、不解决「另一实例实时收到」。建议补槽型 seam（provider:
   in-process / redis-streams），或在 sessionLog 行明确写「读侧扇出由本 seam 吸收」——
   二选一，但要落进表。
3. **`usage`（用量记账）** —— design 5.6 缺口 1 要求 usage 按模型调用粒度入日志；
   7.1 说「审计与计费相关的**服务槽**必须固定在进程级」，引用了一个清单里不存在的槽。
   L3 租户限额没有 usage 记账无从谈起。最小方案：不加 seam，在 sessionLog 事件词汇里
   固化 usage 字段并在 prd 补记账要求；完整方案：补 `usage` 槽。二者取一，现在两头皆空。

### 5.2 表内行需要修正的

4. **`jobs`**：「Definition 全新」应改为「Definition 全新；**local Provider 从
   memory/jobs 泛化**（lease / SKIP LOCKED / 退避 / 幂等键 / 优雅停机已完备，
   见 2.1 的行号）」；并补关键改造动作：**worker 独立进程也要跑组合树**（1.4）。
5. **`memory`**：修正两点。(i)「仅改依赖获取方式与注册方式」按 1.5 清单改写；
   (ii) 它是 18 行中唯一 Definition 列没有方法集的（「memory/ 域升格」不构成接口）。
   memory 的四个耦合面（1.3）更像「一组 Consumer 插件 + 自有存储」而非可替换槽；
   若保留槽定位，必须写出槽接口与「谁会换第二个实现、边界在哪」。
6. **`approval`**：补一句 interaction 域的连带拆迁（3.2）：`RuntimeSuspensionRecord`
   被 5.5 吸收后 provider 自身要重写，P4 前应知情。
7. **`schedule`**：现状零承载者、零消费者（2.2），首批落地时 A3 契约测试没有真实素材；
   唯一排队的消费者是未实现的 B2 队友自动唤醒。建议与 `jobs` 合并为一行（schedule 作为
   jobs Definition 的一部分能力），或推迟到 B2 需求真实出现，避免为空能力写两个 provider。

### 5.3 不必进表、但 design 应写明归属的

8. **team 收件箱**（3.5）：保留独立表还是并入 `session_events`，P4 冻结词汇前必须定。
9. **auth**：插件路由复用认证 Depends（`api/deps.py` 的 `get_current_user`）+ T2 租户
   短时 token（design 9.4）的签发校验，建议作为 api seam Definition 的一部分写明，
   不单独成 seam。附带记录：`auth/service.py:22-28` 仍是 MD5 密码哈希
   （CLAUDE.md 明令不复制的遗留写法），与本次改造无直接关系。

## 六、未逐一核对

- `memory/jobs/dream.py`（988 行）只核对了 import 区与个别签名
  （`rollback_dream_for_api` :597）；内部业务逻辑未逐行读 —— 不影响结论
  （其依赖形状与 extraction 同构：db+llm 构造注入、settings 直读、无 session 直连）。
- `memory/repository.py`（1208 行）只核对了跨域引用点（:20,728,811）与 purge 相关段。
- 七域的 `api.py` 只统计了实例化次数与路由聚合方式，未逐路由核对。
- `runtime/agent.py` 只读了与 memory/team/plan/interaction 相关段
  （:29-50,105-130,552-760,1661-1699），未复读全文。
- permission、skill、hooks、session/service.py 全文未在本次范围内逐行核对
  （仅核对了与七域的交互点）。
- 「19 处 / 24 处实例化」等计数来自 `grep -c` 按文件聚合，个别可能落在注释或字符串中，
  未逐处打开确认；量级可信，精确到个位数需实现时复核。

## 七、对 design.md / prd.md 的修正建议

### design.md 第四节映射表

- **增 2~3 行**：
  - `api`（槽）：Definition = `ctx.http.mount` + 卸载语义 + OpenAPI 缓存失效（内容 6.4
    已写好，只缺表行）；Consumer = 15 个域 api.py；关键动作 = 消灭 `api/router.py:1-38`
    静态聚合。
  - `eventTransport`（槽）：provider = in-process / redis-streams；Definition 采
    `research/cross-instance-events.md` 的结论（读侧扇出、SSE id=seq）。若决定并入
    sessionLog，则改为在 sessionLog 行补「读侧扇出」职责说明，不加新行。
  - `usage`（槽，可选）：或改为在 sessionLog 词汇中固化 usage 字段 + prd 补记账要求。
- **改 4 行**：`memory`（改造动作按 1.5 改写 + 补 Definition 形状）；`jobs`（「全新」→
  「Provider 从 memory/jobs 泛化」+ 补 worker 进程装配动作）；`approval`（补 interaction
  连带拆迁）；`schedule`（与 jobs 合并，或标注「延后到 B2 需求出现」）。
- **删 0 行**（schedule 不合并则保留但降级）。
- 四节末段「真正的重活集中在 tools 与循环」应补第三个重活：**session 域**
  （sessionLog + sessionPersistence 两个新 seam、`_SESSION_CHILD_MODELS` 级联机制改造、
  interaction 吸收，全都压在它身上）。
- 第八节或 sessionPersistence 行补：插件自带 `session_id` 子表的软删除级联问题
  （4.2 #8），与迁移链是两个独立的数据层缺口。

### prd.md

- **R-S-6**：18 → 19~20（+`api`，+`eventTransport` 或并入 sessionLog；`schedule` 并入
  `jobs` 则 -1；`usage` 视决策）。
- **R-S-7**：全局单例清单补两处 —— `core/storage.py:152` `_storage_instance`、
  `memory/jobs/extraction.py:103-106` 的类定义期配置冻结（属「import 期读配置」同类）；
  另建议把 `main.py:60-61`「import 即 create_app」列为同条验收对象。
- R-S-5、R-T-5 本身表述无需改，只是缺 seam 表载体（见上）。
