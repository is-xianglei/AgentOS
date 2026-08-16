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
