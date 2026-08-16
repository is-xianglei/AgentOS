# Research: 事件溯源切真源（P4-4）工程可行性验证

- **Query**: 验证 design.md 5.4/5.5/5.7 的设计断言在本项目真实部署形态下是否成立（Q1 序号设计 / Q2 投影性能 / Q3 单写者 / Q4 幂等追加 / Q5 运行时断言）
- **Scope**: mixed（代码走读 + 官方文档 + 对 `.env` 配置库的只读实测 + 本地基准测试）
- **Date**: 2026-08-15
- **实测环境**: PostgreSQL 17.10（`.env` 的 `AGENTOS_DATABASE_URL`，119.45.22.177:6543），psycopg 3.3.4 + SQLAlchemy 2.0.51（`.venv` 实测导入），探测脚本见 `/tmp/agentos_probe.py`、`/tmp/agentos_probe2.py`、`/tmp/agentos_fold_bench.py`（只读，未写任何表）

## 0. 结论速览

| 问题 | 一句话结论 |
|---|---|
| Q1 序号 | **不要用 BIGSERIAL/sequence 当会话游标**。每会话 `seq` 由写者显式按 `last+1` 赋值 + `UNIQUE(session_id, seq)` 仲裁；全局 BIGSERIAL 只作物理主键。乱序可见性空洞真实存在（PG 官方 + Marten 官方均确认），但按会话单写者 + 会话内游标可整体绕开 |
| Q2 投影 | 纯折叠是**微秒级**（1 万事件 2.25ms），瓶颈全在网络拉取（本环境 1 万行约 23 秒）。快照+水位必须保留且形态与现有 `load_context_for_turn` 同构；Turn 内热路径零 DB 重读可做到 |
| Q3 单写者 | **design 5.5 的「advisory lock 保证单写者」在本项目部署形态下不成立**：会话级锁被事务池（官方 Never）与 SQLAlchemy 连接池归还语义双重击穿；事务级锁又跨不过分步 commit。正解 = 乐观追加（唯一约束仲裁）为正确性底座 + 租约行为效率层 |
| Q4 幂等 | 双唯一约束：`UNIQUE(session_id, seq)`（并发仲裁）+ `UNIQUE(event_id)`（重试幂等）。与 message-db 官方 schema 同型 |
| Q5 断言 | 分三级：构造性保证（循环只吃投影输出）→ 热路径哈希断言（实测 1~5ms CPU，无额外 DB 读）→ CI/影子全量逐字节。失败必须抛错中断并必发 `turn/end`，不能只记日志 |

**总判断：P4-4 工程上可行，但 design 5.5 的单写者机制必须改写，design 5.6 缺口 2 的「DB 单调分配」表述必须精确化，否则照原文实现会产出静默失效的互斥与不可用的续传游标。**

---

## 1. 环境事实（先于一切结论，含对任务前提的两处纠偏）

### 1.1 驱动是 psycopg3，不是 asyncpg（任务描述有误）

- `pyproject.toml:13`：`"psycopg[binary]>=3.3.2"`；`uv.lock` 无 asyncpg 条目；`.venv` 实测 `psycopg 3.3.4`。
- `database/engine.py:16` 连接串前缀 `postgresql+psycopg://`；`engine.py:24` 传 psycopg3 专属参数 `connect_args={"prepare_threshold": None}`。
- 影响：Q3 中所有「asyncpg 会话级锁」的讨论对象应改为「psycopg3 + SQLAlchemy 连接池」，但结论同样成立（陷阱在池化语义，与具体驱动无关）。

### 1.2 数据库前面（按代码注释）有事务模式 pooler

- `database/engine.py:18-19` 注释原文：「Supabase pooler(事务模式)不支持服务端 prepared statements, psycopg3 下需禁用 prepare, 否则并发会话会报 DuplicatePreparedStatement」——这是项目自己踩过的真实故障记录。
- `.env` 的 `AGENTOS_DATABASE_URL` 端口 **6543**。Supabase 官方文档（connecting-to-postgres）：6543 是 transaction-mode pooler（Supavisor 共享池或专用 PgBouncer）的约定端口，5432 才是 session 模式/直连。
- **实测（/tmp/agentos_probe2.py）**：轻负载下同一客户端连接 8 个连续事务 backend pid 稳定不变、两条并发连接 pid 不互换、`SET application_name` 跨事务保持——该表现与会话池/直连一致，**未能在轻负载下确证事务池**（事务池在无竞争时也常复用同一 server 连接，此测法本就不具排他性）。`SHOW pool_mode` 落到真实 PG 报 `UndefinedObject`（说明中间层不拦截 SQL，无法据此分辨）。
- 工程结论：池化模式**实测不可确证、随部署环境可变**，而 engine.py 注释证明该项目至少在某个真实环境里跑在事务池后面。因此任何依赖「客户端连接 = 服务端会话」的机制（会话级 advisory lock、SET、PREPARE、LISTEN）都必须按最坏情况排除。

### 1.3 网络与数据量实测（决定 Q2 的量级判断）

/tmp/agentos_probe.py、probe2 对 `.env` 库的只读实测：

| 指标 | 实测值 |
|---|---|
| RTT（`select 1`） | 平均 18 ms |
| 拉取 500 行 × ~0.8KB jsonb | 487 ms |
| 拉取 10000 行 × ~0.8KB jsonb | **22987 ms（约 23 秒）** |
| 有效带宽 | 约 0.35~1 MB/s（跨公网，带宽是硬瓶颈） |
| `session_messages` 现有行数 | 673（单会话最多 45 条，平均 content 1013 字节） |
| `session_snapshots` 现有行数 | **0**（50k token 压缩阈值 `runtime/compact.py:25` 从未在真实数据上触发过） |
| `session_turns` 现有行数 | 93 |
| `pg_try_advisory_xact_lock` 经此连接路径 | 可用（返回 true，`pg_locks` 可见，回滚自动释放） |

两个直接推论：① 「上万事件全量重放」在本部署下是**秒级到十秒级的网络 IO**，与折叠的 CPU 成本（见 §3）差三到四个数量级，讨论投影性能实质是讨论**拉取行数**；② 现网数据尚小且从未产生过快照，切真源时的存量迁移压力可忽略，风险全部在增量路径。

---

## 2. Q1：append-only 事件表与单调序号

### 2.1 现状（要被替换的东西）

- `sequence` 是**每 HTTP 请求内**的进程内计数：`core/event_bus.py:20` `self._sequence = 0`，`:31-32` emit 时 `+1` 回填；bus 随请求消亡，断线即丢（research/agentos-event-contract.md §1.6 已确认）。
- 事件对象无时间戳（core/events.py 全部字段中没有 timestamp，event-contract §1.3）。
- SSE 帧只有 `data:` 行（runtime/agent.py:1710-1712），无 `id:`，Last-Event-ID 续传无从谈起。

### 2.2 三种序号方案的取舍

| 方案 | 单调性 | gapless | 能否检测双写者 | 结论 |
|---|---|---|---|---|
| A. 全局 BIGSERIAL / IDENTITY | 分配单调，**提交可见顺序不单调** | 否（官方明示） | **否**（两个写者各拿各的号都插入成功，历史静默交错） | 只可作物理主键与全局粗排序 |
| B. 每会话一个 PG sequence 对象 | 同 A | 否 | 否 | 对象数随会话线性膨胀 + DDL 管理，无任何额外收益，排除 |
| C. 每会话应用层显式赋值 `seq = last_seq + 1` + `UNIQUE(session_id, seq)` | 会话内严格全序 | 是（事务性，回滚不留洞） | **是**（第二写者必撞唯一约束） | **采用** |

方案 A/B 的两条官方依据：

1. **gapless 不可能**：PostgreSQL 官方文档 Sequence Manipulation Functions 的 Caution 原文：「the value obtained by nextval is not reclaimed for re-use if the calling transaction later aborts... **PostgreSQL sequence objects cannot be used to obtain "gapless" sequences**」。CREATE SEQUENCE 文档进一步给出替代法：「It is possible to build gapless assignment by using exclusive locking of a table containing a counter」——即方案 C 的行锁计数形态。
2. **乱序**：CREATE SEQUENCE 文档明示 cache>1 时「the values might be generated out of sequence when all the sessions are considered」；即便 cache=1（默认），nextval 的取号顺序也≠事务提交顺序，见下节。

### 2.3 「已提交但序号乱序」的可见性空洞：真实存在，且直接破坏 Last-Event-ID

机理（与驱动无关，纯 MVCC 性质）：事务 T1 取到 seq=100，T2 取到 seq=101；T2 先 commit。
此刻轮询/续传读者看见 101，把游标推进到 101；T1 随后才 commit，其 100 **永远落在游标后面**，
`WHERE seq > 101` 再也读不到它——对 SSE 客户端就是静默丢帧。

业界对该空洞的标准处置（若确需**全局**游标时）：

- **Marten（官方文档 Async Projections Daemon）**：引入 High Water Mark——「the furthest known
  event sequence that the daemon "knows" that all events with that sequence or lower can be safely
  processed in order... will frequently be a little behind the highest known event sequence number
  **if outstanding gaps in the event sequence are detected**」。即：读者不追最大序号，只推进到
  「之前无空洞」的安全水位，检测到 gap 就等待/超时补偿。
- **message-db（Eventide 官方 schema）**：写入端串行化——`write_message` 内先
  `pg_advisory_xact_lock(hash(category))` 再插入（`database/functions/acquire-lock.sql`），
  使同类别流的 `global_position` 分配与提交近似同序。
- **单写者**：只要保证同一流只有一个写者串行追加，流内提交顺序 = 序号顺序，空洞不产生。

**对 AgentOS 的适用性判断**：SSE 的 Last-Event-ID 是**会话内**游标（一个 EventSource 只订一个
session），不需要全局游标。方案 C 的 per-session `seq` 在「每会话单写者」（Q3）成立时，
会话内提交顺序必然等于 seq 顺序，可见性空洞在游标维度上**整体不存在**。全局 BIGSERIAL `id`
仅作物理主键保留；将来若出现跨会话订阅者（审计/复制），再按 Marten 的高水位模式处理，
不要提前设计。

### 2.4 建议表形状（供 P3-4 建表参考，非规定）

```sql
CREATE TABLE session_events (
    id            BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,  -- 物理序，不做游标
    session_id    INT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
    seq           BIGINT NOT NULL,              -- 写者显式赋值 last+1
    event_id      UUID NOT NULL,                -- 构造期生成，重试携带同一值
    turn_id       UUID NULL,
    actor_role    TEXT NOT NULL, actor_name TEXT NOT NULL, run_id TEXT NULL,  -- 缺口4
    type          TEXT NOT NULL,
    payload       JSONB NOT NULL,               -- database/base.py:8-13 统一 JSONB
    occurred_at   TIMESTAMPTZ NOT NULL DEFAULT now(),               -- 缺口2
    UNIQUE (session_id, seq),                   -- 并发仲裁（Q3/Q4 的底座）
    UNIQUE (event_id)                           -- 重试幂等（Q4）
);
-- 读路径索引即主查询 (session_id, seq)，唯一约束已覆盖，无需另建
```

注意两点：① append-only 表与 `Base` 的审计/软删除字段（database/base.py）语义冲突——事件不可
更新不可软删，建议不继承 Base 或豁免软删过滤器（`database/engine.py:35-57` 的全局
`is_deleted = false` 过滤会给每次投影查询加无意义谓词）；② `seq` 用 BIGINT 而非 INT
（session_messages.id 现为 Integer，session/models.py:140，事件量比消息量高一个数量级）。

---

## 3. Q2：投影性能、快照与水位

### 3.1 成本拆解（实测数字）

折叠（/tmp/agentos_fold_bench.py，模拟新词汇 user/assistant/tool-call/tool-result 折叠，
分支复杂度对齐 `session/service.py:468-515` 的 `_messages_to_context`）：

| 规模 | 纯折叠耗时 | 折叠出的 messages 数 |
|---|---|---|
| 1 000 事件 | 0.19 ms | 643 |
| 10 000 事件 | 2.25 ms | 6 429 |
| 50 000 事件 | 21.06 ms | 32 143 |

拉取（§1.3）：500 事件 ≈ 0.5 s，10 000 事件 ≈ 23 s。**结论：500 轮/上万事件的会话，
每次模型调用从头重放在本部署下完全不可接受（23 s/次 vs 现有一次 LLM 调用本身才几秒）；
但罪魁是网络 IO，不是折叠。** 只要把「需要拉取的行数」压到尾部几百行，投影就是免费的。

### 3.2 关键对照：现状本来就是「快照 + 水位」，每轮循环全量重读 DB

- `session/service.py:418-430` `load_context`、`:432-466` `load_context_for_turn`：
  读 `latest_snapshot`（水位 = `through_message_id`，session/models.py:267-277）+
  水位后消息 + 当前 Turn 消息。
- 主循环**每次 LLM 调用都重新调它**（runtime/agent.py:609，for 循环体内第一行）。
  即现状每轮工具迭代已经全量重读一遍 DB 消息表——新形态只需做到不比这更差。

### 3.3 「日志唯一真源」与「不每次全量重放」的并存方案

三层结构，每层都有现成锚点：

1. **Turn 内热路径（零 DB 读）**：写者进程持有内存投影 `(messages, last_seq)`。
   本轮产生的每个事件先 append 入库（分步 commit，见 §5.7 豁免）再折叠进内存投影——
   事件由本写者自己产生，无需回读。对照：translator 已是这种「流内累积状态」形态
   （llm/types.py:118-220 跨多次调用累积 usage/stop_reason）。
2. **Turn 边界冷路径（尾部读）**：turn 开始/接管时，读最新快照行 + `seq > through_seq`
   的尾部事件折叠。目标是把尾部控制在几百行内（≈0.5 s，可接受的接管成本）。
3. **快照行（投影缓存）**：`session_snapshots` 改形态为
   `(session_id, through_seq, projector_version, messages JSONB, summary)`。
   写入时机沿用现有 compact 路径（runtime/compact.py:32-57 的 L2 阈值触发），
   外加「尾部长度阈值」触发（如尾部 > 500 事件即异步补快照，与压缩解耦）。

**缓存失效与重建策略**（Q2 问的核心）：

- 日志 append-only ⇒ 快照**永不会「错」，只会「旧」**。不存在传统缓存失效问题，
  只有「水位落后」问题，落后即多折叠几行尾部，语义仍正确。
- 唯一需要作废快照的场景是**投影函数本身改了**（折叠规则/压缩折叠语义变更）。
  处置：快照行带 `projector_version` 列，读侧不匹配即视为未命中，从更早的匹配快照
  或从头重建（一次性 23 s 级离线成本，后台做，不挡请求）。这是事件溯源的标准
  「快照可丢弃、带版本」做法（Marten/EventStoreDB 均如此对待 snapshot）。
- 压缩事件（design 5.4「往日志追加压缩事件，投影时折叠前缀」）不产生失效：
  压缩事件本身有 seq，`through_seq >= 压缩事件 seq` 的快照已包含其折叠效果；
  更早的快照 + 尾部重放同样会经过该事件——两条路径收敛到同一结果，这正是
  「压缩/fork/恢复/回放共用一条代码路径」成立的机械原因。
- `session_messages` 降级后的用途：仅作 `GET /sessions/{id}/messages`
  （session/api.py:93-108）等展示读模型，允许最终一致，由投影异步维护或直接改为
  从事件表投影生成；不再被 `load_context*` 读取。

**风险提示（如实记录）**：第 1 层内存投影与日志的一致性正是 Q5 断言要守的对象；
若跳过第 1 层、每轮走第 2 层冷路径，性能也可接受（尾部几百行 ≈ 0.5 s/轮，
与现状 `load_context_for_turn` 同量级），实现可先做纯冷路径拿正确性，再加热路径。

---

## 4. Q3：单写者接管——design 5.5 的 advisory lock 断言不成立

### 4.1 两种 advisory lock 的官方语义

PostgreSQL 官方文档 13.3.5（Advisory Locks）原文要点：

- 会话级（`pg_advisory_lock`）：「Once acquired at session level, an advisory lock is held until
  **explicitly released or the session ends**. Unlike standard lock requests, session-level advisory
  lock requests **do not honor transaction semantics**: a lock acquired during a transaction that is
  later rolled back will still be held following the rollback」。
- 事务级（`pg_advisory_xact_lock`）：「automatically released at the end of the transaction, and
  there is no explicit unlock operation」。
- 另有共享内存上限告诫（max_locks_per_transaction × max_connections）。

### 4.2 会话级锁在本项目被两层机制击穿

**第一层：事务模式 pooler（部署形态，§1.2）。** PgBouncer 官方 features 页的 SQL 特性表：
「Session-level advisory locks — Session pooling: Yes; **Transaction pooling: Never**」。
机理：客户端眼中的「会话」与服务端 backend 会话解绑，锁落在某个任意 backend 上；
事务结束后该 backend 被还回池子。若 pooler 未配 `server_reset_query`（事务模式默认不跑
`DISCARD ALL`；而 `DISCARD ALL` 官方定义里恰好包含 `SELECT pg_advisory_unlock_all()`），
这把锁会**永久滞留在某个 backend 上**，既保护不了任何人，也可能永远无法再被获取。

**第二层：SQLAlchemy 连接池的归还语义（即使直连也存在）。** SQLAlchemy 官方 pooling 文档
Reset On Return 一节：「The pool includes "reset on return" behavior which will call the
**rollback()** method of the DBAPI connection when the connection is returned to the pool」。
而 4.1 引文明示会话级锁**不受 rollback 影响**。链条：请求结束 → `AsyncSession` 关闭 →
连接带着锁回池 → 下一个借到同一物理连接的请求**静默持有这把锁**（PG 语义：同会话重复加锁
永远成功——「If a session already holds a given advisory lock, additional requests by it will
always succeed」）→ 互斥静默失效 + 锁泄漏到物理连接销毁为止。`database/engine.py:20-25`
的 engine 正是默认 QueuePool 形态，没有任何锁生命周期管理。

（本次未在真库实测会话级锁泄漏：经事务池取会话级锁有把共享库某 backend 锁死的真实风险，
违反「不得破坏现有环境」约束，故仅依据上述两份官方文档链推理，标记为**推理成立、未实测**。）

### 4.3 事务级锁可用但覆盖不了「一个 Turn」

- 实测可用：`pg_try_advisory_xact_lock` 经 `.env` 连接路径返回 true 且回滚自动释放（§1.3）。
- 但 Agent 一个 Turn 是**多事务**的：runtime/agent.py:177（用户消息先 commit）、:581（memory
  冻结 commit）、:204/:470/:490（挂起 commit）、:737（完成 commit）——CLAUDE.md 第 5 条豁免的
  分步提交正是这个形态。事务级锁在第一次 commit 时就没了，锁不住整个 Turn。
- 它的正确用途是**单事务临界区**：append 批次内「读 last_seq → 赋 seq → 插入」的串行化。
  message-db 官方 `write_message` 正是这么用的（`PERFORM pg_advisory_xact_lock(hash)` 之后
  读 stream_version、校验 expected_version、插入，全在一个事务里）。

### 4.4 现状代码已经给出正确原语（不需要 advisory lock 也能成立）

- 行锁串行化接管决策：恢复路径按 Session → Suspension 顺序 `SELECT ... FOR UPDATE`
  （runtime/agent.py:375-386 注释明言竞争者语义；session/repository.py:194-198、
  interaction/repository.py:74-96）。行锁是事务性的，经事务池安全。
- 部分唯一索引表达「每会话至多一个活跃 X」：`uq_runtime_suspensions_active_session`
  （interaction/models.py:34-41，`WHERE is_deleted = false AND status IN (...)`）。
- 乐观版本号先例：`runtime_suspensions.version` + `ck_runtime_suspensions_version`
  （interaction/models.py:33）。
- worker 认领先例：`with_for_update(skip_locked=True)`（memory/repository.py:928,982）。

### 4.5 替代方案（建议写进 design 5.5）

**正确性底座 = 乐观追加（expectedVersion 模式）**：写者内存记住 `last_seq`，每次 append 赋
`seq = last_seq + 1`；`UNIQUE(session_id, seq)` 使「两个写者同时在写」必然表现为后提交者
IntegrityError。收到该冲突的写者**必须让位终止**（fencing 语义），绝不能「重试 +1」——
重试会把两份历史交错进同一条日志。这等价于 EventStoreDB/message-db 的 expected_version
冲突拒绝（message-db `write-message.sql`：`RAISE EXCEPTION 'Wrong expected version'`）。
该机制**不依赖任何锁的存活期**，天然跨事务、跨请求、跨实例、跨池化模式。

**效率层 = 租约行（避免双写者真的跑到冲突才发现）**：`session_writers(session_id UNIQUE,
instance_id, heartbeat_at, fenced_seq)` 或直接在 sessions 行上加列；接管 = 一个事务内
`SELECT ... FOR UPDATE` + 判断心跳过期 + 改租约（复用 agent.py:375-386 的既有加锁次序）。
租约只减少无谓竞争，**正确性不依赖它**——租约脑裂时兜底的仍是唯一约束。

**可选优化 = 事务级 advisory lock**：append 批次事务内 `pg_advisory_xact_lock(session_id)`
可把唯一约束冲突变成锁等待（message-db 形态）。可用（§1.3 实测）但非必需；若用，只允许
xact 变体，并在代码注释里写明禁用会话级变体的原因（本节 4.2）。

**「挂起=等 approval 事件」的接管语义不受影响**：design 5.5 的「用户批准 = append 一条事件，
任一实例可接管」在乐观追加下自然成立——接管者先冷路径投影（§3.3 第 2 层）拿到 last_seq，
再以 expectedVersion 续写；旧写者若诈尸，第一笔 append 即被唯一约束拒绝。

---

## 5. Q4：幂等追加与唯一约束

两类重复，两个约束，各司其职（与 message-db 官方 schema 完全同型：
`messages_stream UNIQUE(stream_name, position)` + `messages_id UNIQUE(id)`）：

| 重复来源 | 表现 | 防线 | 处置 |
|---|---|---|---|
| 同一逻辑事件重试（网络断开后不确定是否已提交、at-least-once 重发） | 同 `event_id`（构造期生成 UUID，重试携带同一值） | `UNIQUE(event_id)` | `INSERT ... ON CONFLICT (event_id) DO NOTHING`，静默幂等 |
| 接管重叠（两个写者各自认为自己是单写者） | 不同 `event_id`、相同 `(session_id, seq)` | `UNIQUE(session_id, seq)` | IntegrityError 上抛，本写者让位终止（§4.5） |

三条实现要点：

1. `event_id` 必须在**事件构造期**生成并随重试携带；若在写入函数内 `uuid4()`，重试会拿到
   新 id，幂等失效。对照现状缺陷：`StreamEvent` 构造期连 sequence 都没有
   （core/event_bus.py:31-32 是 emit 时注入），新词汇必须把身份提前到构造期。
2. 一个步骤内的**批量事件（如 assistant 消息 + N 个 tool/call）单事务原子提交**，
   保证读者永远看不到「有 call 无配对语境」的中间态；这也是 A4「call/result 配对」
   在崩溃窗口下仍成立的前提（合成错误结果由接管者补写，同样走乐观追加）。
3. 两类冲突必须在异常处理里**区分**：psycopg3 抛的都是 UniqueViolation，需按约束名
   （`session_events_event_id_key` vs `session_events_session_id_seq_key`）分流——
   前者吞掉继续，后者终止让位。混用会把「被接管」误当成「重试成功」。

---

## 6. Q5：「模型可见即已记录」运行时断言的落地形态

### 6.1 断言时机

锚点是现状的 runtime/agent.py:620-622：`context = history + current` 之后、
`self.llm.stream(context, ...)` 之前——新架构中即 `agent/request` waterfall 的末端、
请求真正发出**之前**。必须在发送前拦截：断言的意义是「阻止模型看见未记录的内容」，
发送后再查只能事后报告，不变量已经破了。

### 6.2 三级形态与实测成本（/tmp/agentos_fold_bench.py）

| 级别 | 做法 | 成本（实测） | 何时开 |
|---|---|---|---|
| L0 构造性保证 | 循环取 messages 的唯一入口就是 `derive_messages()`（投影输出），别无他路 | 0 | 永远（这是架构本身） |
| L1 热路径哈希断言 | 「即将发出的 messages」规范化 JSON + sha256，与内存投影的哈希比对；waterfall 改写者必须先 append 事件、投影随之更新，否则哈希必不等 | 235KB 上下文 1.20 ms；939KB 4.30 ms；4.6MB 23.3 ms——均为纯 CPU，**零额外 DB 读** | 生产常开（相对一次 LLM 调用的秒级耗时可忽略） |
| L2 全量逐字节 | 从 DB 重新拉全量日志、从头折叠、与实发请求逐字节 diff（A4 的字面形式） | 尾部读 0.5 s~全量 23 s（§1.3，网络主导） | CI + 影子采样（如每百次抽一），不进热路径 |

L1 能防住的正是 5.4 要防的对象：`agent/request` 是 waterfall（design 5.1），监听器可以改写
请求；改写而不落日志 = 模型可见但未记录。哈希比对使这类改写无法静默通过。
现状里真实存在此类改写者：hook 注入的 `additional_context`（runtime/agent.py:180-184，
event-contract §5.5 已确认「既不在流里也不在库里」）——切真源后它必须变成一条日志事件，
否则 L1 每次都会拦下它（这是特性，不是误报）。

### 6.3 失败处置

只有一种正确处置：**抛异常中断本次模型调用**（AgentException），并按缺口 5 的新契约
必发 `turn/end`（终止原因 = 不变量违规），Turn 置 failed。理由：

- 只记日志等于没有断言——A4 明文要求「断言在 CI 中以运行时不变量形式执行」（prd.md:257）；
- 对照现状反面教材：未预期异常不发任何终结事件、直接 `bus.close()`
  （runtime/agent.py:227-237，event-contract §2.8），前端只能靠断流猜测——新契约禁止此形态；
- 断言在发送前触发，此时尚未产生任何「模型已看见」的既成事实，中断是无损的；
  用户重试即走接管路径（§4.5），投影从日志重建，天然自愈。

### 6.4 热路径性能结论

L1 常开的代价 = 每次模型调用 1~5 ms CPU（正常上下文 ≤1MB）+ 0 次额外 DB 往返，
相对 LLM 调用本身（秒级）占比 <0.5%，**不构成热路径问题**。前提是坚持「哈希对内存投影比，
不回读 DB」；一旦写成「每次调用回读全量日志再比」，在本部署就是每轮 +0.5~23 s，必然被迫关掉。

---

## 7. 引证清单

| 出处 | 支撑的结论 |
|---|---|
| PostgreSQL 17 文档 Explicit Locking §13.3.5 | 会话级锁不随事务释放、持有至会话结束；事务级锁自动释放；共享内存上限 |
| PostgreSQL 文档 Sequence Manipulation Functions（Caution） | sequence 对象无法提供 gapless 序号 |
| PostgreSQL 文档 CREATE SEQUENCE | cache>1 跨会话乱序；「行锁计数表」是 gapless 的官方替代法 |
| PostgreSQL 文档 DISCARD | `DISCARD ALL` 含 `pg_advisory_unlock_all()`（池子不跑它锁就滞留） |
| PgBouncer 官方 features（SQL feature map） | 事务池化下 Session-level advisory locks = **Never**（SET/PREPARE/LISTEN 同为 Never） |
| Supabase 官方 connecting-to-postgres | 端口 6543 = transaction-mode pooler 的约定端口 |
| SQLAlchemy 2.0 官方 Pooling 文档 Reset On Return | 连接归还池只做 rollback（会话级 advisory lock 因此穿透归还而泄漏） |
| message-db 官方仓库（tables/messages.sql、functions/write-message.sql、indexes/） | 标准事件表 = bigserial 全局位 + 每流 position + UNIQUE(stream,position) + UNIQUE(id)；写入 = xact advisory lock + expected_version 冲突拒绝 |
| Marten 官方文档 Async Projections Daemon | High Water Mark：全局序号存在 gap 时读者只推进安全水位（BIGSERIAL 可见性空洞的业界标准处置） |

---

## 8. 对 design.md / prd.md 的修正建议

逐条列出：哪里、原文说了什么、为什么要改、改成什么。

### 8.1 design 5.5（design.md:340）「单写者靠 PostgreSQL advisory lock 保证」——**必须改**

- 原文：「单写者靠 PostgreSQL advisory lock 保证，而非靠进程内锁」。
- 问题：① 会话级锁被事务池（PgBouncer 官方 Never，本项目 `.env` 端口 6543 +
  engine.py:18-19 注释自证跑在事务池后）与 SQLAlchemy 归还语义（只 rollback，锁穿透）
  双重击穿，且失效是**静默**的；② 事务级锁跨不过 Turn 的分步 commit
  （agent.py:177/:581/:204/:737）。原文照做会得到一个看起来在锁、实际锁不住的系统。
- 改成：「单写者是**乐观语义**：事件表 `UNIQUE(session_id, seq)` + 写者显式赋 `seq=last+1`，
  冲突者让位（fencing）；租约行（部分唯一索引，先例 interaction/models.py:34-41）作降噪
  效率层；append 批次事务内可选 `pg_advisory_xact_lock`（仅事务级变体，message-db 同型）。
  禁用会话级 advisory lock，原因记录在 research/event-sourcing-feasibility.md §4.2」。

### 8.2 design 5.6 缺口 2（design.md:358）「seq 由 DB 单调分配而非进程内计数」——**措辞必须精确化**

- 原文：「日志事件自带 (session_id, seq, occurred_at)，seq 由 DB 单调分配而非进程内计数」。
- 问题：「由 DB 单调分配」最自然的读法是 BIGSERIAL/sequence，而这恰是错误方案：
  PG 官方明示 sequence 非 gapless 且跨会话可乱序；提交乱序造成续传游标的可见性空洞
  （§2.3）；且 DB 分配的号**检测不出双写者**（两个写者各拿各的号都能插进去）。
- 改成：「seq 由**写者**按 last_seq+1 显式赋值、由 `UNIQUE(session_id, seq)` 仲裁；
  DB 自增列仅作物理主键，不承担游标与并发控制职责」。

### 8.3 design 5.6 缺口 3 / A11 的 SSE `id:`——补一句限定

- 原文（design.md:359）：「帧带 id:＝日志 seq，客户端用 Last-Event-ID 断点续传」。
- 补充：`id:` 必须是**会话内 seq**（8.2 的产物），且续传免疫空洞的前提是单写者成立
  （会话内提交顺序=seq 顺序）。若未来引入跨会话订阅者，需按 Marten 高水位模式另行设计，
  当前范围不做。

### 8.4 design 5.4（design.md:309-329）——两处补强，方向不变

- 「session_messages/session_snapshots 降级为投影缓存：可重建、可删、可重算」（:321）：
  补上**快照带 projector_version 列**与「快照永不失效、只会落后」的判定规则（§3.3），
  否则「可重算」没有触发条件的定义；同时明确事件表不继承 Base 软删除
  （否则全局 `is_deleted=false` 过滤器徒增谓词且语义矛盾，engine.py:35-57）。
- 「`agent/request` waterfall 的末端比对…不一致即抛错」（:325-326）：明确分级——
  生产常开的是**哈希对内存投影**（1~5ms，§6.2 L1），逐字节全量对比只进 CI/影子（L2）；
  并写明失败处置 = 发送前中断 + 必发 `turn/end`（§6.3）。附带一条既有事实：
  hook 的 `additional_context`（agent.py:180-184）现在不落任何库，切真源后必须事件化，
  否则断言每次都会正确地拦下它。

### 8.5 design 5.7（design.md:366-387）——成立，补一条耦合关系

- 「服务持有 async_sessionmaker，每次操作自开工作单元」照写即可。
- 需补：sessionLog 服务的 `append()` 里「读/记 last_seq → 赋 seq → INSERT」必须在
  **同一个工作单元**内完成并原子提交批量事件（§5 要点 2），这既是 5.7 的事务边界问题
  也是 5.5 单写者机制的载体——两节应互相引用，避免实现时拆散。

### 8.6 prd R-S-5（prd.md:173-175）——补幂等与接管两半句

- 原文只说了「append-only 日志、投影、模型可见即已记录、中止补合成结果」。
- 建议补：「追加幂等（事件构造期身份 `event_id` + 双唯一约束）；并发写者由
  `UNIQUE(session_id, seq)` 仲裁，冲突者让位」。否则 R-S-5 验收时无法拒绝
  「BIGSERIAL + 无幂等」的错误实现——它同样能通过字面上的 R-S-5。

### 8.7 prd A4（prd.md:254-257）——「逐字节一致」限定作用域

- 原文：「用日志投影重建的模型历史与实际发出的请求逐字节一致；断言在 CI 中以运行时
  不变量形式执行」。
- 建议改为：「CI/影子路径逐字节一致（L2）；生产热路径以规范化哈希形式常开（L1），
  失败 = 发送前中断并必发 turn/end」。理由：逐字节全量对比要求回读全量日志，
  本部署实测 0.5~23 s/次（§1.3），若不写清分级，实现者只能在「常开但拖垮热路径」
  与「只在 CI 开、生产裸奔」间二选一。
- A4 后半（中止后 call/result 配对）不变，但补一句：合成错误结果由**接管者**按乐观追加
  补写（写者崩溃时它自己已无法补），这依赖 8.1 的接管机制成立。

### 8.8 prd A11 / R-T-5（prd.md:213, 284-285）——一句对齐

- 「实例重启不丢已落库事件」成立的载体是 DB seq 而非 Redis：Redis 总线只做实时转发，
  SSE 帧的 `id:` 与续传回放必须以 `session_events.seq` 为准（重连时先从 DB 补
  `seq > Last-Event-ID` 的事件再接实时流）。建议在 A11 里点名「续传数据源 = 事件表」，
  防止实现成「续传靠 Redis 缓存」——Redis 重启即丢，与验收语义矛盾。

### 8.9 implement P4-7（implement.md:161-162）——同步 8.1 的改写

- 原文：「PostgreSQL advisory lock 保证单写者」。改为与 8.1 一致的表述
  （乐观追加 + 租约 + 可选 xact lock），并在 P4-4 的「切换前必须先证明 A4 在真实会话上
  通过」旁边加一条前置：**先在真实部署形态下用两个并发写者做接管冲突演练**
  （唯一约束路径），这是 P4-4 不可逆性的直接对冲。

---

## 9. Caveats / 未验证清单

1. **池化模式未实测确证**（§1.2）：轻负载探测无区分力；结论按「必须假设事务池」给出，
   依据是 engine.py:18-19 注释 + 端口约定 + 官方文档，非现场抓包。
2. **会话级 advisory lock 的泄漏未实测**：经事务池实测有锁死共享库 backend 的风险，
   仅给出 PG 文档 + PgBouncer 文档 + SQLAlchemy 文档的三段式推理链（§4.2）。
3. **唯一约束冲突的接管演练未实测**：需要建表写数据，超出本次只读约束；
   psycopg3 对 UniqueViolation 按约束名分流的具体写法（§5 要点 3）未经运行验证。
4. **网络实测值仅代表当前办公网络到 119.45.22.177 的链路**（RTT 18ms、约 0.35~1MB/s）；
   若生产部署与 DB 同机房，§1.3 的绝对数值会好一到两个数量级，但「拉取行数决定投影成本、
   全量重放不可作热路径」的结论不变（同机房拉 1 万行 JSONB 仍是百 ms 级）。
5. 折叠基准用的是**模拟**新词汇的合成事件（/tmp/agentos_fold_bench.py），分支复杂度对齐
   `_messages_to_context` 但非真实实现；量级结论（微秒/毫秒级）对实现细节不敏感。
6. Supavisor（Supabase 自研 pooler）的 advisory lock 行为未找到与 PgBouncer feature map
   同等精度的官方特性表，本文以 PgBouncer 官方表为准（Supabase 专用 PgBouncer 池同样适用；
   Supavisor 事务模式的会话状态语义与之同类，未单独求证，标记未验证）。
7. 本报告基于 2026-08-15 工作区（分支 feature/unify-llm-client，含未提交改动）；
   行号以当日文件为准。
