# Research: Anthropic SDK 适配核对（事件 waterfall 设计 vs 实际 SDK 能力）

- **Query**: 核对 design.md 5.1 的 `agent/request` / `llm/stream` / `agent/request-error` waterfall 设计能否架在当前安装的 Anthropic SDK 上；并回答 usage 时机、tool use 增量、prompt caching、错误重试、openai_client 六个问题
- **Scope**: mixed（installed SDK 源码走读 + 官方文档参考 + 项目代码）
- **Date**: 2026-08-15

## 零、版本与证据来源

| 包 | pyproject 声明 | uv.lock 锁定 | venv 实装 | 备注 |
|---|---|---|---|---|
| anthropic | `>=0.104.1`（pyproject.toml:8） | 0.104.1 | 0.104.1（`anthropic/_version.py`） | 三处一致 |
| openai | `>=1.59.5`（pyproject.toml:9） | **3.0.0** | 3.0.0 | 实装比声明下限高两个大版本，见 Q6 |

证据来源分级（重要，先读）：

1. **已装 SDK 源码**（`.venv/lib/python3.14/site-packages/anthropic|openai`）——
   对「类名、参数、返回结构、流式事件、异常类型与当前版本兼容」这一 CLAUDE.md 要求而言是最高证据，
   下文引用记为 `SDK:<相对路径>:<行号>`。
2. **Anthropic 官方参考（缓存版）**——本次调查在会话内加载了 Anthropic 官方 claude-api
   技能的内置文档（官方发布、缓存于 2026-06），涵盖 streaming / prompt-caching /
   error-codes / models 等页，均给出 canonical URL（platform.claude.com/docs/...）。
3. **在线官方文档抓取失败**：`curl https://platform.claude.com/docs/en/build-with-claude/streaming.md`
   返回 307 → `anthropic.com/app-unavailable-in-region`（区域限制）。因此凡「仅有缓存官方参考、
   无 SDK 源码佐证」的数字（如缓存定价倍率、各模型最小可缓存 token 数），下文标注「未在线验证」。
4. 按约束未发起任何真实模型调用；流事件顺序结论来自 SDK 源码与官方文档，未经真实流量复核。

---

## Q1 waterfall 能不能包住流式调用

### 1.1 SDK 侧事实

- 异步流入口 `client.messages.stream(...)` 返回 `AsyncMessageStreamManager`
  （SDK:resources/messages/messages.py:2488-2515），它既不是协程也不是迭代器，
  必须 `async with` 进入，`__aenter__` 才 await 真正的 HTTP 请求并返回 `AsyncMessageStream`
  （SDK:lib/streaming/_messages.py:316-320）；`__aexit__` 调 `stream.close()` 关闭 HTTP 响应
  （同文件 :321-327）。
- `AsyncMessageStream` 单次消费：`until_done()` 耗尽后只剩累积快照，`get_final_message()`
  = 先 `until_done()` 再返回快照（SDK:lib/streaming/_messages.py:237-243, 266-268）。
- 低层 `AsyncStream.__stream__` 自带 `finally: response.close()`（SDK:_streaming.py，
  与同步版 :155-157 对应）——但该 finally 属于 async generator 帧，**只有 generator
  被终结（aclose/GC）时才执行**。

### 1.2 结论：能包住，且项目现有形状已经是可包的形状

现有 `AnthropicClient.stream()` 已把 `async with` + `async for` 封在一个 async generator 里，
对外暴露的就是 `AsyncIterator[dict]`（llm/anthropic_client.py:54-83）。所以 `llm/stream`
waterfall 的中间件面对的不是 SDK 上下文管理器，而是「返回 AsyncIterator 的调用」——
这正是可以层层包装的形状。可行写法：

```python
# waterfall 每层：async def mw(options, next_)，next_() 返回 AsyncIterator[chunk]
async def recording_mw(options, next_):
    async with aclosing(next_(options)) as inner:      # 关键：aclosing
        async for chunk in inner:
            record(chunk)          # 观测
            yield chunk            # 亦可在此插入/改写/吞掉事件

async def fake_mw(options, next_):
    if options.get("replay"):
        for ev in scripted_events:     # 不调 next_ 即整层替换（测试 Fake）
            yield ev
        return
    async with aclosing(next_(options)) as inner:
        async for chunk in inner:
            yield chunk
```

三种 waterfall 能力全部成立：改写 options 再调 `next`；包装返回的迭代器（流开始后插入/
替换/过滤事件就是在 re-yield 之间插自造 dict，无需 SDK 支持——SDK 高层流自己就是这个模式，
`build_events` 在 raw 事件之外合成 TextEvent/InputJsonEvent 等，SDK:lib/streaming/_messages.py:331-371）；
不调 `next` 短路。

### 1.3 三个必须写进契约的陷阱

1. **资源终结不自动**。`async for` 提前退出（break / 异常 / 上层 aclose）**不会**调用内层
   async generator 的 `aclose()`；Python 只在 GC / `loop.shutdown_asyncgens()` 时兜底，
   时机不确定。SDK 的 HTTP 响应关闭依赖内层 generator 的 `async with` / `finally` 被
   GeneratorExit 触发。因此**每一层包装必须 `contextlib.aclosing(inner)`**（标准库，3.10+），
   或由内核的 waterfall 运行器在边界对返回迭代器统一 aclosing。漏掉的后果是中途放弃流时
   httpx 连接挂到 GC 才还池。
2. **单次消费，禁止 tee**。录制/回放类中间件必须边转发边落盘，不能先耗尽再重放；
   `get_final_message()` 依赖流被完整消费，短路/中断后不存在 `message_final` 帧，
   消费方要能容忍其缺失（现状 runner 在 chunk 循环里探测 message_final，agent.py:632-633）。
3. **错误从 `async for` 内部抛出**。流中途的 SSE `error` 事件在迭代中直接
   `raise _make_status_error(...)`（SDK:_streaming.py:299-311，async 版），
   即异常可能发生在已产出若干 chunk 之后。`agent/request-error` 必须承接
   「半截流失败」形态——重试时此前已 emit 的 `assistant/chunk` 如何作废，词汇要有交代
   （现状 translator 的 `_open` 半开块会残留，llm/types.py:129）。

### 1.4 若嫌包装易错的替代设计

把 `llm/stream` 的 waterfall 契约缩小为「请求前决策」：载荷是 options + 迭代器**工厂**，
中间件只允许改 options 或不调 `next` 整层替换；「流中改事件」的需求排除在契约外，
观测走 `assistant/chunk`(emit)。design 5.1 中 `llm/stream` 列出的唯一挂载方就是
「测试用 Fake 整层替换」，小契约足以覆盖，且消灭陷阱 1/2 的大部分暴露面。推荐按小契约冻结，
需要时再扩。

### 1.5 顺带发现（与 P2-2 相关）

`AnthropicClient.stream()` 与 `complete_structured()` **每次调用都 new 一个
`AsyncAnthropic`**（llm/anthropic_client.py:47-52, 121-126）且从不 close，httpx 连接池
靠 GC 回收。SDK 客户端可安全复用，迁入 provider 后应改为 provider 级持有单例并在插件
卸载 effect 中关闭。

---

## Q2 token usage 的正确获取时机

### 2.1 usage 在流式事件上的分布（SDK 0.104.1 类型 + 官方 streaming 文档一致）

| 事件 | usage 内容 | SDK 出处 |
|---|---|---|
| `message_start` | `message.usage`（完整 `Usage`）：**input_tokens、cache_creation_input_tokens、cache_read_input_tokens** 在此已给出；output_tokens 为初值；另有 cache_creation（按 TTL 细分）、server_tool_use、service_tier、inference_geo | SDK:types/raw_message_start_event.py:11-14；types/usage.py |
| `message_delta` | `usage: MessageDeltaUsage`：**output_tokens 为累计值（必有）**；input/cache_creation/cache_read/server_tool_use 为 Optional 累计值；同事件 `delta` 携带 stop_reason / stop_sequence / stop_details | SDK:types/raw_message_delta_event.py:15-35；types/message_delta_usage.py |
| `message_stop` | 原始 wire 事件**不带 usage**（高层流的 MessageStopEvent 附带的是累积 snapshot） | SDK:lib/streaming/_messages.py:342-345 |

`get_final_message()` 的 usage 是 SDK 在 `accumulate_event` 合成的：以 message_start.usage
为底，message_delta 到达时覆写 output_tokens 及（若存在的）input/cache 字段
（SDK:lib/streaming/_messages.py:503-519）。官方口径：一次请求总输入 =
input_tokens + cache_creation_input_tokens + cache_read_input_tokens
（SDK:types/raw_message_delta_event.py:49-50 docstring 原文）。

### 2.2 对现状的判定：数据没丢在 SDK 层，丢在 translator 覆盖

`AnthropicClient.stream()` 每次调用末尾**必发**一帧自定义 `message_final`，其中 usage 即
final.usage 全量（llm/anthropic_client.py:69-83）。丢失点只有一个：
`AnthropicStreamTranslator._on_message_final`（llm/types.py:210-220）把它写进
`_last_usage` 覆盖前值——translator 跨一个 Turn 多次调用共用（agent.py:565），
于是前 N-1 次的 usage 蒸发。design 5.6 缺口 1 的表述「连事件都不发」准确，
但成因是翻译层缓存策略，不是拿不到数据。

### 2.3 采集点结论

- 保持现有帧结构时：**采集点就是 `message_final`**（每次调用恰好一帧）。修复 =
  `_on_message_final` 不再只覆盖，而是产出一条 per-call usage 记录（translator 返回
  一个内部事件交 runner 入日志，或 runner 在探测到 message_final chunk 处直接读
  `chunk["usage"]` + `chunk["stop_reason"]` 落库——runner 本就在 agent.py:632 检查该帧）。
- 若 provider 改用裸 `stream=True`（无 get_final_message）：采集点为 **message_start
  （input 侧 + 两个 cache 字段）+ 末次 message_delta（累计 output + stop_reason）** 两点合成，
  与 SDK accumulate_event 同构。
- `turn_end` 若还要带整轮 usage，语义应改为「本轮各次调用记录之和」并在词汇里写明
  （跨调用求和是各次请求计费的总和，input 侧不去重）。
- cache 两字段的观测日志 `_log_cache_usage`（llm/anthropic_client.py:85-103）应保留并
  并入 per-call 记录（也是 Q4 缓存击穿的监测手段）。

---

## Q3 Tool Use 的流式增量

### 3.1 完整序列（官方 streaming 文档 + SDK 类型联合验证）

```
message_start
content_block_start    # content_block = {type:"tool_use", id:"toolu_...", name:"...", input:{}}
content_block_delta    # delta = {type:"input_json_delta", partial_json:"<JSON 片段>"} × N（可为 0）
content_block_stop
message_delta          # delta.stop_reason = "tool_use"；usage.output_tokens 累计
message_stop
```

- 并行工具调用 = 同一条 assistant 消息里多个 tool_use 块，按 `index` 递增依次走各自的
  start/delta/stop；partial_json 拼接完才是合法 JSON；空入参工具可能 0 个 delta
  （translator 已处理：buffer 空 → `{}`，llm/types.py:203）。
- content_block_start 的 `content_block` 联合在 0.104.1 有 **12 种**：Text、Thinking、
  **RedactedThinking**、ToolUse、ServerToolUse、WebSearchToolResult、WebFetchToolResult、
  CodeExecutionToolResult、BashCodeExecutionToolResult、TextEditorCodeExecutionToolResult、
  ToolSearchToolResult、ContainerUpload（SDK:types/raw_content_block_start_event.py:24-39）。
- delta 联合：text_delta / input_json_delta / **citations_delta** / thinking_delta /
  **signature_delta**（SDK:lib/streaming/_messages.py:464-497 的分支即全集）。

### 3.2 现有 `AnthropicStreamTranslator` 的覆盖面核对（llm/types.py:118-220）

| 类别 | 判定 |
|---|---|
| 已正确处理 | content_block_start / text_delta / thinking_delta / input_json_delta / content_block_stop / message_final |
| 刻意忽略且安全 | ping、message_start、message_delta、message_stop；高层流合成的 `text` / `input_json` / `thinking` / `signature` / `citation` 事件（与 raw delta 重复，SDK:lib/streaming/_messages.py:331-371 合成） |
| **遗漏 1** | `message_delta` 当噪声 → per-call stop_reason/usage 全押在自定义 message_final 帧上；换裸流实现立即缺数据（与 Q2 同因） |
| **遗漏 2** | `redacted_thinking` 不在 `_BLOCK_TYPE_MAP`（llm/types.py:100-104），block_start 会误标为 TEXT——开启 extended thinking 后遇安全兜底即触发 |
| **遗漏 3** | `signature_delta` / `citations_delta` 丢弃（:190-191）。对 UI 无损（签名与引文在 final content 里），但意味着**增量事件流不足以重建 assistant content**——thinking 块缺 signature 的重建结果回传模型会被拒 |
| **遗漏 4** | server tool 各块类型同样回落 TEXT——当前项目不用 server tools，暂无影响 |

### 3.3 改造后 `tool/call` 事件的采集点

- 采集时刻维持「入参完整」即 tool_use 块的 `content_block_stop`（translator
  `_on_block_stop` 的 tool_use 分支，llm/types.py:200-207）；不要在 block_start 发
  `tool/call`（此刻 input 未知）。
- **入参的权威来源**保持 `message_final.content` 里 SDK 已反序列化的 `tool_use.input`
  （runner 现状即 `extract_tool_uses(final_content)`，agent.py:639；llm/types.py:82-88）；
  增量拼接结果只作 UI 预览。二者理论一致（SDK 用 jiter partial parse 累积同一份 buffer，
  SDK:lib/streaming/_messages.py:469-482），但以 final 为准可免去 json.loads 失败回落 `{}`
  的分叉（llm/types.py:202-205）。
- 若要流式展示入参进度，可透传高层流的 `input_json` 合成事件（带 partial snapshot），
  无需自行 partial parse。

---

## Q4 Prompt Caching 与四种注册的断点策略

### 4.1 官方当前规则（官方 prompt-caching 文档缓存版；canonical URL：
`platform.claude.com/docs/en/build-with-claude/prompt-caching.md`）

- 语法：`cache_control: {"type": "ephemeral"}`，可选 `"ttl": "5m" | "1h"`；可打在任意内容块
  （system 文本块、tool 定义、message 内容块）；另有**请求顶层 `cache_control`**（自动打在
  最后一个可缓存块）——0.104.1 的 `stream()` 已带该参数（SDK:resources/messages/messages.py:1044）。
- **前缀匹配**：任何一个字节变化使其后全部失效；渲染顺序 tools → system → messages；
- 每请求最多 **4 个断点**；断点向前回看至多 **20 个 content block**；
- 最少可缓存 token 数按模型：512（Opus 5 / Fable 5）、1024（Opus 4.8、Sonnet 5/4.6/4.5 等）、
  2048（Opus 4.7）、4096（Opus 4.6/4.5、Haiku 4.5）——低于阈值静默不缓存（未在线验证，
  来源为官方缓存参考）；
- 计费：写 5m = 1.25×、写 1h = 2×、读 = 0.1×（未在线验证，同上）；
- 命中验证：读响应 usage 的 `cache_read_input_tokens` / `cache_creation_input_tokens`
  （字段存在性已由 SDK:types/usage.py 验证）。

### 4.2 现状基线（改造要保住的形状）

`compose_lead_blocks` 打了两个断点：稳定段末尾（把 tools 数组连带缓存）+ Memory Catalog
末尾（供 turn 内多轮迭代复用完整 system），均为默认 5m TTL（prompt/compose.py:48-75）；
plan-mode 指令 append 在断点**之后**（agent.py:598-604）——易变内容排在最后断点后，正确。

### 4.3 正面回答：插件顺序导致缓存频繁失效是真实风险，且默认实现必然踩中

design 6.1 明确「组合条目行序不带加载语义」，epoch 驱动还会在运行期自动重挂插件。
若 `system-prompt/assemble` waterfall 按**监听器注册顺序**拼段落，则插件加载顺序、
重载顺序的任何抖动都会改变前缀字节序 → 缓存全体失效（每次以 1.25× 写溢价重建）。
不做显式排序时该风险不是「可能」而是「必然」。对策按优先级：

1. **确定性排序**：section/context/variable/tools 四种注册全部携带显式 `(priority, key)`，
   assemble 输出按 (priority, key) 排序而非注册顺序；同 key 重复注册直接报错。
   插件挂载顺序从此与输出字节序解耦——这是消除该风险的充分条件。
2. **稳定/易变分层 + 断点归属唯一**：注册时声明段落属「稳定」（部署期不变：身份、工具
   使用说明、技能目录骨架）或「易变」（随 turn 变：plan 指令、memory catalog、时间上下文）；
   assemble 末端由 systemPrompt seam **统一**插 cache_control——断点只打稳定段末尾，
   易变段一律排在最后断点之后。**禁止插件自带 cache_control**，否则 4 断点上限被抢占、
   断点位置随插件集漂移。
3. 接受两类**一次性**失效并写进文档：插件启停（段落集合变化）与租户/preset patch 改配置
   （design 6.2 第 4、5 层）——集合变了失效一次是预期行为；要消灭的是「集合未变、仅顺序变」
   的失效。
4. **工具列表同属前缀**：ToolRegistry 输出必须按工具名排序（P3-3 改造时落实）；
   plan-mode 的 `_registry_for_mode` 切换工具集会整体失效缓存（现状已如此，属集合变化）；
   模型切换（per-agent `active_model_name`）缓存不共享——realm/preset 差异化模型时
   每模型各自建缓存，属预期。
5. 监测：per-call usage 记录（Q2）里的 cache_read 连续为 0 即击穿告警信号，
   保留 `_log_cache_usage` 的语义并升级为结构化指标。

---

## Q5 错误与重试

### 5.1 异常层级（0.104.1 实测，SDK:_exceptions.py 全文核对）

```
AnthropicError
└── APIError (message / request / body)
    ├── APIStatusError (response / status_code / request_id / type: ErrorType)
    │   ├── BadRequestError 400        ├── AuthenticationError 401
    │   ├── PermissionDeniedError 403  ├── NotFoundError 404
    │   ├── ConflictError 409          ├── RequestTooLargeError 413
    │   ├── UnprocessableEntityError 422  ├── RateLimitError 429
    │   ├── ServiceUnavailableError 503   ├── DeadlineExceededError 504
    │   ├── OverloadedError 529        └── InternalServerError (其余 ≥500)
    ├── APIConnectionError → APITimeoutError
    ├── APIResponseValidationError
    └── APIWebhookValidationError
```

`APIStatusError.type` 从响应体 error.type 提取，取值为 ErrorType Literal：
invalid_request_error / authentication_error / permission_error / not_found_error /
rate_limit_error / timeout_error / overloaded_error / api_error / billing_error
（SDK:types/shared/error_type.py）。**没有任何「上下文溢出」专用错误码。**

### 5.2 内置重试与超时（SDK:_base_client.py:804-838）

- 先服从 `x-should-retry: true|false` 响应头；否则重试 **408、409、429、≥500（含 529）**
  与连接错误；默认 `max_retries=2` 指数退避；429 的 `retry-after ≤ 60s` 时按 header 等待
  （:789-791）。项目现值 max_retries=2（llm/anthropic_client.py:25）。
- 400/401/403/404/413/422 不重试。
- 默认超时 10 分钟；非流式且未显式给 timeout 时按 max_tokens 推算
  （SDK:resources/messages/messages.py:984-987）。墙钟上限 ≈ timeout × (max_retries+1)。

### 5.3 流中途错误的特殊形态（对 `agent/request-error` 设计至关重要）

SSE `error` 事件在迭代中 `raise self._client._make_status_error(...)`，但传入的是**已建立的
200 流响应**（SDK:_streaming.py:299-311）→ `_make_status_error` 按 status_code 分派
（SDK:_client.py:495-531）全部落空 → 抛出的是**基类 `APIStatusError`，status_code=200**。
两个推论：

1. 流中途错误（如中途 overloaded）**只能靠 `exc.type` 识别**（如 `"overloaded_error"`），
   不能靠异常类或 status_code；
2. SDK 自动重试只覆盖请求建立阶段——**流开始后的错误 SDK 不重试**，
   `agent/request-error` 要自己定义重试语义（且见 Q1 陷阱 3 的「半截流」问题）。

### 5.4 上下文溢出的可靠识别（如实回答：一半能，一半只能字符串匹配）

溢出在当前 SDK/API 上有**两种互不相同的表现**：

1. **请求期溢出**（输入本身超上下文）：HTTP 400 `invalid_request_error` →
   `BadRequestError`。ErrorType 无专用码，**结构化字段无法把「prompt 过长」与其他 400
   区分开——只能对 `exc.message`/body 做 "prompt is too long" 类字符串匹配，官方未提供
   更可靠的判据，如实说明**。建议：判定函数收敛在 `llm_anthropic` provider 内部
   （translator 同级），匹配失败宁可不触发压缩、按普通 400 上抛，避免误压缩掩盖真实参数错误。
   注意 413 `RequestTooLargeError` 是请求体字节数超限，不是 token 溢出，勿混。
2. **生成期溢出**（输入合法，但生成中触顶）：**成功响应**的
   `stop_reason = "model_context_window_exceeded"`，不是异常。0.104.1 的 GA `StopReason`
   Literal 尚无该值（end_turn / max_tokens / stop_sequence / tool_use / pause_turn / refusal，
   SDK:types/stop_reason.py），它只出现在 `types/beta/beta_stop_reason.py:15`（另含
   compaction）。但 SDK 响应模型经 `construct_type` **无校验构建**
   （SDK:_base_client.py:669），GA 端点若返回该值会原样透传为字符串，不会抛错。
   官方缓存参考将其列为 4.5+ 模型的标准停止原因（migration 指南「Handle the
   model_context_window_exceeded stop reason (4.5+)」）。GA 端点实际是否返回：**未验证**，
   建议按「可能出现」防御性处理。

因此「供应商确认溢出 → 触发压缩」的正确落点是**两处**，design 目前只写了一处：
`agent/request-error`（waterfall，承接情形 1）＋ 成功路径上对 stop_reason 溢出值的分支
（承接情形 2，建议归入 `agent/pre-step` 下一轮判定或 `agent/turn-stopping`）。

### 5.5 openai 侧对照（供中立错误词汇设计）

openai 3.0.0 的 `APIError` 带结构化 `code` / `param` / `type` 字段
（SDK(openai):_exceptions.py:63-80）；上下文溢出是 400 + `code == "context_length_exceeded"`
——**openai 能结构化识别，anthropic 不能**。中立错误词汇应定义「context_overflow」类别，
由各 provider 自行翻译（anthropic 靠字符串匹配、openai 靠 code），循环与压缩插件
只认中立类别，不做任何供应商字符串匹配。

---

## Q6 `openai_client.py` 现状与 P2-3 评估

### 6.1 现状事实

- `stream()` 直接 `raise NotImplementedError`（llm/openai_client.py:73-83）。
- `complete_structured` 用 `client.chat.completions.parse`——在实装 3.0.0 中为 GA 方法
  （SDK(openai):resources/chat/completions/completions.py:91），当前用法兼容；
  `max_completion_tokens`、`response_format=<pydantic 模型>` 均有效。
- **版本错位**：pyproject 声明 `openai>=1.59.5`，锁定/实装是 **3.0.0**（两个大版本差）。
  `parse` 在 1.x 早期位于 `client.beta.chat.completions`，代码现用的是 3.x 位置——
  即代码事实上只兼容新版本，声明下限却允许装回不兼容的旧版。应收紧下限。

### 6.2 补流式实现的工作量与两家 SDK 的差异抹平

openai 3.0.0 有三条可选通路（均已在实装源码核对存在）：

- **通路 A**：裸流 `client.chat.completions.create(stream=True,
  stream_options={"include_usage": True})` → `AsyncStream[ChatCompletionChunk]`；
- **通路 B（推荐）**：SDK 帮助器 `client.chat.completions.stream(...)`
  （SDK(openai):resources/chat/completions/completions.py:1547/3158）→
  `AsyncChatCompletionStreamManager` + `get_final_completion()`
  （SDK(openai):lib/streaming/chat/_completions.py:216-245）——与 anthropic 的
  stream 帮助器**同构**（async with 上下文管理器 + 事件流 + final 快照），
  provider 写法可与 AnthropicClient.stream 完全同形（含合成 `message_final` 等价帧）；
- **通路 C**：Responses API（3.x 主推，`resources/responses/` 存在）——事件词汇完全不同
  且原生带 reasoning summary，作为将来第三个 translator，不建议 P2-3 做。

词汇差异 → 中立表示的抹平表（Anthropic wire ↔ OpenAI Chat Completions wire）：

| Anthropic | OpenAI CC | 抹平方式（openai translator 职责） |
|---|---|---|
| content_block_start/stop + index | 无块概念 | 首个 `delta.content` 到达时合成 block_start(text)，finish 时合成 block_stop |
| text_delta | `choices[0].delta.content` | 直译 |
| thinking_delta | 无（CC 流不含思维） | 无对应，省略 |
| tool_use 三段式（start 带 id/name → input_json_delta → stop） | `delta.tool_calls[]`：`{index, id?, function:{name?, arguments 片段}}`，首片段带 id/name，后续只有 arguments（SDK(openai):types/chat/chat_completion_chunk.py:62-74） | 按 tool_calls[].index 开虚拟块：首见 → 合成 block_start(tool_use)；arguments → input_json_delta；finish_reason=tool_calls → 逐块 stop |
| message_delta.stop_reason | `choices[0].finish_reason`: stop/length/tool_calls/content_filter/function_call（:110） | 映射 stop→end_turn、length→max_tokens、tool_calls→tool_use、content_filter→refusal（近似） |
| message_start/delta 的 usage | 仅终帧 `chunk.usage`（CompletionUsage），须 `stream_options.include_usage`，此前各帧为 null（:324-330 docstring） | prompt_tokens→input、completion_tokens→output；cache_read 对应 prompt_tokens_details.cached_tokens；**无 cache_creation 概念**（OpenAI 缓存自动、无写断点） |
| message_final（项目自定义帧） | 无 | 通路 B 用 get_final_completion() 生成等价帧 |

工作量判定：通路 B 下 provider 本体 ≈ 一个 150–250 行的 translator + 契约测试，**不难**；
真正的阻塞在别处——runner 当前依赖三件 Anthropic 专属物：`message_final.content` 的
Anthropic block 列表（落库 + `extract_tool_uses`）、`tools` 参数为 Anthropic ToolParam 形状
（openai 需 `{"type":"function","function":{...}}` 包装）、system_prompt 的 TextBlockParam
cache 断点（openai 无对应）。这正是 R-S-8 / P3-6 的消息格式脱耦——**脱耦完成前，
openai 流即便实现也接不进主循环**。

结论：P2-3 两个选项中应选「**加载时明确声明不支持流式**」，与阶段顺序自洽；
「补实现」移到 P3-6 之后按通路 B 实施。声明机制建议做成 adapter 能力位
（`supports_stream` / `supports_structured`），LlmRuntime 注册时校验，
缺能力即 fiber FAILED，与 design 6.3 启动断言合流，而不是运行时才炸。

---

## 对 design.md / implement.md 的修正建议

### design 5.1（事件定义）

1. **`llm/stream`(waterfall) 补契约细则或缩小契约**。若保持「中间件可包装流」：
   载荷定义为「options + `next` 返回 AsyncIterator[中立 chunk]」，并写死三条纪律——
   每层包装必须 `contextlib.aclosing`；迭代器单次消费禁止 tee；中断后无 message_final
   等价帧，消费方须容忍缺失。若采用小契约（推荐，见 1.4）：中间件只许改 options 或
   不调 next 整层替换，流中观测一律走 `assistant/chunk`(emit)。
2. **`agent/request` 与 usage 入日志解绑**。5.6 缺口 1 写「usage 随 agent/request 的
   响应事件入日志」，但 waterfall 是请求发出前的改写关口，usage 产生在流耗尽后，
   时机对不上。应新增一个每次调用必发的响应事件（如 `llm/response`，emit/parallel），
   载荷 = {model, stop_reason, usage(input/output/cache_creation/cache_read), request_id}，
   由它入日志；`agent/request` 保持纯请求侧。
3. **`agent/request-error`(waterfall) 补三条语义**：(a) 错误可能发生在流已产出若干
   chunk 之后（SSE error 从 async for 内抛出，且 SDK 不重试流中错误）——契约必须规定
   重试时此前 `assistant/chunk` 的作废方式（建议规定「chunk 仅供渲染，权威内容以
   最终事件为准」，重试即前端整块重绘）；(b) 溢出识别判据下沉 provider：anthropic 靠
   BadRequestError + 消息串匹配，openai 靠 code==context_length_exceeded，事件载荷
   只携带中立错误类别（如 context_overflow / rate_limited / overloaded / transient / fatal），
   循环与压缩插件不做任何字符串匹配；(c) 流中错误抛出的是 status_code=200 的基类
   APIStatusError，只能靠 `.type` 分类——该翻译也属 provider。
4. **compaction 的挂载点从两处改三处**（同时更新第四节 compaction 行与 5.2 表）：
   `agent/pre-step`（压力）+ `agent/request-error`（请求期 400 溢出）+
   **成功路径 stop_reason=model_context_window_exceeded**（生成期溢出，归入
   pre-step 下一轮判定或 `agent/turn-stopping`）。

### design 5.6（缺口描述）

5. **缺口 1 改写成因与处置**：per-call usage 数据在 SDK 层每次调用都完整
   （message_final 帧 = message_start + message_delta 的累积，含 cache 两字段），
   丢失仅因 translator `_on_message_final` 覆盖缓存；处置 = translator/provider 每次
   message_final 产出 per-call usage 记录事件（对应修正 2 的 `llm/response`）入日志；
   注明裸流实现时采集点为 message_start + 末次 message_delta；`turn/end` 的 usage
   语义改为本轮 per-call 记录之和。
6. **补一处新缺口（第 6 处）**：增量事件流不足以重建 assistant content——
   redacted_thinking 被误标 TEXT、signature_delta/citations_delta 被丢弃。处置：
   词汇文档明示「增量事件仅供 UI，assistant content 的落库真源是每次调用的
   final content」；translator 迁入 provider 时补 redacted_thinking 映射。

### implement.md

7. **P2-2 增加四条验收**：(a) `AsyncAnthropic` 改 provider 级单例持有、插件卸载 effect
   中关闭（现状每次调用 new 且不 close，anthropic_client.py:47/121）；(b) translator 补
   `redacted_thinking` 块类型映射；(c) `complete_structured` 的 `temperature=0`
   （anthropic_client.py:149）按模型族开关——官方迁移指南：Opus 4.7+/Sonnet 5/Fable 5
   传 temperature 返回 400（未在线验证，来源官方缓存参考）；(d) max_retries/timeout
   与 P2-1 的 max_tokens 一并进配置。
8. **P2-3 直接定案「加载时声明不支持」**：消息格式脱耦（P3-6）前 openai 流实现了也
   接不进循环；「补实现」改为 P3-6 后置任务，按通路 B（chat.completions.stream 帮助器
   + Q6 词汇映射表）实施；声明机制做成 adapter 能力位 + 注册期校验（fiber FAILED）。
   同批把 pyproject 的 `openai>=1.59.5` 收紧到已验证的 3.x 下限。
9. **P4-2 的 usage 子项细化**：从「每次模型调用的 usage」改为「per-call `llm/response`
   事件（model / stop_reason / usage 四字段 / request_id）入 session_events + turn 聚合
   视图」；并在 A4/异常路径验收中补「request-error 重试与已发 chunk 的交互」一条
   （对应修正 3a）。

---

## Caveats / 未验证

1. **在线官方文档不可达**（区域限制，307 → app-unavailable-in-region）。官方结论证据 =
   Anthropic 官方 claude-api 技能内置参考（缓存 2026-06）+ 实装 SDK 源码；两者一致处视为
   已验证。仅缓存参考支撑、无 SDK 佐证的数字已逐处标「未在线验证」：缓存定价倍率
   （1.25×/2×/0.1×）、各模型最小可缓存 token 数、Opus 4.7+ 拒收 temperature。
2. **未发起真实模型调用**（任务约束）；流事件顺序与错误形态来自 SDK 源码推演 + 官方文档，
   未经真实流量复核。
3. **`model_context_window_exceeded` 在 GA 端点的实际出现与否未验证**：0.104.1 GA
   StopReason 类型不含它、beta 类型含它，且响应经 construct_type 无校验透传——
   按「可能出现」防御性处理（5.4）。
4. `stop_details`（refusal 细节）在 0.104.1 的 message_delta.delta 上已有类型
   （SDK:types/raw_message_delta_event.py:22-23），本报告未展开 refusal 处理，
   新词汇冻结前如需覆盖 refusal 语义须另行调研。
5. openai 3.0.0 的 Responses API 事件词汇未逐一核对（仅确认资源存在），
   P3-6 后若选通路 C 需要补一次专项调研。

## 附：本次核对触达的关键文件

| 文件 | 用途 |
|---|---|
| llm/anthropic_client.py / llm/types.py / llm/base.py / llm/openai_client.py | 现有实现 |
| runtime/agent.py:100-121, 552-702 | 循环内模型调用与 translator 生命周期 |
| prompt/compose.py:42-75 | 现有缓存断点 |
| SDK: _exceptions.py / _base_client.py / _streaming.py / _client.py | 异常、重试、流错误 |
| SDK: resources/messages/messages.py / lib/streaming/_messages.py | stream() 与累积 |
| SDK: types/{stop_reason,usage,message_delta_usage,raw_*}.py / types/beta/beta_stop_reason.py | 事件与字段 |
| SDK(openai): _exceptions.py / resources/chat/completions/ / lib/streaming/chat/ / types/chat/chat_completion_chunk.py | Q6 |
