# Workflow 编排引擎

## 目标与用户价值

让用户在页面上创建可复用的 Workflow：选择一种内置编排模式、勾选若干已定义的
Agent、拖拽排序，保存后可反复运行。解决当前"多 Agent 协作只能在单次会话里
临时组队（team 域），无法沉淀成模板复用"的问题。

## 范围

### 在范围内
- Workflow 定义的建表与 CRUD。
- 三种内置编排模式的**运行时实现**（不止配置态）：sequential / parallel / llm_routing。
- 复用现有发送消息接口作为运行入口，复用现有 SSE 事件链路。

### 范围外
- MCP 接入（本仓库尚无此概念，见 F2，属独立大功能）。
- 前端页面实现（本仓库无前端工程，只交付后端契约）。
- team 域改造：不修改 teams / team_members / team_messages 三张表。
- Evaluator-optimizer、Orchestrator-workers 等其余编排模式。
- Workflow 嵌套（workflow 内调用另一个 workflow）。

### 已接受的代价
用户明确选择"workflow 独立执行，不碰 team"：workflow 自己管运行状态，
并发闸门、状态机、事件推送需另起一套，长期与 team 域并行维护。
缓解措施是执行单元仍复用 `SubAgentRunner`（见 F3），重复面只压在编排层。

## 关键决策（用户已确认）

### D1 复用 POST /api/sessions/messages 作为唯一运行入口
不新增独立运行接口，在现有发送消息接口的请求体上增加可选 `workflow_id`：
有值即表示本次走 Workflow 编排，按 id 查出模式后分发到对应编排 Service。

支撑证据：
- `SessionSendMessageRequest`（`session/schemas.py:8`）已有 session_id、agent_id、
  content 三个字段，**session_id 可不传，接口自动创建新会话**——
  故无需另造"影子会话"承载 workflow 运行。
- `session/api.py:46` 的 send_message 已是 StreamingResponse + format_sse，
  加一个分发分支即可复用整条 SSE 链路，无需新建推送通道（呼应 F6）。
- `AgentRuntime.run()`（`runtime/agent.py:129`）采用生产者-消费者：后台任务跑主
  流程并 emit 到 bus，本协程从 bus 抽干 yield 给 SSE。编排 Service 沿用同一形状
  即可与现有前端事件契约兼容。
- 现有约束可直接对标：`session/api.py:69` 已有"已有会话不能切换Agent"的 409 校验，
  workflow 侧需要一条同构规则。

### D2 会话锁定 workflow，每轮重跑
`sessions` 表新增 `workflow_id` 列（FK -> workflows.id）。首轮带 workflow_id
创建会话后，该会话此后每一轮都执行同一个 workflow；再传入不同的 workflow_id
返回 409，与 `session/api.py:69` 的"已有会话不能切换Agent"同构。

推论：
- **运行必然是流式的**——复用 send_message 即复用其 StreamingResponse。
- `workflow_id` 与 `agent_id` 语义互斥：workflow 会话的 agent_id 留空
  （呼应 F7），二者同时传入应拒绝。
- 追问会重跑整个编排，成本较高，需在编排 Service 的中文注释里写明该语义。

### D3 特殊角色全部内置，关联表不设 role 列
汇总与路由不由用户勾选的 Agent 担任，而是各编排 Service 内部自己调 LLM 完成：
- **Parallel**：并发跑完所有成员后，Service 内部调 LLM 把多份结果汇总成单一输出
  （非纯文本拼接）。
- **Routing**：Service 内部调 LLM 做路由决策，以各成员的 `agents.description`
  作为判断依据——与 `SubAgentSpec.when_to_use`（"给主代理看的路由依据"，
  `tools/subagents/definition.py:57`）的既有设计思想一致。

推论：
- `workflow_agents` **只需 sort_order，不设 role 列**，前端交互回到最简形态
  「勾选 + 拖拽排序」。所有成员在关联表中地位对等，模式差异全部落在编排 Service。
- 编排层的 LLM 调用必须走 `create_llm_client(scenario=...)` 工厂（项目规约），
  而 `_KNOWN_SCENARIOS`（`llm/factory.py:18`）是七值硬白名单，拼错即抛错、
  不静默回落。故需**新增 `workflow` 场景**（见 R5）。汇总与路由共用该场景，
  未来若需分化再拆。
- 各编排 Service 的 `__init__` 需接受 `llm: BaseLLMClient | None = None`，
  未注入时回落工厂，便于测试注入替身（项目规约）。

### D4 Sequential 传递「原始问题 + 上一个 Agent 的输出」
第 N 个 Agent 收到的输入 = 用户原始提问 + 第 N-1 个 Agent 的输出，而非纯管道。

理由与代价：
- 纯管道（只传上一个输出）在链条中段会丢失用户意图，Agent 越靠后越容易跑偏；
  带上原始问题后每个成员都知道"这一轮到底在解决什么"，上下文开销恒定。
- 不采用"累积全部前序输出"：上下文随链长线性膨胀，长链条会撞
  `llm_default_max_tokens`，而质量收益仅在成员需互相参照时才体现。
- 第 1 个 Agent 的输入退化为仅原始问题（无前序输出），是同一规则的自然边界，
  不需要特例分支。
- 拼装格式在编排 Service 内以常量模板固化，不做成用户可配项。

### D5 失败语义按模式区分，全部显式抛错
- **Sequential**：任一成员失败即整体失败并中止后续成员——下游拿不到输入，
  继续执行无意义。
- **Parallel**：单个成员失败不中止其余成员，记录该成员的错误，汇总阶段只对
  成功结果做合并；**全部成员失败**才判整体失败。
- **LlmRouting**：路由决策失败或被选中成员失败，均为单点失败即整体失败。

三者一律 `raise AgentException.message("中文消息", details)`，不静默兜底、
不返回空结果冒充成功；异常详情只进服务端日志，不透出到 API 响应。

## 已确认事实（代码证据）

### F1 Agent 定义已完备，且已能作为主 Agent 运行
- `agents` 表（`agent/models.py:20`）：name、description、system_prompt、
  model_name、is_enabled，workspace 级唯一（软删除条件唯一索引）。
- 绑定表：`agent_tools`（`agent/models.py:78`）、`agent_skills`（`agent/models.py:103`），
  均 M:N，外键 `ondelete="RESTRICT"`——被引用的 tool/skill 不可删。
- `AgentService.get_runtime_config()`（`agent/service.py:187`）已把 AgentRecord
  解析成 `AgentRuntimeConfig`：system_prompt、model_name、tool_runtime_names、
  skill_ids、skill_names，并在存在 skill 绑定时自动追加
  Skill / SkillResource / SkillRun 三个工具。
  **workflow 直接复用此方法，不得自行解析 Agent 配置。**
- `AgentService.require_runnable()`（`agent/service.py:180`）对停用 Agent 抛 409，
  已被 `session/service.py:338,353` 调用，证明自定义 Agent 当前已能作为会话主 Agent 运行。

### F2 用户设想中两项关联并不存在
- **MCP：全库零命中**，本仓库没有任何 MCP 概念。接入 MCP 属独立大功能
  （连接管理、工具发现、生命周期），不纳入本任务。
- **prompt 不是关联表**：`prompt/` 域只有 compose.py / context.py / sections.py，
  无 ORM。Agent 的提示词是 `agents.system_prompt` 单个 Text 列。

### F3 执行底座可复用，但有耦合点
- `SubAgentRunner.run()`（`runtime/subagent.py:86`）docstring 明确
  "不依赖团队成员或收件箱，prompt 由调用方直接给出"——无状态，可复用。
- 但两处耦合：`session_id` 是**必填参数**；内部经
  `team.subagent_run_repository.SubAgentRunRepository` 写 `subagent_runs` 表。
- `run_teammate()`（`runtime/subagent.py:434`）深度依赖 TeamService / member.history /
  收件箱，**本任务不使用**。

### F4 桥接缺口：SubAgentSpec.agent_type 是枚举
- `SubAgentSpec`（`tools/subagents/definition.py:57`）要求 `agent_type: AgentType`
  枚举值，而自定义 Agent 是数据库动态行，无枚举可填。
- 该值被 `run()` 用作 `member_name` 写入运行记录，且属前端事件契约的一部分。
- `SubAgentSpec` 另有 `max_rounds`、`shell_access` 两个字段，`agents` 表中无对应列。
- 本任务必须在 design.md 中给出这三处的桥接方案，否则无法把自定义 Agent 送进执行器。

### F5 现成护栏：禁止递归派生
- `GLOBAL_SUBAGENT_DENIED_TOOLS`（`tools/subagents/definition.py:45`）禁止任何
  SubAgent 使用 Agent / TeamCreate / TeamSpawn 等工具。
- 对本任务是正向约束：天然防止 workflow 内的 Agent 再开 workflow 导致失控，
  「Workflow 嵌套」列为范围外因此无需额外代码。

### F6 StreamBus 是请求内实例，不是 session 频道
- `StreamBus`（`core/event_bus.py:11`）是纯 in-process 的有界 `asyncio.Queue`，
  实例本身不带 session_id：每次请求创建一个，主代理与所有并发子代理共享，
  SSE 端经 `stream()` 单点消费，满时自然背压。
- 推论：**流式必然绑定在一次 HTTP 请求的生命周期内**，不存在"发布到 session
  频道供他人订阅"的机制。后台异步跑 workflow 再让前端订阅进度，需要另建通道，
  不是复用 StreamBus 能解决的——这正是 D1 选择复用现有接口的根本原因。

### F7 sessions.agent_id 是单个 Agent
- `session/models.py:78`：`agent_id` 为 `agents.id` 外键，nullable，
  `ondelete="SET NULL"`，注释"会话绑定的Agent ID，空表示使用默认Agent"。
- 一个会话绑一个 Agent，而 workflow 有多个成员 Agent，语义直接冲突。
  故 workflow 会话的 agent_id 留空，workflow 归属另存于新增的 `workflow_id` 列（D2）。

## 需求

### R1 Workflow 定义与 CRUD
workspace 级实体，支持增删改查与启停；字段含 name、description、mode、is_enabled。
mode 为受控枚举：`sequential` / `parallel` / `llm_routing`，非法取值抛中文异常。
沿用 Base 的审计字段与软删除，不物理删除。

### R2 Workflow 与 Agent 的多对多关联
一个 Agent 可同时出现在多个 workflow 中（用户明确要求）。关联表只携带
`sort_order`（执行/展示序，见 D3）。同一 workflow 内同一 Agent 不可重复绑定。
排序按 `(sort_order, agent_id)` 复合升序，保证 sort_order 相同时次序仍然稳定。

### R3 三种编排模式的运行时
- **Sequential**：按 R2 的排序依次执行；第 N 个成员的输入 = 原始提问 + 第 N-1 个
  成员的输出（D4）；最后一个成员的输出即整体输出。
- **Parallel**：同一输入并发下发给全部成员，收齐后由编排 Service 内部调 LLM
  汇总成单一输出（D3），不是文本拼接。
- **LlmRouting**：编排 Service 内部调 LLM，以各成员的 `agents.description` 为依据
  选出恰好一个成员执行，返回其输出；未被选中的成员不执行。

三种模式的失败语义见 D5。运行入口与会话语义见 D1、D2。
成员无会话记忆：每轮重跑时成员只收到本轮用户提问（顺序模式外加上一步输出），
不注入该会话的历史消息。

### R4 模式元数据接口
提供接口返回全部内置模式的标识、中文名称与说明，供前端渲染模式选择器。
本仓库无前端工程，只交付后端契约。

### R5 新增 `workflow` LLM 场景
Parallel 汇总与 Routing 决策都需要调 LLM，必须走 `create_llm_client(scenario=...)`
工厂。新增场景需四处同步（项目「配置同步规则」）：
`llm/factory.py:18` 的 `_KNOWN_SCENARIOS`、`core/config.py` 的
`Settings.{provider}_workflow_model`、`.env`、`.env.example`。
未配置场景模型时回落 provider 主模型，两者皆空则抛中文异常，不硬编码模型名。

## 验收标准

- AC1 可创建 workflow 并绑定多个 Agent；同一 Agent 可被多个 workflow 绑定；
  重复绑定同一 Agent 被拒绝。
- AC2 关联只有 sort_order 没有 role；成员按 `(sort_order, agent_id)` 稳定排序，
  sort_order 相同时多次读取次序一致。
- AC3 Sequential 下，第 N 个成员收到的输入同时包含用户原始提问与第 N-1 个成员的
  输出；第 1 个成员只收到原始提问（D4）。
- AC4 Parallel 下全部成员并发执行（非串行），最终输出由 LLM 汇总产生，
  可通过注入 LLM 替身断言汇总被调用且入参含全部成员结果。
- AC5 LlmRouting 下仅被选中的一个成员被执行，其余成员的执行器不被调用。
- AC6 保存时校验至少 1 个成员；运行时校验模式最小成员数（parallel / llm_routing
  少于 2 个成员时拒绝运行），均抛中文 AgentException。
- AC7 成员 Agent 被停用或软删除时，运行期抛中文异常并指明是哪个成员，
  不静默跳过（复用 `AgentService.require_runnable()`）。
- AC8 首轮带 `workflow_id` 走编排；同一会话的后续轮次即使不再传 `workflow_id`
  也仍走编排（D2 会话锁定，不得静默降级为普通 Agent）；与 workflow 无关的请求
  行为与现状完全一致（回归不破）。
- AC9 已绑定 workflow 的会话再传入不同 workflow_id 返回 409；
  workflow_id 与 agent_id 同时传入被拒绝（D2）。
- AC10 失败语义按 D5 逐模式验证：Sequential 中断后续不执行；Parallel 部分失败
  仍产出汇总、全败才整体失败；LlmRouting 单点失败即整体失败。
- AC11 新增 ORM 已登记 `database/registry.py`，且有显式 upgrade/downgrade 迁移，
  表与列带中文 comment、外键显式 ondelete。
- AC12 `workflow` 场景已在四处同步（R5），`Settings` 能成功加载，
  `.env.example` 不含任何真实密钥。

## 待定问题（阻塞规划）

无。所有用户侧决策已收敛（D1–D5）。
