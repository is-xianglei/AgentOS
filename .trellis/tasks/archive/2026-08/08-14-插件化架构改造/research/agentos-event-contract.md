# Research: AgentOS 事件与对外契约精确清单（事件词汇映射源）

- **Query**: 为定义「Agent 生命周期事件词汇」提供当前全部事件与对外契约的精确清单
- **Scope**: internal（全仓代码走读）
- **Date**: 2026-08-15
- **仓库状态注意**: 当前分支 `feature/unify-llm-client` 处于中间态：
  `runtime/agent.py:32` 与 `runtime/compact.py:6` 仍 `from llm.client import LLMClient`，
  但 `llm/client.py` 已删除，`llm/__init__.py` 只导出 `BaseLLMClient/AnthropicClient/OpenAIClient`。
  另：`core/events.py:1` 提到的 `docs/streaming-protocol.md` 在仓库中不存在（docs/ 下只有
  `multi-agent-orchestration-design.md`），协议真源只有代码本身。

---

## 一、事件类型全集（core/events.py 全文解析）

### 1.1 两类信封

| 信封 | 定义 | 用途 |
|---|---|---|
| `StreamEvent` | `core/events.py:211-365` | actor（orchestrator/subagent/teammate）产出的生命周期、内容、工具、交互、错误事件；有强类型 `EventType` 枚举与工厂方法 |
| `RuntimeEvent` | `core/events.py:368-411` | 会话级控制事件，**不属于任何 actor**；`type` 是自由字符串（当前仅 `session_ready`、`task_snapshot`），payload 统一包在 `data` 字段里 |

两者的 `sequence` 均由 `StreamBus.emit()` 入队时注入（`core/event_bus.py:31-32`），对象构造期为 None。

### 1.2 EventType 枚举（core/events.py:28-40）

注意：docstring 写「9 种」，实际枚举有 **10 个成员**（注释滞后）。

| EventType | 值 | 携带 payload 字段 | 工厂方法 |
|---|---|---|---|
| TURN_START | `turn_start` | 仅 type/actor(完整形态)/session_id | `StreamEvent.turn_start` core/events.py:233-235 |
| TURN_END | `turn_end` | `stop_reason: str\|None`、`usage: Usage\|None` | :237-247 |
| BLOCK_START | `block_start` | `block: {index, block_type}` | :251-260 |
| BLOCK_STOP | `block_stop` | `block: {index}`（无 block_type） | :262-269 |
| THINKING_DELTA | `thinking_delta` | `block: {index}` + `text` | :273-283 |
| TEXT_DELTA | `text_delta` | `block: {index}` + `text` | :285-295 |
| TOOL_USE | `tool_use` | `block: {index}` + `tool: {id,name,input}` | :299-309 |
| TOOL_RESULT | `tool_result` | `tool: {id,name,output,is_error}`（**无 block**） | :311-315 |
| INTERACTION_REQUEST | `interaction_request` | `interaction: {request_id,suspension_id,kind,schema_version,request_payload}` | :317-329 |
| ERROR | `error` | `error: {code,message,retriable,fatal}` | :333-335 |

### 1.3 StreamEvent 完整字段（core/events.py:219-229）

```
type: EventType            # 必填
actor: Actor               # 必填
session_id: int            # 必填
block: BlockInfo | None    # index + 可选 block_type(thinking/text/tool_use, :43-48)
tool: ToolInfo | None      # id/name + 可选 input(dict) / output(str) / is_error(bool), :126-148
interaction: InteractionInfo | None   # 见 1.2 表, :151-168
usage: Usage | None        # input/output/cache_creation/cache_read tokens, :171-190
error: ErrorInfo | None    # code/message/retriable/fatal, :193-208
text: str | None           # thinking_delta / text_delta 的增量文本
stop_reason: str | None    # 仅 turn_end
sequence: int | None       # StreamBus 注入的全局单调序号
```

序列化规则（`to_dict()`, core/events.py:337-365）：
- `turn_start` 用 `actor.to_dict_full()`（带 task/team）；其余事件用 `to_dict_compact()`（只带 role/name/run_id）；
- 所有可选子对象按非 None 输出，帧里不出现 null 字段；
- **事件对象没有 timestamp 字段**——SSE 帧不含任何时间信息。

### 1.4 Actor 的三种形态（core/events.py:62-109）

| 形态 | 构造 | role | name | run_id | task | team |
|---|---|---|---|---|---|---|
| 主循环 | 常量 `ORCHESTRATOR_ACTOR`（:97） | `orchestrator` | 固定 `"orchestrator"` | 无 | 无 | 无 |
| 一次性子代理 | `subagent_actor(agent_type, run_id, task)`（:100-102） | `subagent` | agent_type 值 | `f"run-{run.id}"`（runtime/subagent.py:123，唯一键） | 派发 prompt | 无 |
| 持久队友 | `teammate_actor(name, run_id, task, team)`（:105-109） | `teammate` | 成员名（唯一键） | 本次推进的隔离 id（runtime/subagent.py:435）；唤醒失败路径为 `wake-{member.id}`（runtime/agent.py:1698） | member.prompt | `TeamRef{id,is_lead}`（:51-59） |

两种序列化形态：`to_dict_full`（:77-86，开场事件带 task/team）与 `to_dict_compact`（:88-93，高频增量只带定位键）。

### 1.5 RuntimeEvent（core/events.py:368-411）

| type | data 字段 | 工厂 | 语义 |
|---|---|---|---|
| `session_ready` | `{session_id, title, status}` | :380-390 | 前端建立 UI 的首帧；首轮运行与恢复复用同一 payload |
| `task_snapshot` | `{session_id, tasks: [TaskResponse.model_dump...], reason?}` | :392-405 | 全量任务快照，前端整体替换本地任务树；reason 仅调试/动画提示 |

序列化为 `{type, data, sequence?}`（:407-411）——与 StreamEvent 的顶层扁平结构不同，RuntimeEvent 的业务字段嵌在 `data` 里。

### 1.6 StreamBus（core/event_bus.py）

- 会话级总线，主代理与所有子代理/队友共享同一实例（`AgentRuntime.__init__` runtime/agent.py:121）；
- `emit()`（:23-35）：`_sequence += 1` 注入事件对象 → `to_dict()` → 入 `asyncio.Queue(maxsize=1000)`，满时自然背压；**close 后 emit 被静默丢弃**（:29-30）；
- `close()`（:37-42）：放入哨兵 `_DONE`，幂等；
- `stream()`（:44-50）：SSE 端单点抽干直到哨兵。sequence 是**每次 HTTP 请求内**（每个 bus 实例）从 1 递增，不跨请求持久。

---

## 二、事件产生点全集（全部 `bus.emit` 调用点）

发射点共 3 个文件：`runtime/agent.py`（orchestrator）、`runtime/subagent.py`（subagent/teammate，经包装 `_emit_event`，bus 为 None 时静默跳过 :205-209）、`task/service.py:35`（task_snapshot）。按阶段归类：

### 2.1 会话就绪（RuntimeEvent）

| 位置 | 事件 | 时机 |
|---|---|---|
| runtime/agent.py:187-189 | `session_ready` | `_produce`：用户消息+Turn 已 commit（:177）、hook 已跑之后，进 LLM Loop 前 |
| runtime/agent.py:407-409 | `session_ready` | resume：suspension 已终态（resolved/cancelled/failed）的幂等重放开场 |
| runtime/agent.py:449-451 | `session_ready` | resume：user_question decision=cancel 收尾后 |
| runtime/agent.py:479-481 | `session_ready` | resume：正常恢复（session 已置回 running 并 commit）后、续跑 LLM Loop 前 |

### 2.2 任务快照（RuntimeEvent task_snapshot）

统一出口 `TaskService.emit_snapshot`（task/service.py:25-35，bus 为 None 静默跳过；读的是当前事务内未提交数据，注释明确「乐观快照，回滚由下次 baseline 自愈」）：

| 触发点 | reason |
|---|---|
| runtime/agent.py:193（每次 run 首帧，紧跟 session_ready） | `baseline` |
| task/service.py:73（TaskService.create，经 TaskCreate 工具 tools/builtin/task.py:53） | `created` |
| task/service.py:118（TaskService.update，经 TaskUpdate 工具 tools/builtin/task.py:125） | `updated` |
| task/service.py:140（claim_task，队友认领，team/service.py:34 注入 bus） | `claimed` |

### 2.3 轮次开始（turn_start）

| 位置 | actor | 说明 |
|---|---|---|
| runtime/agent.py:568 | orchestrator | `_run_llm_loop` 开头，每个用户 Turn 一次；**resume 续跑同一 Turn 时会再次发**（恢复路径 :483 重新进 `_run_llm_loop`） |
| runtime/subagent.py:125 | subagent（带 task=prompt） | `run()` 建完 subagent_runs 记录后 |
| runtime/subagent.py:460 | teammate（带 task/team） | `run_teammate()` 排空收件箱、拼好身份提示后 |

### 2.4 模型流（translator 产出，见第三节）

原始 chunk 逐个经 `AnthropicStreamTranslator.translate()` 转协议事件后 emit：

| 位置 | 循环阶段 |
|---|---|
| runtime/agent.py:634-636 | orchestrator 主循环每次 `llm.stream()` 内 |
| runtime/agent.py:1607-1608 | `_finalize_without_tools`（达轮数上限后禁工具最后一调，沿用同一 translator 保持 block index 连续） |
| runtime/subagent.py:229-232 | subagent/teammate 的 `_stream_and_translate` |

产出事件：`block_start` / `text_delta` / `thinking_delta` / `block_stop` / `tool_use`。
**`tool_use` 事件在 content_block_stop 时一次性发出**（入参 JSON 已累积完整），不是流式增量。

### 2.5 工具结果（tool_result）

`tool_use` 由 translator 发（见 2.4），`tool_result` 全部由 runner 层发：

| 位置 | actor | 场景 | is_error |
|---|---|---|---|
| runtime/agent.py:969-980（`_execute_one`） | orchestrator | 工具真实执行成功（`tool_service.run` 返回后） | False |
| runtime/agent.py:1012-1023（`_record_tool_response`） | orchestrator | 合成结果（不执行工具）：Plan Mode 拦截（:801-811）、入参校验失败（:818-833）、Plan Mode 派非只读子代理（:837-851）、EnterPlanMode 重复/ExitPlanMode 非法（:854-887）、权限 deny（:920-929）、审批拒绝（:1231-1241）、用户取消交互（:1514-1526）、user_question 各分支回执（:1302-1310）、enter/exit_plan_mode 回执（:1330-1338 / :1383-1391） | 视场景 |
| runtime/subagent.py:235-251（`_emit_tool_result`） | subagent/teammate | 执行成功或权限拒绝（ask 在子代理坍缩为拒绝，:270-306） | 视场景 |

注意：工具执行异常（抛出）时 orchestrator **不发 tool_result 事件**——异常沿 `_produce` 上抛走 `_mark_abnormal_end`，流以无 turn_end 直接关闭告终。

### 2.6 轮次结束（turn_end）

| 位置 | actor | stop_reason | usage |
|---|---|---|---|
| runtime/agent.py:696-698 | orchestrator | translator.stop_reason（Anthropic 原生 `end_turn`/`max_tokens` 等）或 `max_iterations`（:693） | translator.last_usage（**仅最后一次 LLM 调用的用量**） |
| runtime/agent.py:410-417 | orchestrator | `interaction_resolved` / `interaction_cancelled` / `interaction_failed`（幂等重放终态） | None |
| runtime/agent.py:452-459 | orchestrator | `user_cancelled`（user_question 取消） | None |
| runtime/subagent.py:147-151 | subagent | translator.stop_reason | translator.last_usage |
| runtime/subagent.py:484-489 | teammate | translator.stop_reason | translator.last_usage |

### 2.7 挂起（interaction_request）

唯一出口 `_emit_interaction`（runtime/agent.py:1560-1573），actor 恒为 orchestrator，payload 为 `InteractionInfo{request_id, suspension_id, kind, schema_version, request_payload}`。触发点：

| 位置 | 场景 |
|---|---|
| runtime/agent.py:207 | 首次运行挂起（suspension 事务已 commit :204 之后才发布） |
| runtime/agent.py:401 | resume 时 suspension 仍 pending 的幂等重放 |
| runtime/agent.py:471 | resume 应用响应后又遇到下一项顺序交互（continue_suspension） |
| runtime/agent.py:491-492 | resume 续跑 LLM Loop 后再次挂起 |

挂起原因两类（`_execute_tool_uses` runtime/agent.py:853-918）：交互类工具（AskUserQuestion/EnterPlanMode/ExitPlanMode → kind 映射 :58-62）与权限 `ask`（kind=`tool_approval`）。

### 2.8 错误（error）

| 位置 | actor | code | retriable/fatal |
|---|---|---|---|
| runtime/agent.py:520-531 | orchestrator | `INTERACTION_RESPONSE_INVALID`（响应未落库前失败，原暂停点保留） | True/True |
| runtime/agent.py:535-546 | orchestrator | `INTERACTION_RESUME_FAILED`（响应已落库但恢复失败，可重放） | True/True |
| runtime/agent.py:1696-1707 | teammate（run_id=`wake-{member.id}`） | `TEAMMATE_WAKE_FAILED`（单个队友唤醒失败，主流程继续） | True/False |
| runtime/subagent.py:156-162 | subagent | `SUBAGENT_RUN_FAILED`（随后异常上抛给 Agent 工具） | False/True |
| runtime/subagent.py:492-498 | teammate | `TEAMMATE_RUN_FAILED` | False/True |

主循环自身的未预期异常（`_produce` runtime/agent.py:227-235、`_produce_resume` :547-548）**不发 error 事件**：
只记日志 + `_mark_abnormal_end` 落终态，然后 `finally: bus.close()`（:236-237, :549-550）——
前端只看到流突然结束，需靠「没收到 turn_end」推断异常。

### 2.9 一次典型回合的事件时序（orchestrator 视角）

```
session_ready → task_snapshot(baseline) → turn_start
→ [ block_start → (thinking_delta|text_delta)* → block_stop | tool_use ]*   # 每次 LLM 调用
→ tool_result*                                                              # runner 执行工具
→ (重复 LLM 调用与工具，block index 跨调用持续递增)
→ turn_end(stop_reason, usage)                                              # 或 interaction_request 挂起
（子代理/队友的同名事件按各自 actor 穿插在主流中，靠 actor 字段区分）
```

---

## 三、流协议翻译层（llm/types.py `AnthropicStreamTranslator`, :118-220）

有状态翻译器：**跨一个 Turn 内多次 LLM 调用共用同一实例**（runtime/agent.py:565 注释），
block index 由 Anthropic 原始事件的 `index` 透传（同一次调用内从 0 起，SDK 每次调用重置——
「持续递增」靠的是 `_open` 字典按 index 管理，跨调用不清零累计状态仅指 usage/stop_reason 与打开中的块）。
`turn_start`/`turn_end`/`tool_result` 由 runner 负责，翻译器只管中间内容（:123 注释）。

### 3.1 输入：Anthropic 原始 chunk 类型 → 输出映射（translate, :143-155）

| 原始 chunk（`llm/anthropic_client.py:62-67` 由 SDK event `model_dump` 透传） | 处理 | 产出 StreamEvent |
|---|---|---|
| `content_block_start` | `_on_block_start`（:157-168）：登记 `_OpenBlock{index, block_type, tool_id, tool_name}`；未知类型回落 TEXT（:161） | `block_start(index, block_type)` |
| `content_block_delta` + `delta.type=text_delta` | :174-177 | `text_delta(index, text)` |
| `content_block_delta` + `delta.type=thinking_delta` | :179-183 | `thinking_delta(index, thinking)` |
| `content_block_delta` + `delta.type=input_json_delta` | :184-189 累积 `json_buffer`，**不产出** | 无 |
| `content_block_delta` + `signature_delta` 等 | :190-191 噪声 | 无 |
| `content_block_stop`（tool_use 块） | `_on_block_stop`（:200-207）：`json.loads(json_buffer)`（解析失败回落 `{}`） | `tool_use(index, ToolInfo{id,name,input})`——**该块不发 block_stop** |
| `content_block_stop`（text/thinking 块） | :199, :208 | `block_stop(index)` |
| `message_final`（自定义帧） | `_on_message_final`（:210-220）：记录 `stop_reason` 与 `Usage`，**不产出事件** | 无 |
| `ping` / `message_start` / `message_delta` / 其他 | :154-155 噪声 | 无 |

### 3.2 自定义 `message_final` 帧的作用

`llm/anthropic_client.py:69-83`：SDK 流耗尽后调 `stream.get_final_message()`，追加一帧
`{type: "message_final", stop_reason, usage, content: [block.model_dump()...]}`。三个消费者：
1. runner 取 `chunk["content"]` 作为 `final_content`（runtime/agent.py:632-633 / subagent.py:229-230）——
   既是落库的 assistant 消息 content，也是 `extract_tool_uses()`（llm/types.py:82-88）的工具解析源；
2. translator 缓存 `stop_reason`/`last_usage` 供 runner 构造 turn_end；
3. `CompactService._auto_compact`（runtime/compact.py:123-125）只认这一帧拿摘要。
`llm/base.py:21-29` 声明该抽象：OpenAI 实现的原始词汇不同（choices[].delta），当前 translator 只认 Anthropic 词汇。

---

## 四、对外 SSE 契约

### 4.1 两个 SSE 端点（均为 POST + StreamingResponse，非 EventSource 标准 GET）

| 端点 | 位置 | 驱动 |
|---|---|---|
| `POST /sessions/messages`（发消息） | session/api.py:42-70 | `AgentRuntime.run()`（生产者-消费者：后台 `_produce` emit 到 bus，本协程抽干 yield，runtime/agent.py:123-146） |
| `POST /interactions/sessions/{session_id}/requests/{request_id}/resolve`（响应交互并恢复） | interaction/api.py:79-120 | 先 `prepare_interaction_response`（校验+响应落库+commit，使 409 等保持普通 HTTP 语义，:101-108），再 `AgentRuntime.resume()` |

辅助 REST（非 SSE）：`GET /interactions/sessions/{id}/pending`（interaction/api.py:62-76，返回
`PendingInteractionResponse{suspension, requests}`，interaction/schemas.py:228-230）；
`POST .../suspensions/{id}/cancel`（interaction/api.py:123-145）；
`GET /sessions/{id}/messages` 历史消息（session/api.py:93-108）。

### 4.2 帧格式（runtime/agent.py:1710-1712 `format_sse`）

```
data: {json.dumps(event, ensure_ascii=False, default=str)}\n\n
```

- **只有 `data:` 行**：无 `event:` 名（前端只能按 JSON 里的 `type` 字段分发）、无 `id:` 行；
- media_type：`text/event-stream; charset=utf-8`（session/api.py:70, interaction/api.py:120）；
- **无心跳帧**、**无显式结束帧**：流结束 = `bus.close()` 后 StreamingResponse 正常断开（agent.py:236-237）；
- **无断线重连支持**：无 `id:`/`Last-Event-ID` 机制，bus 是请求内内存队列，断线即丢事件。
  前端重建手段 = 重连后靠 `GET messages` + `GET pending` + 下次 run 首帧的 `task_snapshot(baseline)`（agent.py:191-193 注释）。

### 4.3 前端判断「一轮结束」

无结束帧，靠语义事件三选一：
1. `turn_end` 且 `actor.role == "orchestrator"`（正常完成 / 幂等重放终态 / 用户取消）；
2. `interaction_request`（本轮挂起，转入 pending 交互 UI；流随后关闭）；
3. `error` 且 `fatal=true`（恢复失败等；流随后关闭）。
兜底：连接关闭本身（异常路径可能无任何终结事件，见 2.8 末段）。
子代理/队友的 `turn_end` 不代表回合结束，必须过滤 actor。

---

## 五、落库真源与「只存在于 SSE 流」的信息清单

### 5.1 session_messages（session/models.py:136-161：id/session_id/turn_id/role/content(JSON)/token_estimate + Base 审计字段）

| 写入点 | role | content | 事务边界 |
|---|---|---|---|
| `start_turn`（session/service.py:357-376 → repository.py:210-228） | `user` | 原始用户输入字符串 | **立即 commit**（agent.py:177，流式生命周期要求先可靠提交） |
| 主循环每次 LLM 调用后（agent.py:641-646） | `assistant` | `message_final.content` 原样（Anthropic block 列表：text/tool_use/thinking 块） | flush，随挂起（:204/:470/:490）或完成（`_complete_turn` :737）commit |
| Stop hook continuation（agent.py:654-662） | `user` | hook 返回的续跑指令文本 | 同上 |
| `_execute_one`（agent.py:981-992）/`_record_tool_response`（:1024-1035） | `tool` | `ToolResultMessage.to_content_dict()`（llm/types.py:42-50：tool_use_id/tool_name/input_args/output/is_error） | 同上 |
| `_finalize_without_tools`（agent.py:1610-1615） | `assistant` | 终局答复 | 同上 |

关键性质：
- 异常中断时 `_mark_abnormal_end`（agent.py:758-781）**先 rollback**——本轮未提交的 assistant/tool 消息全部丢弃，只保留 Turn/Session 终态；
- 读侧 `load_context_for_turn`（session/service.py:432-466）+`_messages_to_context`（:468-515）把 tool 行重组为 Anthropic `tool_result` user 块；
- **只有 orchestrator 的消息进这张表**。subagent/teammate 的对话不进。

### 5.2 session_turns（session/models.py:164-254）

字段：id(UUID)/session_id/user_id/workspace_id/status/started_message_id/completed_message_id/memory_context_id/started_at/completed_at。
status 约束 5 值：running/awaiting_interaction/completed/failed/interrupted（:170-174）；completed 必须带 completed_message_id+completed_at（:175-181）。

| 状态迁移 | 写入点 | commit |
|---|---|---|
| 创建 running | `start_turn` → repository.py:301-319 | agent.py:177 |
| → awaiting_interaction | `_suspend_for_interaction` agent.py:1100（连同 session 状态 :1099） | agent.py:204/470/490 |
| → running（恢复） | agent.py:476 | :477 |
| → completed | `_complete_turn` agent.py:711-716 | :737 |
| → interrupted / failed | `_mark_abnormal_end` agent.py:773-775；取消交互 `_finish_cancelled_interaction` :1532（interrupted） | 各自路径内 commit |

### 5.3 session_snapshots（session/models.py:257-286：through_message_id/snapshot_type="compact"/messages(JSON)/summary）

唯一写入点：`CompactService.maybe_compact`（runtime/compact.py:32-57）→ L2 `_auto_compact` 超过
50k token 估算阈值时生成 LLM 摘要并 `add_snapshot`（session/repository.py:343-361）。
时机在每轮 LLM 调用前的上下文组装（agent.py:614-619），事务随主循环（挂起/完成时 commit）。
读侧 `latest_snapshot`（repository.py:363-373）取最新一条非空水位快照，`load_context*` 用
「快照 messages + 水位后消息」重建上下文。L1 micro_compact（compact.py:63-110）只改内存副本不落库。

### 5.4 其他相关表（补充落库面）

| 表 | 写入 | 备注 |
|---|---|---|
| `tool_calls`（tools/models.py:60-107） | `ToolService.run`（tools/service.py:211-222 新建/复用 → succeed :263 / fail :247）；挂起路径 `start_call`+`mark_awaiting_interaction`（agent.py:1052-1059）；审批拒绝 `reject_awaiting`（agent.py:1009 → tools/repository.py:177-184） | 状态机 running/succeeded/failed/awaiting_interaction/rejected。**三类 actor 共用**（subagent.py:282 也走 ToolService.run），但表内**无 actor/run_id 列**，无法回溯是谁调的（agent_id 指 Agent 配置而非产出者） |
| `subagent_runs`（team/models.py:91-104） | subagent.py:121 create → :145 succeed / :154 fail，独立 db session 立即 commit（:146/:155） | 只有 prompt/report/status——**无中间过程** |
| `team_members.history`（team/models.py:57） | `run_teammate` 写回完整 messages JSON（subagent.py:479-483 commit） | teammate 的对话真源（含 assistant content 块），但工具 output 以 tool_result user 块形式存于 history |
| `runtime_suspensions` / `interaction_requests` | 见第六节 | 挂起/交互真源，事件可据此重建 |
| `tasks` | TaskService（task/service.py） | task_snapshot 事件的数据源 |

### 5.5 只存在于 SSE 事件流、从未落库的信息（= 事件溯源要新增记录的信息量）

按丢失程度排序的明确清单：

1. **每次 LLM 调用的 token 用量与 stop_reason**（最实质的缺口）
   - `Usage`（input/output/cache_creation/cache_read，core/events.py:171-190）只在 `turn_end` 事件里出现一次，
     且只是**最后一次** LLM 调用的用量（translator 只保留 last，llm/types.py:210-220）；
   - 一个 Turn 内前 N-1 次调用的 usage **连事件都没有**（message_final 每次都带，但 translator 覆盖式缓存），
     只剩 anthropic_client.py:97-103 的 cache 命中日志；
   - 库中无任何 usage 列：session_turns、session_messages 都没有；`token_estimate` 仅 `len(str)//4`（session/service.py:404）。
2. **事件序号 sequence** —— bus 内存自增（event_bus.py:31-32），请求结束即消失；无表存储。
3. **事件时间戳** —— StreamEvent/RuntimeEvent 无 timestamp 字段；细粒度时序只能靠各记录表的
   created_at/started_at/finished_at 粗略近似（增量事件完全无时间）。
4. **actor 归属** —— 每帧都带 actor{role,name,run_id,task,team}，但：session_messages 无产出者列；
   tool_calls 无 actor/run_id 列（subagent 与 orchestrator 的调用在表里不可区分）；
   subagent_runs 有 id（run_id 的来源）但工具调用没有外键指向它。
5. **subagent 的全部中间过程** —— thinking/text 增量、block 结构、逐轮 assistant content、tool_use 与
   tool_result 的对应关系：subagent 的 messages 列表纯内存（subagent.py:348-379），只落 prompt+report；
   其 turn_end 的 usage/stop_reason 也不落库。
6. **turn_end.stop_reason 的语义** —— 事件里有 `end_turn`/`max_tokens`/`max_iterations`/`user_cancelled`/
   `interaction_*` 等细粒度原因；session_turns.status 只有 5 个粗状态，无 stop_reason 列。
7. **error 事件的结构化信息** —— code/retriable/fatal（INTERACTION_RESUME_FAILED、TEAMMATE_WAKE_FAILED、
   SUBAGENT_RUN_FAILED 等）只在流里+日志；session_turns.status=failed 无原因；
   tool_calls.error_message 仅覆盖工具自身失败。
8. **块级流式结构** —— block_start/block_stop 的 index 序列、thinking/text 的分块节奏。
   （orchestrator 的最终 content 块列表本身有落库，subagent/teammate 见第 5 条。）
9. **task_snapshot 的 reason**（baseline/created/updated/claimed）——任务数据落库，快照动因不落。
10. **合成 tool_result 的完整上下文** —— orchestrator 侧有落（session_messages role=tool），
    但 Plan Mode 拦截/权限 deny 这类「未执行即拒绝」在 tool_calls 表**无记录**
    （`_record_tool_response` 仅当复用 awaiting record 时才更新 tool_calls，agent.py:1007-1011；
    校验失败/Plan Mode 拦截路径 record=None，只有消息表有痕迹）。

**无缺口**（事件可由库重建）：`session_ready`（sessions 表）、`interaction_request`（interaction_requests 表）、
`task_snapshot` 的 tasks 本体（tasks 表）、orchestrator 的 `tool_use`/`tool_result`/最终文本
（session_messages + tool_calls）。

**既不在流里也不在库里**（补充）：Hook 注入的 `additional_context`（agent.py:180-184，仅注入请求副本；
挂起时会冻结进 continuation_payload，正常完成后彻底丢失）；PreToolUse hook 对入参的改写过程
（tools/service.py:206-208，改写后入参落 tool_calls，但「被改写过」这一事实无记录）。

---

## 六、挂起 / 恢复（RuntimeSuspensionRecord）

### 6.1 表结构

- `runtime_suspensions`（interaction/models.py:24-99）：id(UUID)/session_id/turn_id/
  status(pending/resuming/resolved/cancelled/failed, :62-68)/continuation_payload(JSONB)/
  resolution_policy(JSONB)/version/resolved_at。部分唯一索引保证**每会话最多一个活跃暂停点**
  （status in pending/resuming 且未删，:34-41）。
- `interaction_requests`（:102-213）：暂停点下的顺序请求，kind 4 值（tool_approval/user_question/
  enter_plan_mode/exit_plan_mode），sequence 暂停点内严格递增，冻结 request_payload、
  记录 response_payload/responded_by/responded_at，关联 tool_call_id + tool_use_id。

### 6.2 挂起时序列化了什么（`_suspend_for_interaction` runtime/agent.py:1037-1108）

continuation_payload（:1060-1074）：

```json
{
  "assistant_message_id": <发起本批 tool_use 的助手消息 id>,
  "actor": {"role": "orchestrator", "name": "orchestrator"},   // to_dict_compact
  "pending": [ {"tool_use_id", "tool_name", "input", "tool_call_id"(仅首项非空)} , ...剩余未执行工具 ],
  "done_tool_use_ids": ["已执行过的 tool_use id"],
  "additional_context": "<hook 注入的临时上下文，可为 null>"
}
```

resolution_policy 固定 `{"mode": "sequential"}`（:1088）。同时：目标工具建 tool_calls 记录并置
awaiting_interaction（:1052-1059）；request_payload 按 kind 构造（`_build_interaction_payload`
:1116-1149——tool_approval 带 tool+reason+allowed_decisions+can_update_input；user_question 透传
AskUserQuestion 入参；exit_plan_mode 带 plan 全文+版本）；session/turn 置 awaiting_interaction
（:1099-1100）。**先 commit（:204）再发 interaction_request 事件（:207）**。

### 6.3 恢复时如何重放（`_produce_resume` runtime/agent.py:349-550）

1. 响应先落库：`prepare_interaction_response`（:269-314）校验→`resolve_request`（interaction/service.py:194-235，
   全部请求 resolved 后 suspension 置 `resuming`）→commit。相同响应可幂等重放（内容不同则 409）。
2. 按 Session→Suspension 顺序重新加锁（:377-386）；按 suspension.status 分派：
   - `pending`：上次恢复已推进到下一项交互 → 只重发当前待处理 interaction_request（:396-402）；
   - 终态：只发 session_ready + turn_end(`interaction_{status}`)（:404-418）；
   - `resuming`：走 `_apply_interaction_and_continue`（:1151-1217）。
3. 应用 continuation：从 pending 反序列化 ToolUse 列表，复用挂起时的 awaiting tool_calls 记录
   （:1174-1180，避免同一调用两条记录），按 kind 应用响应（tool_approval 执行/拒绝并可带 updated_input
   :1219-1262；user_question/enter|exit_plan_mode 生成合成 tool_result :1264-1391），然后
   `_execute_tool_uses(done_ids 累积, existing_suspension 复用)` 继续剩余工具——再遇交互则
   `continue_suspension_with_request`（interaction/service.py:158-192，同一暂停点追加下一 sequence，回到 pending）。
4. 全部工具完成：suspension 置 resolved、session/turn 置 running、commit、发 session_ready，
   **重新进 `_run_llm_loop`（再次发 turn_start）**续跑，直至 turn_end 或再次挂起。

取消路径：`cancel_interaction`（runtime/agent.py:316-347）/ user_question 的 cancel 决策（:434-460）→
`cancel_suspension`（interaction/service.py:237-265）+ `_finish_cancelled_interaction`
（agent.py:1497-1533：awaiting 的 tool_calls 逐个合成拒绝 tool_result，turn 置 interrupted、session 置 idle）。

### 6.4 这段状态与事件日志的映射关系（客观对应，供新词汇设计参考）

continuation_payload 本质是「Turn 内事件日志的一个游标 + 待重放队列」：

| continuation 字段 | 事件流中的等价物 |
|---|---|
| `assistant_message_id` | 最近一个 assistant 轮（该轮 `tool_use` 事件批次）的定位 |
| `done_tool_use_ids` | 已发出 `tool_result` 事件的 tool_use id 集合 |
| `pending[]` | 已发出 `tool_use` 事件、尚未有 `tool_result` 的队列（含入参快照） |
| `additional_context` | 不存在于事件流（hook 注入的请求副本） |
| suspension.status 机 | `interaction_request` 事件（pending）→ 响应落库（resuming）→ 续跑（resolved） |
| interaction_requests 行 | `interaction_request` 事件的持久化镜像（request_payload 冻结 + response_payload） |

即：若会话事件日志成为唯一真源，挂起点可表达为「日志截断位置 + 未闭合的 tool_use 集合」，
当前 continuation 里唯一无法从既有事件推导的是 `additional_context` 与 `tool_call_id` 关联。

---

## 七、Caveats / Not Found

1. **`docs/streaming-protocol.md` 不存在**：core/events.py:1 引用的协议文档已不在仓库（docs/ 仅有
   multi-agent-orchestration-design.md），本清单以代码为准。
2. **分支中间态**：`runtime/agent.py:32`、`runtime/compact.py:6` 导入的 `llm.client.LLMClient` 模块已删除
   （feature/unify-llm-client 未完成），当前工作区代码无法直接运行；事件契约本身不受该重构影响
   （translator/chunk 词汇在 llm/types.py 与 anthropic_client.py 中保持一致）。
3. **EventType docstring 写 9 种、实际 10 个成员**（core/events.py:29）——INTERACTION_REQUEST 为后加。
4. Hook 事件（hooks/__init__.py:26-36：UserPromptSubmit/PreToolUse/PostToolUse/Stop）是**进程内扩展点**，
   不是 SSE 事件；与流事件词汇无重叠但生命周期锚点一致，设计新词汇时可作对照。
5. 未发现除 `bus.emit` / `_emit_event` / `emit_snapshot` 之外的其他事件发射包装；未发现 WebSocket 或
   其他推送通道；SSE 为唯一实时对外通道。
6. 本清单基于 2026-08-15 工作区（分支 feature/unify-llm-client，含未提交改动）。

## 附：关键文件索引

| 文件 | 角色 |
|---|---|
| core/events.py | 事件对象全集 |
| core/event_bus.py | StreamBus（sequence 注入、背压、关闭哨兵） |
| runtime/agent.py | orchestrator 全部发射点、挂起/恢复状态机、format_sse |
| runtime/subagent.py | subagent/teammate 发射点 |
| llm/types.py / llm/anthropic_client.py / llm/base.py | 翻译器、message_final、客户端抽象 |
| session/api.py / interaction/api.py | SSE 端点与交互 REST |
| session/models.py / repository.py / service.py | 三张会话表的真源 |
| interaction/models.py / service.py / schemas.py | 暂停点与交互请求 |
| tools/service.py / repository.py / models.py | tool_calls 状态机 |
| task/service.py | task_snapshot |
