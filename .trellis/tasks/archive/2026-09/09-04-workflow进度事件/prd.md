# Workflow 进度事件

## 目标与用户价值

为 Workflow 编排补充**节点级进度事件**与 **agent_id 元信息**，让前端能实时渲染工作流图：
节点 = 成员 Agent，每个节点能稳定显示 running / done / failed；编排阶段（路由决策中、
并行汇总中、顺序第 N/M 步）也有事件载体。上一任务（09-02）为控制前端契约变更面主动选择
「不新增事件类型」，本任务在用户明确要求实时工作流图后**推翻该取舍**。

## 背景与现状痛点

- 成员事件的 `actor.name` 是 `wf-<Agent名>` 并截断到 120 字符
  （`runtime/workflow.py:71-77`，落 `subagent_runs.member_name String(120)`）。
  同名 Agent、或名字超长被截断后，前端无法把事件稳定映射到工作流图上的节点。
- `Actor`（`core/events.py:62-93`）只有 role / name / run_id / task / team，
  没有任何数据库主键；`subagent_actor()`（`:100-102`）也没有入口传入 agent_id。
- 编排阶段没有任何事件：`SequentialOrchestrator.run`（`runtime/workflow.py:227-257`）、
  `ParallelOrchestrator.run`（`:271-295`）、`LlmRoutingOrchestrator.run`（`:370-379`）
  只在最终产出时经 `_emit_final_text`（`:198-218`）或 `_merge`（`:327-360`）发文本块。
  「正在路由」「正在汇总」「第 N/M 步」前端一无所知。
- Parallel 的单成员失败只进 `logger.exception`（`:312-324`）后被 `gather` 吞掉，
  前端只能从该成员 `SubAgentRunner` 自发的 `error` 事件间接推断，且无法与节点关联。
- `subagent_runs` 表（`team/models.py:91-115`）没有 agent_id 列，历史运行记录同样只有
  可能重名的 `member_name`，无法反查是哪一个 Agent 定义。

## 范围

### 范围内
- `core/events.py`：新增 `EventType.WORKFLOW_PROGRESS` 与对应 payload 对象、工厂方法。
- `Actor` 扩展 `agent_id`，`subagent_actor()` 透传，`SubAgentSpec` 扩展 `agent_id`。
- `subagent_runs` 表新增可空 `agent_id` 列 + Alembic 迁移 `0023`。
- 三个编排器在关键节点发 `workflow_progress` 事件。
- 更新既有测试断言，新增覆盖各验收标准的用例。
- code-spec `workflow-orchestration.md` 与 README 回写前端契约变更。

### 范围外
- 前端实现（本仓库无前端工程，只交付事件契约）。
- 历史回放接口（按 session 拉取历史进度事件）：本任务只做实时 SSE，不做落库回放。
- 路由理由：`workflow_progress` 不携带 LLM 选中某成员的原因文本。
- `RuntimeEvent` 改造：进度事件归 `StreamEvent`，`RuntimeEvent` 不动。
- team（teammate）路径的进度：`run_teammate()` 与 `TeamService` 不发进度事件，
  `subagent_runs.agent_id` 对 team 路径保持 NULL。
- 内置子代理（Agent 工具派生的 explore / plan 等）的事件形状变更：必须零变化。

## 关键决策（用户已确认）

### D1 新增 StreamEvent 类型 `workflow_progress`
归 `StreamEvent` 而非 `RuntimeEvent`：编排阶段是**编排者的行为**，有明确 actor
（`ORCHESTRATOR_ACTOR`，`core/events.py:97`），且与编排者的 `turn_start` / `turn_end`
（`runtime/workflow.py:520-524`）处于同一 turn 内；`RuntimeEvent` 的定位是
「不属于某个 actor 的会话级控制事件」（`core/events.py:368-374`），语义不符。

### D2 payload 粒度 = 节点状态 + 编排阶段，扁平结构
- 每个成员各发 running / done / failed 一条；编排阶段（routing / merging）各发
  running / done（或 failed）一条。
- 字段扁平：`phase`、`status`、`agent_id`、`member_name`、`ordinal` / `total`
  （Sequential 用），全部为受控取值或标量。
- **不带**成员输出正文（正文已由成员自己的 `text_delta` 与编排者最终文本块承载），
  **不带**路由理由（范围外）。
- 不用 `detail: dict[str, Any]` 自由字典：前端要按字段稳定归节点，自由字典会让
  契约漂移到实现细节里，无法在类型层约束。

### D3 `Actor` 新增 `agent_id`，两种序列化形态都输出
- `Actor` 末尾追加 `agent_id: int | None = None`（frozen dataclass，默认字段追加在末尾，
  现有全部关键字构造点零改动）。
- `to_dict_full()` 与 `to_dict_compact()` **都**在非 None 时输出 `agent_id`：
  前端按增量事件（`text_delta` / `tool_use` 等走 compact 形态）归节点，只在
  `turn_start` 带会让增量事件无法归节点，等于没加。
- `subagent_actor()` 加关键字参数 `agent_id: int | None = None`。
- `SubAgentSpec`（`tools/subagents/definition.py:57`）加 `agent_id: int | None = None`，
  与 `display_name`（`:85`）同模式：内置 spec 不传、保持 None；
  `runtime/subagent.py:137` 构造 actor 时透传 `spec.agent_id`。

### D4 `subagent_runs` 新增可空 `agent_id` 列
- 列定义：FK `agents.id`，`ondelete="SET NULL"`，`nullable=True`，`index=True`，
  中文 comment。照 `sessions.agent_id`（`session/models.py:81-86`）的写法。
- 迁移 `alembic/versions/0023_subagent_run_agent_id.py`，
  `down_revision = "0022_create_workflows"`（当前 head，`alembic/versions/0022_create_workflows.py:12`）。
- `SubAgentRunRepository.create`（`team/subagent_run_repository.py:13`）加可选
  `agent_id: int | None = None`；`runtime/subagent.py:135` 透传 `spec.agent_id`。
- 内置子代理与 team 路径不传、保持 NULL：`team/service.py` 只调 `list_by_session`
  （`:165,:175`），不建运行记录，天然不受影响。

### D5 不改 `format_sse`、不加白名单
- `format_sse`（`runtime/agent.py:1851-1853`）只做 `json.dumps`，对 type 无感知，
  新事件经 `bus.emit → to_dict → format_sse` 直接透传，不需要任何白名单。
- 前端契约变更记入 code-spec `workflow-orchestration.md`（改写 :172、:221、:265 三处
  「不新增事件类型 / 成员 subagent 事件 → 编排者文本块」的记述），README 补一行
  说明 `workflow_progress` 事件。

## 已确认事实（代码证据）

### F1 事件链路无 type 白名单
`StreamBus.emit`（`core/event_bus.py:23-35`）只注入 sequence 并 `to_dict()`；
`session/api.py:110` 直接 `format_sse(event)`。新增 `EventType` 枚举值即可全链路透传。

### F2 `Actor` 是 frozen dataclass，末尾追加默认字段安全
全库 `Actor(...)` 构造点仅三处，全部关键字传参：`core/events.py:97`（ORCHESTRATOR_ACTOR）、
`:102`（subagent_actor）、`:109`（teammate_actor）。`subagent_actor` 调用点仅
`runtime/subagent.py:137` 与 `tests/test_stream_assembler.py:263`。

### F3 `SubAgentSpec` 构造点
内置四个 spec 在 `tools/subagents/registry.py:43,55,69,82`；workflow 成员在
`runtime/workflow.py:162-168`；测试 `tests/test_subagent_spec.py:26`。均关键字构造，
追加默认字段不影响。

### F4 `AgentRuntimeConfig` 已带 `agent_id`
`agent/service.py:32-41` 的 `AgentRuntimeConfig` 首字段即 `agent_id: int`，
编排器 `_execute_member`（`runtime/workflow.py:150-185`）已持有 config，
传递链无需新增查询。

### F5 现有精确断言会被新事件打破
`tests/test_workflow_orchestration.py:243-244` 断言 bus 事件**恰为**
`[BLOCK_START, TEXT_DELTA, BLOCK_STOP]`；`:307` 与 `:871` 分别按 type 过滤和断言
Runtime 层的 `[session_ready, turn_start, turn_end]`（该处 orchestrator 被替身替换，
不受影响）。只有 `:244` 必须改写。

### F6 Parallel 编排层没有「成员失败」标记
`_run_member_isolated`（`runtime/workflow.py:297-325`）失败分支只 `logger.exception`
后 `raise`，由 `gather(return_exceptions=True)` 收集；编排层不发任何事件。
本任务在该分支补发 `failed` 进度事件。

### F7 现存两处与本任务方向相反的既定记述
- `runtime/workflow.py:4` 模块 docstring：「不新增事件类型」。
- `.trellis/spec/backend/workflow-orchestration.md:172`、`:221`、`:265`。
两者随本任务一并改写（AC8）。

## 需求

### R1 进度事件契约
`type = "workflow_progress"`，actor 固定为编排者；payload 键 `progress`，
字段 `phase`（member / routing / merging）、`status`（running / done / failed）、
`agent_id`、`member_name`、`ordinal`、`total`，非 None 才输出。详见 design §1。

### R2 成员事件可归节点
所有由 workflow 成员产出的 subagent 事件（turn_start、block_*、text_delta、tool_*、
error、turn_end）的 `actor` 均携带 `agent_id`。内置子代理 actor 的 `agent_id` 为 None，
序列化时省略键，事件形状与改造前逐字节一致。

### R3 三种模式的发事件点
- Sequential：每个成员执行前 `member/running`（带 ordinal/total），成功后
  `member/done`，异常 `member/failed` 后再抛错中止。
- Parallel：配置加载完成后对全部成员各发 `member/running`；成员各自成功
  `member/done`、失败 `member/failed`（其余成员继续）；gather 完成、`_merge` 前发
  `merging/running`，汇总文本产出后 `merging/done`。
- LlmRouting：`_route` 前 `routing/running`；选中后 `routing/done`（带选中 agent_id）；
  执行选中成员前后 `member/running` / `member/done`，失败 `member/failed`。
- `asyncio.CancelledError` 一律不发 `failed`，直接重抛，保持既有取消语义。

### R4 运行记录可反查 Agent
workflow 成员的 `subagent_runs.agent_id` 落成员 Agent 主键；内置子代理与 team 路径 NULL。

## 验收标准

- AC1 Sequential 三成员：bus 上每个成员各有一条 `member/running` 与 `member/done`，
  `ordinal` 依次 1/2/3、`total` 恒为 3，`agent_id` 与 `member_name` 对应正确；
  running 先于该成员执行器被调用，done 晚于执行器返回。
- AC2 Parallel 三成员一个失败：派发前三条 `member/running` 均已发出；失败成员恰一条
  `member/failed`，其余两成员各一条 `member/done`；`merging/running` 在全部成员
  终态之后、汇总 LLM 调用之前；`merging/done` 在汇总文本之后；汇总正常返回。
- AC3 LlmRouting：`routing/running` 早于 `complete_structured` 调用；`routing/done`
  的 `agent_id` 等于选中成员；只有选中成员有 `member/running` 与 `member/done`，
  未选中成员零条进度事件。
- AC4 workflow 成员事件的 `actor` 在 `to_dict_full` 与 `to_dict_compact` 都含
  `agent_id`；内置 spec 的 `subagent_actor` 序列化结果不含 `agent_id` 键，
  `tests/test_stream_assembler.py:274` 的既有断言原样通过。
- AC5 `SubAgentRunRepository.create(agent_id=...)` 落库；`runtime/subagent.py` 透传
  `spec.agent_id`；内置 spec 与 team 路径的运行记录 `agent_id` 为 NULL。
- AC6 迁移 `0023_subagent_run_agent_id` upgrade 后 `subagent_runs.agent_id` 列、外键、
  索引存在；downgrade 后全部消失；`alembic upgrade head && downgrade -1 && upgrade head`
  跑通。
- AC7 `tests/test_workflow_orchestration.py:244` 改为过滤掉 `WORKFLOW_PROGRESS` 后断言
  文本块序列；全量 `uv run pytest -q` 通过。
- AC8 code-spec `workflow-orchestration.md` :172、:221、:265 三处「不新增事件类型」记述
  改写为新契约，并补 `workflow_progress` 的 payload 表；`runtime/workflow.py:4`
  docstring 同步；README 补一行。

## 风险

| 风险 | 影响 | 缓解 |
|---|---|---|
| 前端对未知 `type` 不容错 | 旧前端收到 `workflow_progress` 崩溃 | 契约写入 code-spec 与 README；本仓库无前端，属对外告知义务 |
| `Actor` 改动波及全部事件产出者 | 主循环 / teammate / 内置子代理事件形状意外变化 | 末尾默认字段 + 非 None 才序列化；AC4 逐字节断言 |
| 进度事件与成员自发事件顺序交错 | 前端状态机看到 done 早于成员 turn_end | 进度事件由编排器在 `await run()` 返回后才发，bus 单队列保证 FIFO |
| Parallel 的 `failed` 与成员自发 `error` 重复 | 前端重复报错 | 语义分层：`error` 是成员自述，`failed` 是节点状态；code-spec 写明 |
| 迁移链断裂 | 多人并行开发 head 冲突 | 明确 `down_revision = "0022_create_workflows"`，实施前 `alembic heads` 复核 |

## 待定问题（阻塞规划）

无。所有用户侧决策已收敛（D1–D5）。
