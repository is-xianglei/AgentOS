# Logging Guidelines

> How logging is done in this project.

---

## Overview

AgentOS 使用 Python 标准库 `logging` 模块进行日志记录。

**核心原则：**

- 新代码使用标准 `logging`，通过 `extra` 参数传递 `request_id`、`session_id`、`turn_id` 等结构化上下文
- 日志内容统一使用**简体中文**，包含定位问题所需的关键上下文
- 不要使用 `print()` 进行调试输出（历史遗留代码可能存在，但新代码禁止）
- 日志不得输出密码、Token、密钥、完整数据库连接串或其他敏感信息
- 详细异常信息用于服务端日志，不应直接泄漏到 API 响应或模型输出中

**Logger 初始化：**

```python
import logging

logger = logging.getLogger(__name__)
```

参考文件：`/Users/xianglei/Documents/develop/code/python/AgentOS/runtime/agent.py` 第 56 行

---

## Log Levels

按严重程度使用不同日志级别：

### `logger.debug()`
用于开发调试信息，生产环境通常不输出。

**使用场景：**
- 函数参数值
- 中间计算结果
- 详细的执行路径跟踪

### `logger.info()`
用于记录正常的业务流程和关键状态变更。

**使用场景：**
- 会话创建、任务启动、Agent 运行开始
- 关键业务节点完成（如工具调用成功、Memory 提取完成）
- 配置加载、服务启动
- 正常的状态转换

**示例：**
```python
logger.info(
    "子Agent运行完成，agent_type=%s rounds=%d",
    agent_type,
    len(turns),
    extra={"session_id": session_id},
)
```

### `logger.warning()`
用于记录潜在问题或异常情况，但不影响核心功能。

**使用场景：**
- 降级处理（如 Memory 提取失败但不影响主流程）
- 超过配置阈值但未达到错误级别
- 可恢复的异常情况
- Hook 执行失败但按 fail-open 放行

**示例：**
```python
logger.warning(
    "提取候选数量超过配置上限，已截断多余候选",
    extra={"candidate_count": len(self.memories), "limit": limit},
)
```

参考文件：`/Users/xianglei/Documents/develop/code/python/AgentOS/memory/jobs/extraction.py`

### `logger.error()`
用于记录错误，但程序可以继续运行。

**使用场景：**
- 单次请求失败
- 可重试的错误
- 非关键功能失败

### `logger.exception()`
**优先使用 `logger.exception()` 记录异常**，自动保留完整异常堆栈。

**使用场景：**
- 在 `except` 块中捕获异常时
- 需要完整堆栈信息定位问题

**示例：**
```python
try:
    # 业务逻辑
    result = await process_task(task_id)
except Exception:
    logger.exception(
        "处理任务失败，任务 ID=%s",
        task_id,
        extra={"session_id": session_id, "turn_id": str(turn_id)},
    )
    raise
```

参考文件：`/Users/xianglei/Documents/develop/code/python/AgentOS/runtime/agent.py`

**不要在异常处理中使用 `print()`。**

---

## Structured Logging

通过 `extra` 参数传递结构化上下文，便于日志聚合和问题定位。

### 常用字段

| 字段名 | 类型 | 说明 |
|--------|------|------|
| `request_id` | `str` | HTTP 请求 ID |
| `session_id` | `str` | 会话 ID |
| `turn_id` | `str` | 交互轮次 ID |
| `job_id` | `str` | 异步任务 ID |
| `user_id` | `str` | 用户 ID |
| `agent_type` | `str` | Agent 类型 |
| `duration_ms` | `float` | 耗时毫秒，统一 `round(x, 2)`；用 `time.perf_counter()` 测量 |
| `ttft_ms` | `float` | 流式首 token 到达耗时，单次 consume 内只打一条，不按 delta 刷屏 |
| `tool_name` | `str` | 工具名；**只记名字与结果，不记入参**（入参可能含敏感路径或命令） |
| `prompt_chars` | `int` | 用户提问字符数；**正文严禁入日志** |
| `round` / `rounds` | `int` | 当前 ReAct 轮次 / 本回合总轮数 |
| `msg_count` | `int` | 送入 LLM 的上下文消息数 |
| `stop_reason` | `str` | 回合终止原因 |
| `in_tokens` / `out_tokens` | `int` | 回合 token 用量（仅日志，不落库、不改 SSE 契约） |
| `est_tokens_before` / `est_tokens_after` | `int` | 上下文压缩前后的 token 估算 |
| `member_name` | `str` | teammate 名称 |

### 示例

```python
logger.exception(
    "处理会话消息失败",
    extra={"session_id": session_id, "turn_id": str(turn.id if turn else "")},
)
```

参考文件：`/Users/xianglei/Documents/develop/code/python/AgentOS/runtime/agent.py`

```python
logger.exception(
    "Memory提取任务发生未预期异常",
    extra={
        "job_id": str(job.id),
        "session_id": str(job.session_id),
        "turn_id": str(job.turn_id),
    },
)
```

参考文件：`/Users/xianglei/Documents/develop/code/python/AgentOS/memory/jobs/service.py`

---

## What to Log

### 必须记录的事件

1. **业务流程关键节点**
   - 会话创建、Agent 启动、任务认领
   - 工具调用开始和结束
   - Memory 提取、Dream 执行
   - Subagent 启动和完成

2. **状态变更**
   - 会话状态转换
   - 任务状态更新
   - 权限检查结果

3. **错误和异常**
   - 所有捕获的异常（使用 `logger.exception()`）
   - 降级处理和 fallback 逻辑触发
   - 重试次数和最终结果

4. **关键业务指标**
   - LLM 调用耗时和 Token 消耗
   - 数据库查询性能
   - 并发任务数量

5. **安全相关**
   - 认证失败
   - 权限拒绝
   - 危险工具调用（记录工具名和决策结果，不记录参数）

### 日志内容要求

- 使用**简体中文**描述问题
- 包含定位问题的**关键上下文**（ID、状态、参数等）
- 使用格式化字符串和参数，而不是字符串拼接：
  ```python
  # 正确
  logger.info("处理任务完成，任务 ID=%s，耗时=%d ms", task_id, duration)
  
  # 错误
  logger.info(f"处理任务完成，任务 ID={task_id}，耗时={duration} ms")
  ```
- 使用 `extra` 传递结构化字段，不要拼接到消息字符串中

---

## What NOT to Log

### 严禁记录的敏感信息

1. **认证凭证**
   - 密码（明文、哈希值）
   - API Key、Token
   - OAuth 凭证
   - Session ID 和 JWT 完整内容（可记录前缀或摘要）

2. **密钥和密文**
   - 加密密钥
   - 签名密钥
   - 数据库连接串（可记录主机和库名，不记录用户名密码）
   - Redis 密码

3. **个人隐私信息（PII）**
   - 完整的电子邮箱（可记录脱敏版本，如 `i***@example.com`）
   - 电话号码
   - 身份证号、护照号
   - 完整的用户输入（如果包含敏感内容）

4. **业务敏感数据**
   - 完整的 Prompt 内容（可记录长度、摘要）
   - 模型输出的完整内容（可记录长度、类型）
   - 用户上传的文件内容

### 安全实践

- 记录工具调用时，只记录工具名和调用结果，不记录完整参数（可能包含敏感路径、命令）
- 记录错误时，避免将原始异常直接返回给客户端，应转换为用户友好的错误消息
- 使用结构化字段传递 ID，而不是在消息中拼接完整对象
- 定期审查日志输出，确保没有泄漏敏感信息

### 示例：正确的敏感信息处理

```python
# 正确：只记录操作类型和结果
logger.info(
    "用户认证成功，user_id=%s",
    user_id,
    extra={"auth_method": "password"},
)

# 错误：记录了密码
logger.info(f"用户登录，email={email}, password={password}")  # ❌ 严禁


# 正确：记录工具调用但不记录敏感参数
logger.info(
    "执行 Bash 工具，命令长度=%d",
    len(command),
    extra={"session_id": session_id},
)

# 错误：记录了完整命令（可能包含密钥）
logger.info(f"执行命令：{command}")  # ❌ 避免


# 正确：记录异常但不泄漏数据库连接串
try:
    await db.execute(query)
except Exception:
    logger.exception(
        "数据库查询失败",
        extra={"query_type": "insert", "table": "sessions"},
    )
```

---

**参考文件：**
- `/Users/xianglei/Documents/develop/code/python/AgentOS/runtime/agent.py`
- `/Users/xianglei/Documents/develop/code/python/AgentOS/memory/jobs/service.py`
- `/Users/xianglei/Documents/develop/code/python/AgentOS/hooks/__init__.py`

---

## 日志基建契约（`core/logging.py`）

### 1. Scope / Trigger

新增环境变量 `AGENTOS_LOG_LEVEL` 并以自定义 Formatter 接管全局输出格式，
属于 env wiring + 跨层输出契约变更，故按 code-spec 深度记录。

### 2. Signatures

```python
# core/logging.py
def setup_logging() -> None: ...

class _KeyValueFormatter(logging.Formatter):
    def formatMessage(self, record: logging.LogRecord) -> str: ...

_THIRD_PARTY_FLOOR_LOGGERS: tuple[str, ...]   # 压级名单
_BUILTIN_FIELDS: set[str]                     # 探测法生成，非硬编码

# core/config.py
class Settings(BaseSettings):
    log_level: str = "INFO"
```

### 3. Contracts

**环境变量**

| 键 | 必填 | 取值 | 默认 | 同步点 |
|---|---|---|---|---|
| `AGENTOS_LOG_LEVEL` | 否 | `DEBUG`/`INFO`/`WARNING`/`ERROR`/`CRITICAL`（大小写不敏感） | `INFO` | `core/config.py` + `.env` + `.env.example` 三处缺一不可 |

**输出行格式**

```
<asctime> <levelname> <logger name> <中文消息> key=value key=value
```

- `extra` 传入的字段按插入顺序追加到**消息行尾**；
- 无 `extra` 时行尾**不得**有多余空格；
- `LogRecord` 内置属性（`pathname`/`lineno`/`msecs`/`taskName` 等）不得外泄。

### 4. Validation & Error Matrix

| 条件 | 行为 |
|---|---|
| `AGENTOS_LOG_LEVEL` 未设置 | 取代码默认 `INFO` |
| 取值合法（任意大小写） | 生效 |
| 取值非法（如 `VERBOSE`） | 启动即抛中文 `AgentException`（"日志级别配置非法"），**不静默回落默认值** |
| `.env` 有键但 `Settings` 无字段 | `extra="forbid"` 导致启动失败 —— 这是保护不是缺陷 |
| 根级别降到 `DEBUG` | 第三方 SDK logger 仍压在 `INFO`（见下方安全约束） |
| 根级别高于 `INFO`（如 `WARNING`） | 第三方同步抬高，不得比根更吵 |

### 5. Good / Base / Bad Cases

- **Good**：`logger.info("记忆召回完成", extra={"session_id": 42, "selected": 3})`
  → `... 记忆召回完成 session_id=42 selected=3`
- **Base**：无 `extra` 的普通日志 → 原样输出，行尾无空格
- **Bad**：`logger.info(f"会话 {sid} 召回完成")` → 禁止 f-string 内插，
  上下文必须走 `extra`，否则无法 grep 字段、也无法被聚合系统解析

### 6. Tests Required

`tests/test_logging_formatter.py`（基建）与 `tests/test_logging_chain.py`（链路语义）：

| 断言点 | 说明 |
|---|---|
| `extra` 字段出现在消息行尾 | 渲染契约 |
| 无 `extra` 时 `output == output.rstrip()` | 行尾无多余空格 |
| 异常日志字段在 `lines[0]`、`lines[-1] == "ValueError: boom"` | **核心防回归** |
| 内置属性不出现在输出中 | 探测法有效 |
| 非法级别抛中文 `AgentException` | 无静默兜底 |
| 根 `DEBUG` 时第三方 logger `level == INFO` | 防 Prompt 泄漏 |
| 失败日志 `record.exc_info is not None` | 证明是 `exception()` 而非 `error()` |

### 7. Wrong vs Correct：Formatter 必须覆盖 `formatMessage()`

`logging.Formatter.format()` 的顺序是**先**生成消息行、**再**把 traceback 拼在其后。
在 `format()` 里追加 `extra`，异常日志的字段会落到**堆栈最后一行**。

#### Wrong

```python
class _KeyValueFormatter(logging.Formatter):
    def format(self, record):
        return f"{super().format(record)} {pairs}"   # 字段被拼到 traceback 末行
```

```
Traceback (most recent call last):
  ...
ValueError: boom tool=Bash session_id=42     # ← 堆栈被污染，无法解析
```

#### Correct

```python
class _KeyValueFormatter(logging.Formatter):
    def formatMessage(self, record):
        message = super().formatMessage(record)
        pairs = " ".join(
            f"{k}={v}" for k, v in record.__dict__.items() if k not in _BUILTIN_FIELDS
        )
        return f"{message} {pairs}" if pairs else message
```

```
2026-08-28 16:31:55 ERROR tools.service 工具执行失败 tool=Bash session_id=42
Traceback (most recent call last):
  ...
ValueError: boom
```

> **内置属性集用探测法生成，不要硬编码清单**：
> `set(logging.LogRecord("", logging.INFO, "", 0, "", None, None).__dict__) | {"asctime", "message"}`。
> Python 3.14 的 `LogRecord` 已含 `taskName`，硬编码清单会随版本失效。

---

## Gotchas

### Gotcha: DEBUG 档会经第三方 SDK 泄漏完整 Prompt

> **Warning**：`openai._base_client`、`httpcore2` 等在 `DEBUG` 级别会把**完整请求体**
> （system prompt 与对话正文）打进日志，直接违反本文档「严禁记录 Prompt 正文」的约束。

引入 `AGENTOS_LOG_LEVEL=DEBUG` 就等于打开了这个泄漏面，必须由压级名单挡住：

```python
# 取根级别与 INFO 的较高者：根降到 DEBUG 时第三方停在 INFO，
# 根高于 INFO 时同步抬高，避免第三方比根更吵。
floor = max(logging.getLevelNamesMapping()[level_name], logging.INFO)
for name in _THIRD_PARTY_FLOOR_LOGGERS:
    logging.getLogger(name).setLevel(floor)
```

名单需同时覆盖 `httpx2`/`httpcore2`（本项目实际分发名，其内部 logger 名即为 `"httpx2"`）
与 `httpx`/`httpcore`，防依赖切换后失效。

**实测对照**：修复前后同一次问答，含 Prompt 正文的日志行 2 → 0，
第三方 DEBUG 行 62 → 0，日志体积 132K → 4.0K，而本项目自己的 DEBUG 埋点 5 条不受影响。

### Gotcha: 本项目 `.env` 覆盖 shell 环境变量

`core/config.py` 的 `settings_customise_sources` **故意**把 `.env` 排在 shell env 之前
（防残留环境变量劫持配置的既有设计）。因此下面这条验证命令在本项目**无效**：

```bash
# ❌ 无效：.env 的值会覆盖它
AGENTOS_LOG_LEVEL=DEBUG uv run uvicorn main:app

# ✅ 有效：改 .env
sed -i '' 's/^AGENTOS_LOG_LEVEL=.*/AGENTOS_LOG_LEVEL=DEBUG/' .env
```

调整任何 `AGENTOS_*` 配置做临时验证时都适用，验证后记得还原。

### Gotcha: 回滚时必须同步删除 `.env` 中的配置键

`Settings.model_config` 为 `extra="forbid"`。`git revert` 掉代码后，
`.env` 里残留的 `AGENTOS_LOG_LEVEL` 会因字段不存在导致**启动直接失败**。
凡新增配置项，回滚步骤都必须包含「删除 `.env` 中对应键」。
