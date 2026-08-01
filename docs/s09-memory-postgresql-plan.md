# AgentOS PostgreSQL Memory 设计与实施方案

## 1. 设计范围

本文只基于以下材料：

- `s09_memory/README.md`
- `s09_memory/code.py`
- `memory-overview.svg`
- `memory-subsystems.svg`
- AgentOS 当前实际代码

本文不引用、不复用仓库内已有的 Memory 设计文档。

目标不是把 `.memory/*.md` 简单改成数据库行，而是在不使用本地文件的前提下，完整保留 s09 的四个行为契约：

1. 精简记忆目录常驻 SYSTEM，并在同一用户 Turn 内保持不变。
2. 相关记忆正文按需选择，最多 5 条，只临时注入当前模型请求。
3. 仅在真正的终轮结束后，从压缩前原始消息中提取长期记忆。
4. 通过低频 Dream 对记忆去重、合并、解决矛盾和剪枝。

v1 不把向量检索作为必要条件。s09 的核心选择器是 `name + description` 的 LLM side-query，PostgreSQL 词法检索只负责失败降级。向量召回只能作为后续大规模增强，不能替换该选择器。

## 2. 结论

推荐使用“版本化 Memory Space + 不可变 Revision + Turn 冻结上下文 + PostgreSQL Job”架构。

```mermaid
flowchart LR
    U[用户消息] --> T[Session Turn]
    T --> C[查询生成 Catalog]
    C --> S[LLM Side-query]
    S --> R[最多 5 条 Revision]
    R --> A[请求期临时注入]
    A --> L[主 LLM 与工具循环]
    L --> E[终轮完成]
    E --> J[Extraction Job]
    J --> X[提取与去重]
    X --> M[(PostgreSQL Memory)]
    M --> C
    M --> D[Dream Job]
    D --> M
```

PostgreSQL 是唯一事实源。Memory 正文、Turn 目录快照、选择结果、来源、修订、任务、锁和审计全部入库；Redis、MinIO、本地目录和进程内缓存都不是 Memory 的必要依赖。

## 3. s09 到 PostgreSQL 的等价映射

| s09 文件语义 | PostgreSQL 等价物 | 必须保持的行为 |
|---|---|---|
| `.memory/` | `memory_spaces` | 每个用户和工作空间拥有独立记忆边界 |
| `*.md` | `memory_items + memory_revisions` | `name/description/type/body` 无损保存 |
| YAML frontmatter | 强类型列 | `type` 只能是四种固定值 |
| 文件名/slug | `memory_key` | Space 内唯一，禁止静默覆盖 |
| 文件 mtime | `updated_at` | 用于稳定排序和时效标注 |
| `MEMORY.md` | 查询 `memory_items` 后生成目录，并保存到 `turn_memory_contexts.rendered_catalog` | 是查询投影，不是独立事实表 |
| 读文件正文 | 按 Revision ID 批量查询 | 只能读取选择器候选集中的记录 |
| 文件锁 | Job Lease + 行锁 | 崩溃后可恢复，多实例不重复执行 |
| 全量覆写 Dream | 操作集 + Revision + `catalog_version` CAS | 失败不能破坏旧记忆集合 |
| transcript | `session_messages` 原始记录 | 提取不能依赖压缩摘要 |

四类记忆严格保持 s09 定义：

| 类型 | 含义 |
|---|---|
| `user` | 用户身份、背景和稳定偏好 |
| `feedback` | Agent 应如何工作的反馈和纠正 |
| `project` | 项目背景、状态、原因和决策 |
| `reference` | 外部信息、入口和排查线索的位置 |

不要在 v1 中扩展类型。流程型知识仍由 Skill 承担，临时任务进度仍由 Session 上下文承担。

## 4. 当前 AgentOS 的前置问题

Memory 接入前必须先修复以下问题，否则会产生消息污染、上下文丢失或越权。

### 4.1 动态上下文会污染原始消息

`services/agent_runtime.py:87-90` 先执行 UserPromptSubmit Hook，再保存用户消息；`services/agent_runtime.py:535-548` 会把 `additional_context` 拼进用户文本。

Memory 不能复用这条路径。必须先原样持久化 `raw_content`，再单独构造只发送给 LLM 的消息副本。Recall 正文永远不能进入 `session_messages.content`。

### 4.2 Snapshot 会漏掉后续消息

`services/session_service.py:195-200` 发现 Snapshot 后直接返回 `snapshot.messages`，没有加载 Snapshot 之后的新消息。

必须给 `session_snapshots` 增加 `through_message_id`，上下文恢复规则改为：

```text
snapshot.messages + session_messages WHERE id > snapshot.through_message_id
```

### 4.3 缺少持久 Turn

旧实现只在 `session.extra` 保存工具审批指针，无法证明多次 ReAct、Stop continuation 和
人工交互恢复属于同一个用户 Turn。

必须新增 `session_turns`，并让同一 Turn 的所有模型调用复用完全相同的 Catalog 和相关记忆 Revision。

### 4.4 权限边界还不完整

- `SessionService.check_access()` 的 workspace 成员分支仍是 TODO。
- Repository 主要按裸 `session_id` 查询，再由上层做 Python 判断。
- JWT 中的 `workspace_id` 不能替代数据库中的实时成员校验。
- `sessions.user_id/workspace_id` 的 ORM 非空定义和 Alembic nullable 定义不一致。

v1 默认只启用“用户在当前 Workspace 下的私有 Memory Space”，避免尚未具备角色模型时开放 Workspace 共享写入。

### 4.5 Hook 没有真正 fail-open

`hooks/__init__.py:100-111` 使用 `wait_for`，但没有逐 Hook 捕获异常和超时。Memory 相关 Hook 失败时必须记录错误并放行主会话。

### 4.6 SSE 断开会取消主执行

`AgentRuntime.run()` 在流结束时会取消 producer。提取和 Dream 不能用请求内裸 `asyncio.create_task()`，必须先写 PostgreSQL Job，再由可恢复执行器处理。

## 5. 作用域设计

s09 的 `.memory/` 本质上是“某个用户在某个项目中的记忆目录”。AgentOS 当前没有 Project 实体，因此 v1 使用：

```text
Memory Space = workspace_id + user_id
```

- 四类记忆全部存入同一个 Space，忠实对应一个 `.memory/`。
- 子代理只读继承当前 Turn 已选中的记忆，不直接写主 Space。
- Workspace 共享 Memory、Team Memory 和 Agent 专属 Memory 放到后续阶段。

## 6. 数据模型

### 6.1 现有表改造

#### `session_turns`

| 字段 | 类型 | 说明 |
|---|---|---|
| `id` | UUID PK | 稳定 Turn ID |
| `session_id` | Integer FK | 所属 Session |
| `user_id/workspace_id` | Integer FK | 可信作用域 |
| `status` | String | running/awaiting_interaction/completed/failed/interrupted |
| `started_message_id` | Integer | 启动本 Turn 的用户原始请求消息 |
| `completed_message_id` | Integer nullable | 成功完成本 Turn 的最终 assistant 回复 |
| `memory_context_id` | BigInteger nullable | 冻结的 Memory 上下文 |
| `started_at` | timestamptz | AgentRuntime 开始处理本 Turn 的时间 |
| `completed_at` | timestamptz nullable | 本 Turn 成功完成的时间 |

`session_messages` 增加 nullable `turn_id`。历史数据可以为空，新消息必须有 Turn。

表中继承的 `created_at` 表示 Turn 记录创建时间，`started_at` 表示运行时真正开始处理的时间。`completed_at` 和 `completed_message_id` 只对 `status='completed'` 有值；`running/awaiting_interaction/failed/interrupted` 状态下二者都必须为空。`started_message_id` 必须指向触发该 Turn 的原始 user 消息，`completed_message_id` 必须指向最终对用户可见的 assistant 消息。

`session_snapshots` 增加非空 `through_message_id`。迁移时旧 Snapshot 需要根据生成时点或消息范围回填；不能可靠回填的旧 Snapshot 应失效重建。

### 6.2 `memory_spaces`

| 字段 | 类型 | 说明 |
|---|---|---|
| `id` | BigInteger PK | Space ID |
| `workspace_id` | Integer FK | 工作空间边界 |
| `user_id` | Integer FK | 私有所有者 |
| `catalog_version` | BigInteger | Memory 有效变更计数器 |
| `last_dream_at` | timestamptz nullable | 最近整理时间 |
| `last_scan_at` | timestamptz nullable | 扫描节流 |
| `sessions_since_dream` | Integer | Dream 会话门控 |
| `settings` | JSONB | 开关和预算 |

唯一约束建议使用部分唯一索引，允许软删除后重建 Space：

```sql
CREATE UNIQUE INDEX uq_memory_spaces_active_user_workspace
ON memory_spaces(workspace_id, user_id)
WHERE is_deleted = false;
```

`catalog_version` 不指向独立 Catalog 表。每次 active Memory 集合或 `name/description/version` 发生有效变化时，在同一事务内原子 `+1`，用于 Turn 一致性检查、Dream CAS 和可选缓存失效。

### 6.3 `memory_items`

| 字段 | 类型 | 说明 |
|---|---|---|
| `id` | BigInteger PK | 稳定逻辑身份 |
| `space_id` | BigInteger FK | 所属 Space |
| `memory_key` | String(160) | 稳定的业务标识，等价于 DB 版 slug |
| `memory_type` | String(16) | 四种固定值 |
| `name` | String(200) | 人类可读标题 |
| `description` | String(500) | 选择器最重要的摘要 |
| `body` | Text | Markdown 正文 |
| `status` | String(16) | active/superseded/archived |
| `version` | Integer | 乐观锁版本 |
| `superseded_by_id` | BigInteger nullable | 新记忆 |
| `source_kind` | String(16) | explicit/extracted/dream/manual |
| `last_used_at/use_count` | timestamptz/Integer | 使用统计 |

约束和索引：

```sql
CHECK (memory_type IN ('user', 'feedback', 'project', 'reference'));
CHECK (status IN ('active', 'superseded', 'archived'));
CREATE UNIQUE INDEX uq_memory_items_active_memory_key
ON memory_items(space_id, memory_key)
WHERE status = 'active' AND is_deleted = false;
CREATE INDEX ix_memory_catalog
ON memory_items(space_id, status, updated_at DESC, id);
```

正文建议限制为 16 KiB，description 限制为 500 字符。限制应在 Schema、Service 和数据库 CHECK 三层同时执行。

### 6.4 `memory_revisions`

每次创建、编辑、提取更新和 Dream 合并都保存不可变 Revision：

```text
id, memory_id, revision, memory_type, name, description, body,
actor_type, actor_id, run_id, created_at
```

唯一约束 `(memory_id, revision)`。Turn 注入记录 Revision ID，而不是只记 Item ID，确保历史可重放。

### 6.5 `memory_sources`

保存每条记忆的来源：

```text
memory_id, revision_id, session_id, turn_id,
from_message_id, to_message_id, source_kind, source_excerpt
```

来源消息 ID 必须再次校验属于当前用户和 Workspace。`source_excerpt` 只保留短证据，禁止复制工具大输出和敏感信息。

建议增加来源唯一约束 `(memory_id, turn_id, from_message_id, to_message_id, source_kind)`，避免同一提取结果重复关联。

### 6.6 Catalog 查询投影

Catalog 不建独立表。每个新 Turn 查询 `memory_spaces + memory_items`，在代码中生成与 s09 一致的一行一条格式：

```markdown
- [user-preference-tabs](memory:123@4) - User prefers tabs for indentation
```

查询至少返回 `space.catalog_version` 以及每条 active Memory 的 `id/version/memory_key/name/description/type`。建议使用一个 SQL 语句或同一一致性快照完成读取，避免版本号和 Item 集合来自不同提交。

```sql
SELECT
    s.catalog_version,
    m.id,
    m.version,
    m.memory_key,
    m.memory_type,
    m.name,
    m.description
FROM memory_spaces AS s
LEFT JOIN memory_items AS m
    ON m.space_id = s.id
   AND m.status = 'active'
   AND m.is_deleted = false
WHERE s.id = :space_id
  AND s.workspace_id = :workspace_id
  AND s.user_id = :user_id
ORDER BY m.memory_key, m.id
LIMIT 200;
```

目录最多 200 行、25 KiB。生成后的精确文本保存到当前 Turn 的
`turn_memory_contexts.rendered_catalog`，因此工具循环和人工交互恢复不需要重新查询，
也不需要维护全局 Catalog 历史表。

目录按 `memory_key ASC, id ASC` 确定性渲染，禁止依赖数据库默认行序。若 active 数量达到 200，必须先触发 Dream 或拒绝普通自动新增，不能静默让部分 active Memory 从目录中消失。

### 6.7 `turn_memory_contexts`

```text
id, turn_id UNIQUE, space_id, catalog_version,
selected_revision_ids, rendered_catalog, rendered_memories,
selector_status, degraded_reason, byte_count, created_at
```

该表保存本 Turn 发给模型的精确 Memory 字节。工具循环和人工交互恢复只能读取它，禁止重新选择。

### 6.8 `memory_jobs`

`memory_jobs` 负责持久任务：

```text
id UUID, job_type extract/dream, space_id, session_id, turn_id,
idempotency_key UNIQUE, status pending/running/retry/succeeded/dead,
attempts, max_attempts, available_at, lease_until, worker_id,
model, prompt_version, base_catalog_version,
started_at, finished_at, last_error_code, last_error_message, payload
```

v1 不建立独立执行记录表。`attempts` 保存累计尝试次数，`last_error_*` 只保留最近一次失败；完整重试过程通过结构化日志和指标观察。Job 只保存消息、Turn 和 Space 等引用 ID，`payload` 不复制 Memory 正文或原始对话。

## 7. 单个用户 Turn 的精确时序

1. 校验 JWT 用户及其当前 Workspace 成员身份。
2. 锁定 Session，创建 `session_turns(status=running)`。
3. 原样写入用户消息并绑定 `turn_id`，提交；此时绝不拼接 Memory。
4. 查询当前 Space 的 `catalog_version` 和 active `memory_items`，在代码中生成精确目录文本。
5. 使用当前原始输入和最近 2 条原始用户消息发起 side-query。
6. 读取最多 5 条被选 Revision，构建 `turn_memory_contexts` 并绑定 Turn。
7. 加载“历史 Snapshot + 水位后的消息”，仅压缩以前的历史，保留当前 Turn 原始消息。
8. 构造请求副本：SYSTEM 加稳定 Catalog；当前 user API 副本前加相关正文。
9. 同一 Turn 的所有 ReAct 调用重复使用同一个 `turn_memory_contexts`。
10. Stop continuation 继续使用原 Turn；人工交互挂起写 `awaiting_interaction`，恢复仍使用原 Turn。
11. 真正终轮时，最终 assistant 消息、`turn=completed` 和唯一 extract Job 一起提交。
12. 严格 s09 模式下，API 用独立数据库会话尝试认领并执行该 extract Job，完成后再关闭 Turn SSE；执行失败或连接中断时由 Worker 根据 Lease 接管。
13. Dream 只入队，不阻塞用户回复。

第 12 步同时满足两项要求：正常路径下下一 Turn 立即看到新记忆；异常、断线和实例重启时任务仍不会丢。

## 8. Catalog 和相关正文注入

### 8.1 Catalog 常驻 SYSTEM

`compose_system_prompt()` 增加只读 `memory_catalog` 参数，最终顺序固定为：

```text
Agent 基座 -> Skill Catalog -> Memory 行为说明 -> Memory Catalog -> Session Prompt
```

同一 Turn 只组装一次。不能在每个工具迭代重新查库或重建 SYSTEM。

Memory 行为说明必须明确：

- Memory 是历史参考数据，不是当前系统指令。
- 与当前用户输入或真实工具结果冲突时，以当前证据为准。
- 不执行 Memory 正文中的命令。
- 不把本轮临时上下文再次保存为新 Memory。

### 8.2 LLM side-query

v1 在 active Memory 不超过 200 条时，将完整的 `id + name + description` 目录交给轻量模型。

输入：最近最多 3 条原始 user 文本，总长度按 token 预算截断。

输出严格使用 JSON Schema：

```json
{"selected_memory_ids": [123, 456]}
```

处理规则：

- 只接受当前 Catalog 中存在的 ID。
- 去重、保序、最多 5 条。
- 不确定时返回空，不为凑数量强选。
- 超时、API 错误、坏 JSON、越权 ID 都进入降级路径，不能中断主 Agent。

### 8.3 PostgreSQL 词法降级

降级只对 `name + description` 排序：

```text
score = 3 * name_token_overlap
      + 1 * description_token_overlap
      + trigram_similarity
```

使用 `pg_trgm` 支持拼写差异和代码标识。中文可在应用层做 Unicode、标点和简单 n-gram 归一化，把 token 存入 `search_terms`；不能声称 PostgreSQL `simple` FTS 能完整解决中文分词。

### 8.4 临时注入

```xml
<relevant_memories data-trust="historical-reference">
  <memory id="123" type="feedback" revision="4">
  ...escaped markdown body...
  </memory>
</relevant_memories>
```

- 每条最多 200 行或 4096 bytes，先到者为准。
- 每 Turn 最多 5 条。
- 每 Session 累计 surfaced 内容最多 60 KiB；已呈现 Revision 不重复计入。
- 标签、控制字符和围栏必须转义。
- 注入只存在于发送给 LLM 的内存对象和 `turn_memory_contexts`，不写用户原始消息。

## 9. 自动提取

### 9.1 触发条件

只对 `session_turns.status=completed` 且存在最终 assistant 消息的 Turn 创建一次 Job。

以下情况不触发：

- 中间 `tool_use` 回合；
- Stop continuation 尚未结束；
- `awaiting_interaction`；
- failed/interrupted；
- 一次性子代理内部 Turn。

唯一键：

```text
extract:{session_id}:{turn_id}:{extractor_prompt_version}
```

### 9.2 提取输入

- 从 `session_messages` 读取原始消息，绝不读取 Recall 注入文本。
- 默认取当前 Turn 及向前最多 10 条相关消息。
- 按消息边界从最新向前截断，不使用 `dialogue[:4000]` 这种会丢最新内容的截法。
- 工具结果只提供工具名、成功状态和短摘要，不提供大段原始输出。
- 给 Extractor 当前 Catalog 的 `name + description`，用于避免重复。

### 9.3 提取输出

使用严格 Schema：

```json
{
  "memories": [
    {
      "memory_key": "user-preference-tabs",
      "type": "user",
      "name": "user-preference-tabs",
      "description": "User prefers tabs for indentation",
      "body": "User prefers tabs, not spaces...",
      "source_message_ids": [101]
    }
  ]
}
```

Worker 必须重新校验类型、长度、消息归属和敏感内容。LLM 输出不能直接拼 SQL，也不能指定其他用户或 Space。

### 9.4 去重和冲突

1. 对类型和主题进行规范化，生成稳定 `memory_key`。
2. 同一 Job 通过唯一 `idempotency_key` 保证只成功提交一次；重复来源通过 `memory_sources` 唯一约束拦截。
3. 相同 active `memory_key` 命中时不新增 Item；内容更完整时锁定 Item、写新 Revision、`version + 1`。
4. 不同 key 但语义近似的候选，由 Extractor 对照现有 `name + description` 判定为新增、更新或忽略；后续 Dream 继续处理低频语义重复。
5. 新事实明确推翻旧事实时，新建或更新目标 Revision，并把旧项设为 `superseded`。
6. 无法确定是否矛盾时不覆盖，保留旧项并记录 Job 结果供人工检查。
7. 一批候选全部校验通过后，在一个短事务中落库，并将 `memory_spaces.catalog_version` 只增加一次。

敏感信息默认拒绝自动保存，包括密码、Token、API Key、私钥、Cookie 和完整身份凭据。

## 10. Job 并发算法

### 10.1 Claim

```sql
SELECT id
FROM memory_jobs
WHERE status IN ('pending', 'retry')
  AND available_at <= now()
  AND (lease_until IS NULL OR lease_until < now())
ORDER BY available_at, id
FOR UPDATE SKIP LOCKED
LIMIT :batch_size;
```

Claim 短事务只更新 `running/worker_id/lease_until/attempts`，随后立即提交。LLM 网络调用期间不能持数据库行锁或事务。

### 10.2 Apply

1. 重新读取 Job 并验证 Lease 所有者。
2. 对被更新的 Item 执行 `SELECT FOR UPDATE`。
3. 通过 Job 幂等键、active `memory_key` 唯一约束和来源唯一约束抵御重复执行。
4. 写 Item、Revision、Source。
5. 锁定 Space，将 `catalog_version` 原子增加一次。
6. Job 标记 succeeded 后统一提交。

Worker 在 LLM 返回后、提交前崩溃时，Lease 到期后可以重试；重复执行不会重复创建 Memory。

重试建议：5 秒、30 秒、2 分钟、10 分钟、30 分钟。结构校验失败直接进入 dead，429、5xx 和超时允许重试。

## 11. Dream 整理

### 11.1 门控

同时满足以下条件才创建 Dream Job：

1. active Memory 数量至少 10。
2. 距上次 Dream 至少 24 小时。
3. 扫描节流窗口已通过。
4. 自上次 Dream 至少完成 5 个不同 Session。
5. 当前 Space 没有有效 Dream Lease。

Lease 默认 1 小时，实例崩溃后自动过期。

### 11.2 操作集而不是全量替换

Dream LLM 返回显式操作：

```json
{
  "operations": [
    {"action": "merge", "source_ids": [1, 2], "target": {}},
    {"action": "update", "item_id": 3, "expected_version": 4, "target": {}},
    {"action": "supersede", "item_id": 5, "by_item_id": 6},
    {"action": "archive", "item_id": 7}
  ]
}
```

执行步骤：

1. Claim 时记录 `base_catalog_version`，同时记录本次读取的 active Revision ID 集合。
2. 事务外调用 LLM 并校验完整操作集。
3. Apply 时锁定 Space，比较当前 Catalog 版本。
4. 版本已变化则整次结果作废并重新排队，绝不覆盖并发新记忆。
5. 版本未变化则写 Revision 和 lineage，并将 `catalog_version` 原子增加一次。

禁止照抄教学代码“先删除所有旧文件，再逐个写新文件”的方式。任何错误都必须保持旧 Catalog 完整可用。

## 12. 多租户与应用层隔离

应用层所有 Memory Repository 方法必须显式接收 `user_id + workspace_id`，SQL 的首要过滤条件必须包含这两个字段对应的 Space。

Memory 与项目现有用户系统保持一致，统一使用一条 `AGENTOS_DATABASE_URL`，隔离链路为：

- JWT 验证得到可信 `current_user.id`；
- 从已验签 Token 读取当前 `workspace_id`，不接受请求参数指定 owner；
- 每个请求实时校验用户仍是 Workspace 有效成员；
- API 将 `workspace_id + user_id` 传给 Service；
- Repository 通过 Space 或 Turn Join 同时过滤 `workspace_id + user_id`。

用户可达的 CRUD、Catalog、Recall、导出和彻底删除路径都必须遵守该边界。按裸 Item、Revision、Context 或 Job ID 查询后再做 Python 判断不合格。

后台 Worker 是内部受信执行器，复用 `AGENTOS_DATABASE_URL` 并允许全局 Claim Job；执行目标作用域只能从 `Job -> Space -> Session/Turn` 的数据库关系派生。API 就地执行单个 Job 时必须额外携带 `workspace_id + user_id` 限制 Claim，避免只凭 Job UUID 跨作用域认领。

## 13. API 和管理能力

建议提供最小管理 API：

| 方法 | 路径 | 作用 |
|---|---|---|
| GET | `/api/memories` | 分页列出当前 Space 记忆 |
| POST | `/api/memories` | 用户显式创建 |
| GET | `/api/memories/{id}` | 查看正文、来源和修订 |
| PATCH | `/api/memories/{id}` | 带 version 更新 |
| DELETE | `/api/memories/{id}` | 软删除并递增 `catalog_version` |
| POST | `/api/memories/search` | 管理页词法搜索 |

PATCH/DELETE 必须携带 `version`，版本不一致返回 409。用户必须能够纠正和删除自动记忆，否则错误记忆会永久影响后续会话。

可选增加统一 `Memory` 工具，支持 `list/search/add/update/archive`。`ToolContext` 必须先增加可信 `user_id/workspace_id/turn_id/actor`，模型输入中不能出现可任意指定的 owner ID。

## 14. 代码改造清单

### 14.1 新增文件

```text
models/memory.py
repositories/memory_repo.py
schemas/memory.py
services/memory_service.py
services/memory_recall_service.py
services/memory_extraction_service.py
services/memory_job_service.py
services/context_assembler.py
memory/worker.py
api/memories.py
tools/builtin/memory.py              # 可选
```

### 14.2 修改文件

| 文件 | 改造内容 |
|---|---|
| `models/session.py` | Turn、message.turn_id、Snapshot 水位 |
| `models/__init__.py` | 注册 Memory ORM |
| `repositories/session_repo.py` | 按作用域查询、Snapshot 增量恢复 |
| `services/session_service.py` | Turn 生命周期和 raw message 持久化 |
| `services/compact_service.py` | 只压缩历史，不吞当前 Turn；保存水位 |
| `services/agent_runtime.py` | Turn 冻结、ContextAssembler、终轮 Job |
| `llm/prompts.py` | 稳定 Catalog 和 Memory 行为说明 |
| `hooks/__init__.py` | 逐 Hook 超时、异常隔离、Turn 字段 |
| `api/deps.py` | 实时 Workspace 成员校验和可信作用域 |
| `api/router.py` | 注册 memories 路由 |
| `tools/base.py` | 可选 Memory Tool 所需身份上下文 |
| `services/tool_service.py` | 传递可信 ToolContext |
| `core/config.py` | Memory、Selector、Worker 和预算配置 |

Alembic 新迁移必须接当前唯一 Head `0011_fix_users_table_schema`。

## 15. 分阶段实施方案

### Phase 0：上下文和权限基线

1. 统一 Session ORM 与迁移的 nullable 约束。
2. 完成 Workspace 成员实时校验。
3. 新增 `session_turns` 和 `session_messages.turn_id`。
4. 新增 Snapshot `through_message_id` 并修复增量恢复。
5. 拆分 raw 用户消息和 API 临时上下文。
6. 修复 Hook fail-open。

退出条件：压缩后不丢消息；动态上下文不落原始消息；人工交互恢复复用原 Turn；
跨用户/Workspace 访问被拒绝。

### Phase 1：Memory 存储和手动管理

1. 创建 Space、Item、Revision、Source 表和索引。
2. 实现 Repository、Catalog 查询渲染和 `catalog_version` 原子递增。
3. 实现 CRUD API、软删除、版本冲突和来源查看。
4. Recall 和 Dream 灰度默认设为 0%，自动提取先以 Shadow 模式运行。

退出条件：数据库中显式创建一条 Memory 后，新 Session 能读取同一完整 Catalog；并发更新不会静默覆盖。

### Phase 2：s09 读取链路

1. 实现 Catalog SYSTEM 注入。
2. 实现 LLM side-query 和 PostgreSQL 词法降级。
3. 实现 `turn_memory_contexts` 和临时正文注入。
4. 固定同一 Turn 的选择结果，支持人工交互恢复。
5. 加入 5 条、4 KiB/条和 60 KiB/Session 预算。

退出条件：selector 失败不影响主回答；相关正文不进入消息历史；同一 Turn 多次工具调用的 Memory 字节完全一致。

### Phase 3：自动提取和可靠 Job

1. 创建 Job 表和独立 Worker。
2. 实现终轮 Job 原子写入。
3. 实现严格 Schema 提取、去重、冲突和 `catalog_version` 单次递增。
4. 实现 API 就地 Claim 的严格 s09 模式和 Worker 接管。
5. 加入重试、Lease、dead job 和指标。

退出条件：用户显式“记住”后下一 Turn 立即可见；断线和 Worker 崩溃不丢任务；重复投递不生成重复 Memory。

### Phase 4：Dream

1. 实现时间、扫描、Session 数和 Lease 门控。
2. 实现基于操作集的合并、淘汰和 supersede。
3. 实现 `catalog_version` CAS 和并发变更重排队。
4. 增加 Revision 回滚和 Dream 审计。

退出条件：Dream 期间并发新增 Memory 不会被覆盖；失败后原 active Memory 集合完整；用户偏好优先保留。

### Phase 5：生产化

1. 完成应用层跨用户和跨 Workspace 隔离测试。
2. 增加 Recall、Job、Dream、预算和拒绝访问指标。
3. 增加数据导出、彻底删除和保留策略。
4. 先影子提取，再按用户灰度 Recall，最后灰度 Dream。

#### Phase 5 生产部署约束

API、流式 Runtime、内联 Job 和独立 Worker 统一使用 `AGENTOS_DATABASE_URL`。生产部署不创建 Memory 专用数据库用户，也不依赖 PostgreSQL RLS 或事务级自定义变量。

隔离由认证依赖和 Repository 查询共同保证。任何新增的用户可达 Memory 查询都必须包含 `workspace_id + user_id`；全局 Job Claim、Retention 扫描和 Worker 专用裸 ID 查询只能保留在内部后台服务中，禁止暴露为允许客户端指定作用域的 API。

建议按下面顺序发布，每一步都先观察 `/metrics` 和 Job 终态：

1. `AGENTOS_MEMORY_EXTRACTION_SHADOW_ENABLED=true`，Recall 和 Dream 灰度保持 0%，验证提取输出结构和耗时；
2. 关闭影子提取，使新 Job 进入 active 模式，观察 Job 终态和 Catalog 变化；
3. 将 Recall 灰度从 1% 逐步提升，Dream 保持 0%；
4. Recall 稳定后逐步提高 Dream 灰度。

影子提取 Job 会冻结 `mode=shadow`，只保存候选数量和 Catalog 版本，不保存候选正文，
也不写 Item、Revision、Source 或推进 Dream。Recall 和 Dream 使用稳定用户分桶；已入队
Job 的模式不随运行时配置漂移。

## 16. 测试方案

关键集成测试必须使用真实 PostgreSQL，SQLite 无法验证 `JSONB`、`pg_trgm`、行锁和 `SKIP LOCKED`。

### 16.1 s09 行为验收

1. Session A：`I prefer using tabs for indentation, not spaces. Remember that.`
2. Session B：要求创建 Python 文件，确认 Agent 使用 tab。
3. Session C：询问用户偏好，确认能回答 tab 偏好。
4. 增加单引号偏好，确认下一 Turn 生效。
5. 触发 Compact 后重复步骤 2 和 3，行为不变。

### 16.2 污染和边界测试

- Recall 正文不出现在 `session_messages.content`。
- Extractor 输入不含 `turn_memory_contexts.rendered_memories`。
- tool_use 中间态、Stop continuation 和 `awaiting_interaction` 不创建 extract Job。
- Resume 完成后只创建一个 Job。
- 选择器最多返回 5 条，重复和越权 ID 被过滤。
- 恶意 Memory 标签不能逃逸围栏或覆盖 SYSTEM。
- Token、密码和私钥不会被自动保存。

### 16.3 并发和恢复测试

- 两个 Worker 同时 Claim 100 个 Job，每个 Job 只有一个生效结果。
- Worker 在 LLM 返回后、提交前崩溃，Lease 到期后成功恢复。
- 同一提取 Job 重放不增加 Item 数量。
- 两个 Turn 并发更新同一 key 时不会 lost update。
- Dream 基于旧 Catalog 运行时遇到新写入，CAS 失败并重排队。

### 16.4 隔离测试

- 用户 A 不能读取同 Workspace 用户 B 的私有 Memory。
- 用户伪造 Workspace claim 不能绕过成员校验。
- 用户被移出 Workspace 后立即失去 Recall 和 CRUD 权限。
- Worker 只能处理 Job 记录指定的 Space。

## 17. 验收标准

- [ ] 四种类型与 s09 一致，没有开放式扩展。
- [ ] Catalog 常驻 SYSTEM，同一 Turn 字节稳定。
- [ ] LLM side-query 基于 `name + description`，最多选择 5 条。
- [ ] side-query 失败时词法降级，主回答继续。
- [ ] 正文只临时注入，不污染原始消息、摘要和提取输入。
- [ ] 仅真正终轮创建提取任务，并从压缩前原始消息读取。
- [ ] 新 Memory 在下一 Turn 可见。
- [ ] Dream 具备完整门控、Lease、CAS 和 Revision，不能全量先删后写。
- [ ] PostgreSQL 是唯一事实源，运行时不读写 `.memory/` 或其他本地文件。
- [ ] 多实例重复执行不会产生重复 Memory 或覆盖并发新数据。
- [ ] 用户和 Workspace 隔离测试全部通过。
- [ ] 用户可以查看、纠正和删除自己的 Memory。

## 18. 不建议采用的方案

- 只建一张 `memories(JSONB)` 表：无法安全复刻修订、来源、Turn 冻结和 Dream 并发控制。
- 把所有 Memory 正文常驻 SYSTEM：破坏 s09 的索引加按需正文设计，Token 成本失控。
- 只做 pgvector top-k：偏离 s09 的 LLM selector 行为，也缺少可靠降级。
- 通过 UserPromptSubmit Hook 注入：会把 Memory 拼进原始用户消息并再次提取。
- 每个 ReAct 迭代重新召回：同一 Turn 上下文漂移，人工交互恢复无法重放。
- Stop Hook 中裸起后台协程：SSE 断开、进程重启或扩缩容会丢任务。
- Dream 先删旧数据再写新数据：任何坏输出或中途异常都会造成不可恢复的数据损坏。

最终交付顺序必须是：先修 Turn、Snapshot、raw message 和权限边界，再做 Memory 存储，然后接 Recall、Extraction 和 Dream。直接从“建 memories 表”开始会把现有上下文缺陷永久带进新系统。
