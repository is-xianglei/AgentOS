# 技术设计：补全问答链路中文日志

## 边界与原则

本任务是**纯观测性改造**，不改变任何业务控制流。硬约束：

1. 不吞异常、不改事务边界、不调整 `finally` 语义；
2. 不在 `finally` 中引入可能抛出的日志调用（避免掩盖原始异常）；
3. 不改变 SSE 事件序列与前端契约；
4. 日志失败不得影响主链路——沿用标准 `logging` 的默认行为
   （handler 异常由 `logging.raiseExceptions` 吞掉），不额外包裹 try。

分层归属：日志基建改造属 `core/`，各节点日志就地落在其所属 feature 包内，
不新建跨域日志封装层。

## 一、日志基础设施（`core/logging.py`）

### 1.1 KV Formatter

自定义 `Formatter`，把 `LogRecord` 上的非内置属性以 `key=value` 追加到消息行尾。

**关键实现约束：必须覆盖 `formatMessage()`，不能覆盖 `format()`。**

`logging.Formatter.format()` 的执行顺序是：先 `formatMessage(record)` 生成消息行，
再把 traceback 拼到其后。若在 `format()` 里追加 `extra`，异常日志的字段会被拼到
**堆栈最后一行**，形如 `ValueError: boom tool=Bash session_id=42`——已实测复现。
覆盖 `formatMessage()` 则字段留在消息行，traceback 正常跟随其后：

```
2026-08-28 16:31:55 ERROR tools.service 工具执行失败 tool=Bash session_id=42
Traceback (most recent call last):
  ...
ValueError: boom
```

内置属性集用**探测法**生成，不硬编码清单：

```python
_BUILTIN_FIELDS = set(
    logging.LogRecord("", logging.INFO, "", 0, "", None, None).__dict__
) | {"asctime", "message"}
```

Python 3.14 的 `LogRecord.__dict__` 已含 `taskName`（实测确认），探测法自动覆盖，
无需手工维护版本差异。`asctime` / `message` 是 `format()` 过程中动态挂上的，
需显式补入排除集。

无 `extra` 时直接返回原串，保证行尾无多余空格（AC1）。

### 1.2 级别配置

`setup_logging()` 从 `settings.log_level` 读取级别，替代硬编码 `logging.INFO`。
非法取值按 CLAUDE.md「无静默兜底」原则抛中文 `AgentException`，不回落默认值。

`basicConfig` 换成显式 `StreamHandler` + 自定义 Formatter，并设置
`force=True` 或先清 root handlers，避免 uvicorn 预装 handler 导致重复输出。

### 1.3 Worker 进程统一

`memory/jobs/worker.py:103` 的独立 `logging.basicConfig(level=INFO)`（默认格式、
无时间戳）改为调用 `setup_logging()`，使 Worker 与主进程格式一致。

## 二、配置项

| 项 | 值 |
|---|---|
| 环境变量 | `AGENTOS_LOG_LEVEL` |
| `Settings` 字段 | `log_level: str = "INFO"` |
| 位置 | `core/config.py` |
| 同步点 | `core/config.py` + `.env` + `.env.example` |

`Settings.model_config` 为 `extra="forbid"`，三处漏改任一处都会启动失败——
这既是风险也是保护。`.env.example` 写 `AGENTOS_LOG_LEVEL=INFO`（非敏感，可写实值）。

## 三、上下文字段获取路径

各层拿 `session_id` / `turn_id` 的既有途径，**不新增参数透传、不引入 ContextVar**：

| 层 | 来源 |
|---|---|
| `session/api.py` | 请求体 Schema 与 `SessionService` 返回 |
| `runtime/agent.py` | `_produce()` 局部变量 `session` / `turn`，已有 |
| `runtime/stream_assembler.py` | `TurnEventAssembler.__init__` 已持有 `session_id` |
| `tools/service.py` | `ToolContext` |
| `runtime/subagent.py` | 现有日志已在用 `session_id` / `agent_type` |
| `memory/recall/service.py` | `turn` 参数 |
| `memory/jobs/*` | `job` 对象已含 `session_id` / `turn_id` |

`tools/service.py` 若 `ToolContext` 未直接携带 `turn_id`，以 `session_id` +
`tool_name` 为准，不为加日志而改动工具调用签名。

## 四、耗时与 TTFT 测量

统一用 `time.perf_counter()`，输出毫秒并 `round(x, 2)`，字段名统一 `duration_ms`
（与中间件 `core/logging.py:26` 既有写法一致）。

**TTFT**：在 `runtime/stream_assembler.py` 的 `consume()` 内，进入循环前记起点，
首次遇到 `TEXT_DELTA` 或 `THINKING_DELTA` 时打一条 INFO 并置标志位，
后续 delta 不再计时不再打印（保证 R3.4 不刷屏）。标志位是**单次 consume 内**的
局部状态，不污染装配器的跨调用状态（其现有唯一跨调用状态是块 index，
见该文件 docstring）。

**单轮 LLM 耗时**：在 `runtime/agent.py:630-638` 的 `assembler.consume(...)`
外层计时，与 R3.1 的「调用开始」配对。

**Turn 总耗时**：`_run_llm_loop` 入口计时，`_complete_turn`（`:712`）输出。

## 五、Token 用量

`result.usage`（`runtime/agent.py:704-706`）目前只随 `turn_end` SSE 事件发出。
本次仅**增加一条日志**读取该值，字段 `in_tokens` / `out_tokens`，
**不落库、不改 SSE 契约**（落库是独立议题，不在本任务）。

`llm/anthropic_client.py:407` 已有的 cache 统计日志保持原样，不重复输出。

## 六、logger 命名

各模块沿用 `logging.getLogger(__name__)`，输出中体现为 `runtime.agent`、
`tools.service`、`memory.recall.service` 等，便于按模块过滤。
不引入自定义 logger 名（中间件既有的 `agentos.request` 保持不变，避免破坏
可能存在的外部过滤规则）。

## 七、兼容性与回滚

**向后兼容**：

- 现有 38 条日志一行不改（除 R1.3 那条英文消息），其 `extra` 自动变可见——
  这是收益不是破坏；
- 日志格式变化仅影响人读与 grep，不涉及任何程序化消费方
  （仓库内无日志解析代码，已确认）；
- 不新增依赖、不改 ORM、无 Alembic 迁移。

**回滚**：改动是纯增量的日志语句 + 单文件基建改造，`git revert` 即可完整回退；
配置项 `AGENTOS_LOG_LEVEL` 有默认值，回退后 `.env` 中的残留键因
`extra="forbid"` 会导致启动失败——**回滚时须同步删除 `.env` 中该键**，
这是唯一的回滚陷阱，需在实施记录中标注。

## 八、权衡记录

| 决策 | 取舍 |
|---|---|
| KV 而非 JSON | 换本地可读性，牺牲机器直接消费；后续接聚合系统需加解析规则或换 Formatter |
| 不引入 request_id ContextVar | `session_id`+`turn_id` 已够串链路，省一层基建（D4） |
| 不落库 token 用量 | 保持本任务纯观测，落库涉及 ORM + 迁移，独立议题 |
| OpenAI 客户端不补齐 | 与 Anthropic 侧不对称的问题留待独立任务，避免本次范围膨胀 |
| 探测法生成内置属性集 | 避免硬编码清单随 Python 版本失效 |

## 九、测试策略

- **单元测试**（新增 `tests/test_logging_formatter.py`）：KV Formatter 的三种
  情形——带 `extra`、不带 `extra`、异常日志（断言字段在消息行而非堆栈尾）；
  以及内置属性不外泄。
- **配置测试**：`AGENTOS_LOG_LEVEL` 默认值、合法值、非法值抛中文异常。
- **链路验证**：靠真实问答 + `caplog` 断言关键节点日志存在，不追求覆盖每条语句。
- 现有 6 个测试文件须保持全绿，改造前先跑一次基线留存对照。
