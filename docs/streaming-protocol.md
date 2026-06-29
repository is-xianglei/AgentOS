# AgentOS 前端流式事件协议

本文档定义 AgentOS 后端通过 SSE 推送给前端的**统一事件协议**。它是后端自定义的归一化协议,
而非直接转发 Anthropic 原始 SSE 事件,目的是:隔离模型供应商差异、让前端不关心传输细节、
便于后端在同一层做持久化与用量统计。

> 设计原则:**后端归一化 + 增量传输 + 后端用快照对账**。前端只消费本协议,不直接解析
> Anthropic 的 `content_block_delta` 等原始事件。

---

## 一、信封(每个事件公共字段)

| 字段 | 说明 |
| --- | --- |
| `type` | 事件类型(见第三节清单) |
| `sequence` | 会话内单调递增序号 —— 用于排序、去重、断线重连 resume 的锚点 |
| `session_id` | 会话 ID |
| `actor` | 事件产出者(见第二节,分"完整"与"精简"两种形态) |
| `ts` | 毫秒时间戳(可选) |

---

## 二、actor:三种 role + 两种形态

`actor` 表达"这个事件是谁产出的"。`role` 是绝对种类,不随团队层级变化。

### role 取值

| role | 谁 | `name` | 唯一键 | 有 `task`? |
| --- | --- | --- | --- | --- |
| `orchestrator` | 会话主循环 | `"orchestrator"` | 固定一个 | 否 |
| `subagent` | Agent 工具派的一次性子代理 | agent_type(如 `explore`) | **`run_id`**(name 会重复) | 是 |
| `teammate` | TeamSpawn 派的持久成员 | 成员名(如 `alice`) | **`name`**(会话内唯一) | 是 |

> 层级最多两层(`orchestrator → subagent/teammate`):子代理被禁止再派子代理
> (`disallowed_tools` 含 `Agent`),因此不存在嵌套树,actor 保持扁平、无 `parent_run_id`。
> 注意:teammate / subagent 会**自己调工具**(如 Bash、SendMessage),这只是"调工具",
> 层级不变深,事件的 actor 仍是它自己。

### 两种形态(带宽关键)

不变的元信息(`task`、`team`)只在**开场事件**发一次;高频增量事件只带定位键。

```jsonc
// 完整 actor:仅在 turn_start 使用,带 task + team
"actor": {"role": "teammate", "name": "alice", "run_id": "run-7f3c",
          "task": "修复登录 bug", "team": {"id": 3, "is_lead": false}}

// 精简 actor:增量事件(*_delta、block_*)使用,只带定位键
"actor": {"role": "teammate", "name": "alice", "run_id": "run-7f3c"}
```

- `task`:简述该 run 的任务;**仅 subagent / teammate 有**,orchestrator 无;且只在开场事件出现。
- `team.is_lead`:团队内"是不是 lead"是相对角色,放此处,与 `role` 解耦
  (`SendMessage` 的 `recipient:"lead"` 是团队层寻址,不影响 `actor.role`)。

---

## 三、事件类型清单(9 种,所有 actor 通用)

**核心:思考 / 回答 / 工具调用对所有 actor 是同一套 type,只是 actor 不同。**
不存在 `subagent_thinking` 这类专属类型;前端用同一套渲染逻辑,按 actor 分面板。

| 组 | type | 关键字段 |
| --- | --- | --- |
| 生命周期 | `turn_start` | 完整 actor |
| 生命周期 | `turn_end` | `stop_reason`、`usage` |
| 内容 | `block_start` | `block.index`、`block.block_type`(thinking/text/tool_use) |
| 内容 | `thinking_delta` | `text`(思考增量) |
| 内容 | `text_delta` | `text`(正式回答增量) |
| 内容 | `block_stop` | `block.index` |
| 工具 | `tool_use` | `tool.id/name/input`(input 一次性给全,不逐字流) |
| 工具 | `tool_result` | `tool.id/name/output/is_error` |
| 控制 | `error` | `error.code/message/retriable/fatal` |

> `turn_end.usage` 含四个字段:`input_tokens` / `output_tokens` /
> `cache_creation_input_tokens`(缓存写入)/ `cache_read_input_tokens`(缓存命中)。
> 后两者用于成本核算(缓存读取计费远低于新鲜 input);供应商不返回时为 0。

> 约定:`tool_use.input` 累积完整后一次性给出,不透传 Anthropic 的 `input_json_delta`;
> 传输层噪声(`ping`、`signature_delta`、仅更新 usage 的中间 `message_delta`)在后端消化,
> 不进本协议;全量 `snapshot` 仅后端落库 / 对账用,不发前端。

---

## 四、完整事件流实例

场景:用户让 orchestrator 处理登录 bug。orchestrator 思考 → 回答 → 用 `Agent` 工具派一次性
`explore` 子代理(子代理内部也有完整的思考 / 工具 / 回答)→ 再用 `TeamSpawn` 派持久成员 alice
(alice 也有完整的思考 / 工具 / 回答,并演示一个可重试错误)→ 各自结束。`//` 为讲解,非 JSON。

```jsonc
// ═══ orchestrator 回合 ═══
{"type":"turn_start","sequence":1,"session_id":42,"ts":1719500000000,
 "actor":{"role":"orchestrator","name":"orchestrator"}}
{"type":"block_start","sequence":2,"session_id":42,"actor":{"role":"orchestrator","name":"orchestrator"},"block":{"index":0,"block_type":"thinking"}}
{"type":"thinking_delta","sequence":3,"session_id":42,"actor":{"role":"orchestrator","name":"orchestrator"},"block":{"index":0},"text":"先查代码再派人。"}
{"type":"block_stop","sequence":4,"session_id":42,"actor":{"role":"orchestrator","name":"orchestrator"},"block":{"index":0}}
{"type":"block_start","sequence":5,"session_id":42,"actor":{"role":"orchestrator","name":"orchestrator"},"block":{"index":1,"block_type":"text"}}
{"type":"text_delta","sequence":6,"session_id":42,"actor":{"role":"orchestrator","name":"orchestrator"},"block":{"index":1},"text":"我让 explore 查一下。"}
{"type":"block_stop","sequence":7,"session_id":42,"actor":{"role":"orchestrator","name":"orchestrator"},"block":{"index":1}}
{"type":"tool_use","sequence":8,"session_id":42,"actor":{"role":"orchestrator","name":"orchestrator"},"block":{"index":2},
 "tool":{"id":"toolu_01EX","name":"Agent","input":{"agent_type":"explore","prompt":"定位登录校验代码","description":"查登录代码"}}}

// ═══ subagent explore:完整的 思考 → 工具 → 回答(唯一键 run_id)═══
{"type":"turn_start","sequence":9,"session_id":42,
 "actor":{"role":"subagent","name":"explore","run_id":"run-1a2b","task":"定位登录校验代码"}}
{"type":"block_start","sequence":10,"session_id":42,"actor":{"role":"subagent","name":"explore","run_id":"run-1a2b"},"block":{"index":0,"block_type":"thinking"}}
{"type":"thinking_delta","sequence":11,"session_id":42,"actor":{"role":"subagent","name":"explore","run_id":"run-1a2b"},"block":{"index":0},"text":"先 grep session 校验。"}
{"type":"block_stop","sequence":12,"session_id":42,"actor":{"role":"subagent","name":"explore","run_id":"run-1a2b"},"block":{"index":0}}
{"type":"tool_use","sequence":13,"session_id":42,"actor":{"role":"subagent","name":"explore","run_id":"run-1a2b"},"block":{"index":1},
 "tool":{"id":"toolu_0GR","name":"Bash","input":{"command":"grep -rn validate_session auth/"}}}
{"type":"tool_result","sequence":14,"session_id":42,"actor":{"role":"subagent","name":"explore","run_id":"run-1a2b"},
 "tool":{"id":"toolu_0GR","name":"Bash","output":"auth/session.py:42: def validate_session(...)","is_error":false}}
{"type":"block_start","sequence":15,"session_id":42,"actor":{"role":"subagent","name":"explore","run_id":"run-1a2b"},"block":{"index":2,"block_type":"text"}}
{"type":"text_delta","sequence":16,"session_id":42,"actor":{"role":"subagent","name":"explore","run_id":"run-1a2b"},"block":{"index":2},"text":"校验在 auth/session.py:42。"}
{"type":"block_stop","sequence":17,"session_id":42,"actor":{"role":"subagent","name":"explore","run_id":"run-1a2b"},"block":{"index":2}}
{"type":"turn_end","sequence":18,"session_id":42,"actor":{"role":"subagent","name":"explore","run_id":"run-1a2b"},"stop_reason":"end_turn","usage":{"input_tokens":820,"output_tokens":55,"cache_creation_input_tokens":0,"cache_read_input_tokens":0}}
// ── explore 的结果回到 orchestrator(actor = 发起方)──
{"type":"tool_result","sequence":19,"session_id":42,"actor":{"role":"orchestrator","name":"orchestrator"},
 "tool":{"id":"toolu_01EX","name":"Agent","output":"{\"report\":\"校验在 auth/session.py:42,缺空值判断\"}","is_error":false}}
// ── orchestrator 派 teammate alice ──
{"type":"tool_use","sequence":20,"session_id":42,"actor":{"role":"orchestrator","name":"orchestrator"},"block":{"index":3},
 "tool":{"id":"toolu_02SP","name":"TeamSpawn","input":{"name":"alice","role":"coder","prompt":"修复 auth/session.py:42 空值判断"}}}
{"type":"tool_result","sequence":21,"session_id":42,"actor":{"role":"orchestrator","name":"orchestrator"},
 "tool":{"id":"toolu_02SP","name":"TeamSpawn","output":"{\"name\":\"alice\",\"status\":\"working\"}","is_error":false}}

// ═══ teammate alice:完整的 思考 → 工具(含错误)→ 回答(唯一键 name)═══
{"type":"turn_start","sequence":22,"session_id":42,
 "actor":{"role":"teammate","name":"alice","run_id":"run-7f3c","task":"修复 auth/session.py:42 空值判断","team":{"id":3,"is_lead":false}}}
{"type":"block_start","sequence":23,"session_id":42,"actor":{"role":"teammate","name":"alice","run_id":"run-7f3c"},"block":{"index":0,"block_type":"thinking"}}
{"type":"thinking_delta","sequence":24,"session_id":42,"actor":{"role":"teammate","name":"alice","run_id":"run-7f3c"},"block":{"index":0},"text":"加 None 判断。"}
{"type":"block_stop","sequence":25,"session_id":42,"actor":{"role":"teammate","name":"alice","run_id":"run-7f3c"},"block":{"index":0}}
{"type":"tool_use","sequence":26,"session_id":42,"actor":{"role":"teammate","name":"alice","run_id":"run-7f3c"},"block":{"index":1},
 "tool":{"id":"toolu_03BA","name":"Bash","input":{"command":"pytest tests/test_login.py"}}}
{"type":"error","sequence":27,"session_id":42,"actor":{"role":"teammate","name":"alice","run_id":"run-7f3c"},
 "error":{"code":"TOOL_TIMEOUT","message":"Bash 超时(120s)","retriable":true,"fatal":false}}
{"type":"tool_use","sequence":28,"session_id":42,"actor":{"role":"teammate","name":"alice","run_id":"run-7f3c"},"block":{"index":2},
 "tool":{"id":"toolu_04SM","name":"SendMessage","input":{"sender":"alice","recipient":"lead","content":"已修复,重试测试中","type":"message"}}}
{"type":"tool_result","sequence":29,"session_id":42,"actor":{"role":"teammate","name":"alice","run_id":"run-7f3c"},
 "tool":{"id":"toolu_04SM","name":"SendMessage","output":"{\"id\":91,\"recipient\":\"lead\"}","is_error":false}}
{"type":"block_start","sequence":30,"session_id":42,"actor":{"role":"teammate","name":"alice","run_id":"run-7f3c"},"block":{"index":3,"block_type":"text"}}
{"type":"text_delta","sequence":31,"session_id":42,"actor":{"role":"teammate","name":"alice","run_id":"run-7f3c"},"block":{"index":3},"text":"修好了,已通知 lead。"}
{"type":"block_stop","sequence":32,"session_id":42,"actor":{"role":"teammate","name":"alice","run_id":"run-7f3c"},"block":{"index":3}}
{"type":"turn_end","sequence":33,"session_id":42,"actor":{"role":"teammate","name":"alice","run_id":"run-7f3c"},"stop_reason":"end_turn","usage":{"input_tokens":1840,"output_tokens":110,"cache_creation_input_tokens":0,"cache_read_input_tokens":1536}}

// ═══ orchestrator 回合结束 ═══
{"type":"turn_end","sequence":34,"session_id":42,"actor":{"role":"orchestrator","name":"orchestrator"},"stop_reason":"tool_use","usage":{"input_tokens":2310,"output_tokens":230,"cache_creation_input_tokens":2048,"cache_read_input_tokens":0}}
```
---

## 五、前端渲染心智模型

- 按 **actor 唯一键**分面板:orchestrator 固定一个;subagent 用 `run_id`;teammate 用 `name`。
- 面板内事件处理:
  - `turn_start` → 建面板,读 `task` 作标题;
  - `block_start` → 开一段,按 `block_type` 决定样式;
  - `thinking_delta` → 灰色可折叠思考区;
  - `text_delta` → 正文(累加,`accumulated += text`);
  - `tool_use` / `tool_result` → 工具卡片(input/output);
  - `error` → 错误提示(按 `retriable`/`fatal` 决定样式);
  - `turn_end` → 收尾,可展示 `usage`。
- subagent / teammate 面板默认可折叠(它们是 orchestrator 的下属产出)。orchestrator 面板里
  `tool_result` 中 `name:"Agent"` / `"TeamSpawn"` 那条,与被派出的子面板形成视觉关联。

---

## 六、实现备注

- 子代理 / teammate 与主循环**共用同一套精细 emit 逻辑**,只是带各自的 actor:
  即 `block_start` / `thinking_delta` / `text_delta` / `tool_use` / `block_stop`,而非粗粒度
  的单一 `subagent_run_delta`。
- 所有产出者把事件 emit 到**同一个会话级 bus**(`StreamBus`),SSE 端单点消费;`sequence` 在该 bus
  上统一分配,保证多产出者并发下的全序。
- 后端用 SDK 便利事件的 `snapshot` / 最终 `Message` 做落库与对账,**不**把 `snapshot` 发前端。

> 状态:协议已定稿。落地需改造 `SubAgentRunner`(`run` / `run_teammate`)的 emit 与
> `app/llm/client.py` 的事件解析,使三类 actor 输出统一精细事件。


