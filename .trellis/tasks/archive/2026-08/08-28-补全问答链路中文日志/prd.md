# 补全问答链路中文日志

## 目标

让「用户提问 → 拿到响应」这条链路（含记忆召回、记忆提取、子代理、上下文压缩）
在运行时可通过日志完整复盘：每个关键节点有简体中文日志、带得上定位用的结构化
上下文，一条 `grep "session_id=42"` 就能捞出整次会话的全过程，出问题时不必靠
临时加 `print` 或断点还原现场。

## 背景与现状（已核实）

### 日志基础设施

- `core/logging.py:9` `setup_logging()` 仅调用 `logging.basicConfig`，
  级别硬编码 `logging.INFO`（`core/logging.py:11`），无 handler / formatter 定制。
- formatter 为 `"%(asctime)s %(levelname)s %(name)s %(message)s"`
  （`core/logging.py:12`），**不含任何 `extra` 字段占位符**。
- 实测验证：传入 `extra={"request_id": ..., "session_id": ...}` 后输出为
  `2026-08-28 16:14:31,717 INFO agentos.demo 用户提问已进入编排`，
  结构化字段被静默丢弃。
- `core/config.py` 中**没有任何 log 相关配置项**，`.env` / `.env.example`
  中也没有对应键（`grep -i log` 零命中）。`Settings.model_config` 为
  `extra="forbid"`（`core/config.py:20`），新增配置项漏改任一处都会启动失败。
- `main.py:42` 调 `setup_logging()`、`main.py:51` 注册中间件，除此之外无装配。
- Worker 进程另有一处 `logging.basicConfig(level=INFO)`（`memory/jobs/worker.py:103`），
  用默认格式，连时间戳都没有。

### 关联标识

- `request_id` 由中间件生成（`core/logging.py:20`）并写入 `request.state`，
  经 `core/responses.py:30` / `core/errors.py:44` 进入响应外壳。
  **没有 ContextVar**，脱离 `Request` 对象的深层代码（runtime、memory、tools）
  拿不到它；全项目唯一的 ContextVar 是 `team/isolation.py:10`，与日志无关。
- 全项目唯一带 `request_id` 的日志是中间件自己的
  `"request finished"`（`core/logging.py:27`），也是**唯一一条英文日志**。
- 注意区分：`core/events.py:155` 的 `InteractionInfo.request_id` 与
  `runtime/agent.py` 中的 `request_id: UUID` 指人机交互请求 ID，与 HTTP
  `request_id` 是两个概念。

### 现有日志覆盖

产品代码共 **38 条** logger 调用：`memory/jobs/service.py` 7、`runtime/agent.py` 5、
`memory/jobs/worker.py` 5、`hooks/builtin.py` 5、`memory/recall/service.py` 4、
`runtime/subagent.py` 3、`runtime/stream_assembler.py` 2、`llm/anthropic_client.py` 2、
`hooks/__init__.py` 2、`tools/builtin/bash.py` 1、`memory/jobs/extraction.py` 1、
`memory/jobs/dream.py` 1。除中间件那条外全部为中文。

`runtime/agent.py` 共 1732 行，5 条日志**全部是 `logger.exception`**
（233、517、737、761、786 行）——核心编排器只在出错时说话，正常流程全程静默。
`memory/recall/service.py` 的 4 条同样全是 warning / exception，召回成功时无日志。

产品代码**无 `print` 残留**（仓库内 11 处 `print` 全在 `.claude/hooks/` 的
Trellis 工具脚本，属 stdout 协议，不在整改范围）。

### 已有埋点被级别静默

`hooks/builtin.py` 已存在 4 条覆盖关键节点的埋点，级别是 `debug`，
在硬编码 INFO 根级别下**全部不可见**：`:25` 用户已提交消息、
`:30` 工具准备开始执行、`:39` 工具已执行完成、`:48` 交互轮次即将结束。

### 链路锚点与缺口

| 节点 | 锚点 | 现状 |
|------|------|------|
| 提问入口 | `session/api.py:43` `send_message()` | 零日志 |
| 交互恢复入口 | `interaction/api.py:83` `resolve_interaction()` | 零日志 |
| 编排入口 | `runtime/agent.py:128` `run()` → `:153` `_produce()` | 仅 :233 异常兜底 |
| 记忆召回 | `runtime/agent.py:584` `get_or_create_context()` | 仅降级/异常 |
| ReAct 循环 | `runtime/agent.py:557` `_run_llm_loop()` | 零日志 |
| 上下文装载 | `runtime/agent.py:613` `load_context_for_turn()` | 零日志 |
| 上下文压缩 | `runtime/agent.py:619` → `runtime/compact.py:31` | **整个文件零日志** |
| LLM 调用 | `runtime/agent.py:630-638` `llm.stream()` | 零日志 |
| 首 token | `runtime/stream_assembler.py:80` TEXT_DELTA | 无观测点 |
| 工具执行 | `runtime/agent.py:791`/`:947` → `tools/service.py:165` | **零日志** |
| 权限裁决 | `runtime/agent.py:913-935` ask 挂起 / deny 拒绝 | 零日志（子代理侧反有） |
| 人工交互 | `runtime/agent.py:1043` 挂起 / `:321` 取消 | 零日志 |
| 回合收尾 | `runtime/agent.py:704` turn_end / `:712` `_complete_turn()` | 零日志 |
| token 用量 | `runtime/agent.py:704-706` `result.usage` | 只发 SSE，不写日志不落库 |
| 记忆提取 | `runtime/agent.py:732` 入队 / `:748` 就地执行 | 仅失败分支 |
| 子代理 | `runtime/subagent.py:85` `SubAgentRunner.run()` | 启动/成功无日志 |
| teammate | `runtime/subagent.py:400` `run_teammate()` | 失败仅 emit SSE |

工具执行失败（`tools/service.py:246-262`）只落库+上抛，最终混进
`runtime/agent.py:233` 的总兜底，而**那条日志不带 `tool_name`**。
teammate 失败（`runtime/subagent.py:494-503`）raise 后被
`_wake_team_members`（`runtime/agent.py:1714-1727`）捕获，仅 emit SSE 事件、
无 logger——客户端已断连时该失败在服务端日志中**完全消失**。

零日志文件：`session/api.py`、`session/service.py`、`interaction/api.py`、
`interaction/service.py`、`agent/*`、`tools/service.py`、`tools/registry.py`、
`runtime/compact.py`、`core/event_bus.py`、`llm/factory.py`、`llm/openai_client.py`
（后者 `:32` 定义了 logger 但零调用，与 Anthropic 侧 2 条不对称）。

### 记忆链路运行条件

| 配置项 | 默认值 | 影响 |
|--------|--------|------|
| `memory_recall_rollout_percent` | `0`（`core/config.py:137`） | **召回默认全部跳过** |
| `memory_dream_rollout_percent` | `0`（`core/config.py:138`） | Dream 默认全部跳过 |
| `memory_extraction_enabled` | `True`（`core/config.py:110`） | 提取默认开启 |
| `memory_extraction_inline` | `True`（`core/config.py:111`） | 提取在请求协程内就地执行 |

召回同步发生在 `runtime/agent.py:584`；提取在 `_complete_turn`
（`runtime/agent.py:712`）入队、由 `_run_inline_memory_job`（`:748`）在主事务
commit 后就地执行；Dream 只有独立 Worker 进程（`python -m memory.jobs.worker`）
才会认领（`memory/jobs/service.py:200-229` 在传 `job_id` 时只认领 extract）。

**对本任务的含义**：召回日志在默认配置下不会输出，验收需临时调高
`memory_recall_rollout_percent` 才能观察到。

### 既有规范

`.trellis/spec/backend/logging-guidelines.md` 已完整定义本项目日志规范：中文消息、
`extra` 传结构化上下文、级别语义、`logger.exception` 优先、格式化参数而非 f-string、
以及敏感信息红线。本任务是**按既有规范补齐落地**，不重新定义规范。

## 关键决策

### D1：自定义 Formatter，`extra` 渲染成行尾 `key=value`

在 `setup_logging()` 里换掉裸 `basicConfig`：自定义 `Formatter`，把 `LogRecord`
上的非标准字段按 `key=value` 追加到行尾。

```
2026-08-28 16:14:31 INFO runtime.agent 记忆召回完成 session_id=42 turn_id=7 selected=3 duration_ms=128
```

理由：现有 29 条已带 `extra` 的日志零改动即刻可见；中文消息在前、上下文在后，
一行一事件；`grep "session_id=42"` 可捞出整条链路；改动集中在单文件、无新依赖。

放弃 JSON 形态：本地终端可读性差，中文需处理 `ensure_ascii`。
放弃「拼进消息文本」：违背既有 spec，且已有 29 条日志的 `extra` 依然浪费。

### D2：引入 `AGENTOS_LOG_LEVEL`，骨架 INFO / 细节 DEBUG

新增配置项 `AGENTOS_LOG_LEVEL`（默认 `INFO`），由 `setup_logging()` 读取，
替代硬编码级别。日志分两档：

- **INFO（骨架）**：一次问答约 8 行——收到提问、记忆召回完成、每轮 LLM 调用开始、
  首 token 到达、工具执行完成、回合结束（stop_reason / rounds / token 用量）、
  记忆提取完成。
- **DEBUG（细节）**：上下文装载 `msg_count` / `est_tokens`、记忆选择器候选集、
  工具参数校验等。`hooks/builtin.py` 已有的 4 条 DEBUG 埋点顺带复活。

### D3：覆盖到主链路 + 派生执行层

覆盖主问答链路，外加子代理（`runtime/subagent.py:85`）、teammate（`:400`）、
上下文压缩（`runtime/compact.py:31`）三层派生执行。不含 OpenAI 客户端对称补齐、
StreamBus 背压、Dream / Worker（见「不在本次范围」）。

### D4：以 `session_id` + `turn_id` 作为关联键，不引入 `request_id` ContextVar

SSE 场景下一次提问对应一个 turn，`session_id` + `turn_id` 已足够串起全链路，
且这两个值在 runtime / memory / tools 各层本就拿得到。引入 `request_id`
ContextVar 需要额外基建且对复盘无增量价值，本次不做。

## 需求

### R1 日志基础设施改造

- R1.1 `core/logging.py` 自定义 `Formatter`，将 `LogRecord` 上的非标准属性
  按 `key=value` 空格分隔追加到行尾；无 `extra` 时不留多余空格。
  需排除 `LogRecord` 内置属性，避免把 `pathname`、`lineno` 等一并打出来。
- R1.2 新增 `AGENTOS_LOG_LEVEL` 配置项（默认 `INFO`），在 `core/config.py`
  的 `Settings`、`.env`、`.env.example` **三处同步**；`setup_logging()` 读取该项
  替代硬编码级别；取值非法时抛中文 `AgentException`，不静默兜底。
- R1.3 `core/logging.py:28` 的英文 `"request finished"` 改为中文。
- R1.4 `memory/jobs/worker.py:103` 的独立 `basicConfig` 改为复用 `setup_logging()`，
  使 Worker 进程与主进程日志格式一致。

### R2 请求入口与回合生命周期

- R2.1 `session/api.py:43` 记录收到用户提问：`session_id`、`agent_id`、
  提问字符数（**不记提问正文**）。
- R2.2 `interaction/api.py:83` 记录交互恢复入口。
- R2.3 `runtime/agent.py:704`/`:712` 记录回合结束：`stop_reason`、总迭代轮数、
  Turn 总耗时、input/output token 用量。
- R2.4 客户端断连取消（`runtime/agent.py:144-151`、`:222-231` 的
  `CancelledError` 分支）记录为 warning。

### R3 LLM 调用与流式

- R3.1 `runtime/agent.py:630` 每轮 `llm.stream()` 前记录：轮次序号、模型名、
  上下文消息数（DEBUG 档补 `est_tokens`）。
- R3.2 `runtime/stream_assembler.py` 记录首 token 到达（TTFT 毫秒）。
- R3.3 单轮 LLM 调用结束记录耗时与本轮 token 用量。
- R3.4 **禁止**按 `text_delta` / `thinking_delta` 事件粒度打日志。

### R4 工具执行

- R4.1 `tools/service.py:165` `ToolService.run` 记录工具开始与完成：
  `tool_name`、`status`、耗时（**不记工具入参**，遵循 spec 红线）。
- R4.2 工具失败分支（`tools/service.py:246-262`）记录 `logger.exception`，
  必须带 `tool_name`，补上当前总兜底缺失的定位信息。
- R4.3 `runtime/agent.py:913-935` 记录主代理侧权限 ask 挂起与 deny 拒绝。
- R4.4 `runtime/agent.py:1043`/`:321` 记录人工交互挂起、恢复、取消。

### R5 记忆链路

- R5.1 `memory/recall/service.py:60` 记录召回开始与完成：命中条数、耗时；
  灰度跳过时以 DEBUG 记录跳过原因。
- R5.2 选择器候选集规模记为 DEBUG（`candidates` → `selected`）。
- R5.3 `runtime/agent.py:732` 记录提取任务入队（`job_id`、模式 shadow/active）。
- R5.4 `runtime/agent.py:748` 就地执行提取记录开始；完成日志复用
  `memory/jobs/service.py:274` 已有条目，补上 `session_id` / `turn_id`。

### R6 派生执行层

- R6.1 `runtime/subagent.py:85` 记录子代理启动、完成、失败：`agent_type`、
  轮数、耗时。
- R6.2 `runtime/subagent.py:400` teammate 唤醒与失败；失败必须在
  `_wake_team_members`（`runtime/agent.py:1714-1727`）捕获处补
  `logger.exception`，消除客户端断连时失败静默消失的缺口。
- R6.3 `runtime/compact.py:31` 记录是否触发压缩、压缩前后 token 估算。

### R7 规范遵从

- R7.1 全部新增日志使用简体中文，`extra` 传结构化字段，格式化参数用 `%s`
  而非 f-string。
- R7.2 遵守 `.trellis/spec/backend/logging-guidelines.md` 敏感信息红线：
  不记完整 Prompt、模型输出正文、工具入参、凭证、完整邮箱。
- R7.3 异常处一律 `logger.exception`，不新增 `print`。

## 验收标准

### AC1 结构化字段可见（对应 R1.1）

改造后执行：

```python
logging.getLogger("t").info("记忆召回完成", extra={"session_id": 42, "selected": 3})
```

输出行必须包含 `session_id=42 selected=3`；且**不得**出现 `pathname=`、
`lineno=`、`msecs=` 等 `LogRecord` 内置属性。无 `extra` 的日志行尾无多余空格。

### AC2 级别可配（对应 R1.2）

- 不设 `AGENTOS_LOG_LEVEL` 时默认 INFO，`hooks/builtin.py:25` 的 DEBUG 埋点不输出；
- 设为 `DEBUG` 后重启，同一次问答能看到该埋点；
- 设为非法值（如 `VERBOSE`）时启动抛中文 `AgentException`，不静默回落；
- `core/config.py`、`.env`、`.env.example` 三处均有该键，且 `Settings` 能成功加载。

### AC3 一次问答可完整复盘（对应 R2/R3/R4，核心验收）

默认 INFO 档下发起一次**触发工具调用**的真实问答，服务端日志中按序至少出现：
收到用户提问 → 每轮 LLM 调用开始（含模型名与轮次）→ 首 token 到达（含 ttft_ms）
→ 工具执行完成（含 tool_name、status、耗时）→ 回合结束（含 stop_reason、rounds、
input/output token 用量）。

上述所有行均带 `session_id` 与 `turn_id`，`grep "session_id=<本次会话>"`
能一次捞出全过程且顺序连贯。

### AC4 记忆链路可观测（对应 R5）

- 临时将 `memory_recall_rollout_percent` 调至 100 后发起问答，
  能看到记忆召回完成日志（含命中条数与耗时）；
- 保持默认 0 时，DEBUG 档能看到灰度跳过原因；
- `memory_extraction_inline=True` 默认配置下，能看到提取入队与完成日志，
  且带得上 `session_id` / `turn_id`。

### AC5 派生执行层可观测（对应 R6）

- 触发一次 Agent 工具调用，日志中出现子代理启动与完成（含 `agent_type`、轮数、耗时）；
- 构造 teammate 失败（可用 monkeypatch 注入异常），断开客户端后服务端日志
  **仍能看到** `logger.exception`，验证缺口已消除；
- 触发上下文压缩，日志中出现压缩前后 token 估算。

### AC6 不刷屏（对应 R3.4）

一次不含工具调用的普通问答，INFO 档日志行数 ≤ 10 行；
流式输出 token 数不影响日志行数（即无按 delta 打印）。

### AC7 规范与回归（对应 R7）

- `grep -rn "logger\.\(info\|warning\|error\|exception\|debug\)" --include="*.py"`
  新增条目全部为中文消息，无 f-string 内插；
- 产品代码无新增 `print`；
- 日志中不出现完整 Prompt、模型输出正文、工具入参、API Key、密码；
- `uv run pytest` 全量通过（现有 6 个测试文件，基线须先跑一次留存对照）。

## 不在本次范围

- `llm/openai_client.py` 与 Anthropic 侧的日志对称补齐（`:32` 定义了 logger
  但零调用）——独立议题，可后续单开任务。
- `core/event_bus.py:34` StreamBus 队列满背压观测。
- `memory/jobs/dream.py`、`memory/jobs/worker.py` 的 Dream 执行与轮询日志
  （Dream 默认灰度 0%，且只在独立 Worker 进程执行，不在请求链路上）。
  R1.4 只统一 Worker 的日志装配格式，不新增其业务日志。
- 日志落盘、轮转、接入外部日志聚合系统（ELK / Loki）。
- 引入 `request_id` ContextVar（见 D4）。
- 修改既有 38 条日志的消息文案（除 R1.3 那条英文外）。
- `.claude/hooks/` 与 `.trellis/` 下的工具脚本 `print`（属 stdout 协议）。

## 技术约束

1. `text_delta` 逐 token 产生（`runtime/stream_assembler.py:81`），
   `runtime/` 内共 19 处 `bus.emit`，**不得**按事件粒度打日志。
2. `Settings.model_config` 为 `extra="forbid"`，新增配置项必须三处同步，
   漏改即启动失败。
3. 记忆提取在主事务 commit 后就地执行（`runtime/agent.py:755-757`），
   其日志属于请求协程内，但已在流式响应结束之后——复盘时注意时序。
4. 日志新增不得改变任何业务控制流：不得吞异常、不得改变事务边界、
   不得在 `finally` 中引入可能抛出的日志调用。
5. 遵循 4 空格缩进、100 字符行宽、Python 3.14 语法。
