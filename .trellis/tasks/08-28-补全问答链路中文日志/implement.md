# 实施计划：补全问答链路中文日志

## 执行顺序

基建先行，再按链路顺序补埋点。每一步都可独立验证，出问题时回退面小。

### 步骤 0：基线留存

```bash
uv run pytest 2>&1 | tail -20
```

记录当前通过/失败项作为对照。**改造前必须先跑**，避免把既有失败算到本次头上。

### 步骤 1：KV Formatter（R1.1）

- 文件：`core/logging.py`
- 实现 `_KeyValueFormatter`，覆盖 `formatMessage()`（**不是 `format()`**，
  见 design.md 一.1 的实测结论）；
- 内置属性集用探测法 + `{"asctime", "message"}`；
- `setup_logging()` 换成显式 `StreamHandler`，注意清理 uvicorn 预装 handler
  避免重复输出。

验证：

```bash
uv run python -c "
from core.logging import setup_logging
import logging
setup_logging()
logging.getLogger('t').info('记忆召回完成', extra={'session_id':42,'selected':3})
try: raise ValueError('boom')
except Exception: logging.getLogger('t').exception('工具执行失败', extra={'tool':'Bash'})
"
```

期望：字段在消息行尾；异常日志的字段**不在**堆栈末行；无 extra 时行尾无空格。

### 步骤 2：日志级别配置项（R1.2）

- `core/config.py` 加 `log_level: str = "INFO"`；
- `.env` 与 `.env.example` 同步加 `AGENTOS_LOG_LEVEL=INFO`；
- `setup_logging()` 读取；非法值抛中文 `AgentException`。

验证：

```bash
uv run python -c "from core.config import settings; print(settings.log_level)"
AGENTOS_LOG_LEVEL=DEBUG uv run python -c "from core.config import settings; print(settings.log_level)"
AGENTOS_LOG_LEVEL=VERBOSE uv run python -c "import core.logging as l; l.setup_logging()"   # 应抛中文异常
```

⚠️ `Settings.model_config` 为 `extra="forbid"`——三处漏改任一处启动即失败。

### 步骤 3：基建收尾（R1.3、R1.4）

- `core/logging.py:28` 英文 `"request finished"` 改中文；
- `memory/jobs/worker.py:103` 的独立 `basicConfig` 改调 `setup_logging()`。

### 步骤 4：主链路埋点（R2、R3、R4）

按数据流顺序，逐个文件推进：

| 序 | 文件 | 内容 |
|---|---|---|
| 4.1 | `session/api.py:43` | 收到用户提问（`prompt_chars`，**不记正文**） |
| 4.2 | `interaction/api.py:83` | 交互恢复入口 |
| 4.3 | `runtime/agent.py:630` | 每轮 LLM 调用开始 + 单轮耗时 |
| 4.4 | `runtime/stream_assembler.py` | 首 token TTFT（单次 consume 内标志位） |
| 4.5 | `tools/service.py:165` | 工具开始/完成/失败（**带 tool_name**，不记入参） |
| 4.6 | `runtime/agent.py:913-935` | 主代理侧权限 ask 挂起 / deny 拒绝 |
| 4.7 | `runtime/agent.py:1043`/`:321` | 人工交互挂起/恢复/取消 |
| 4.8 | `runtime/agent.py:704`/`:712` | 回合结束：stop_reason、rounds、token 用量、总耗时 |
| 4.9 | `runtime/agent.py:144-151`、`:222-231` | 客户端断连取消记 warning |

### 步骤 5：记忆链路埋点（R5）

| 序 | 文件 | 内容 |
|---|---|---|
| 5.1 | `memory/recall/service.py:60` | 召回开始/完成（命中数、耗时）；灰度跳过记 DEBUG |
| 5.2 | `memory/recall/service.py:144` | 选择器候选集规模（DEBUG） |
| 5.3 | `runtime/agent.py:732` | 提取任务入队（job_id、shadow/active） |
| 5.4 | `runtime/agent.py:748` | 就地执行开始 |
| 5.5 | `memory/jobs/service.py:274` | 已有完成日志补 `session_id` / `turn_id` |

### 步骤 6：派生执行层埋点（R6）

| 序 | 文件 | 内容 |
|---|---|---|
| 6.1 | `runtime/subagent.py:85` | 子代理启动/完成/失败（agent_type、轮数、耗时） |
| 6.2 | `runtime/subagent.py:400` | teammate 唤醒与失败 |
| 6.3 | `runtime/agent.py:1714-1727` | **teammate 失败捕获处补 `logger.exception`**（消除静默消失缺口） |
| 6.4 | `runtime/compact.py:31` | 是否触发压缩、压缩前后 token 估算 |

### 步骤 7：测试

新增 `tests/test_logging_formatter.py`：

- 带 `extra` → 字段出现在消息行尾；
- 不带 `extra` → 行尾无多余空格；
- 异常日志 → 字段在消息行、traceback 完整且字段不在其末行；
- 内置属性（`pathname` / `lineno` / `msecs` / `taskName`）不外泄；
- `AGENTOS_LOG_LEVEL` 默认/合法/非法三态。

### 步骤 8：端到端验证

按 PRD 的 AC3 / AC4 / AC5 做真实问答验证：

1. 默认 INFO 档发起一次**触发工具调用**的问答，确认日志按序完整（AC3）；
2. `grep "session_id=<本次>"` 确认一次捞全且顺序连贯；
3. 临时把 `memory_recall_rollout_percent` 调到 100，确认召回日志出现（AC4）；
4. 触发 Agent 工具确认子代理日志；monkeypatch 注入 teammate 异常确认
   失败不再静默消失（AC5）；
5. 普通无工具问答确认 INFO 档 ≤ 10 行（AC6）。

## 验证命令

```bash
uv run pytest                                    # 全量回归
uv run pytest tests/test_logging_formatter.py -v # 新增测试
uv run ruff check .                              # 行宽 100、Python 3.14
uv run python -c "from core.config import settings; print(settings.log_level)"
```

新增日志规范自检：

```bash
# 新增条目应为中文、无 f-string 内插
grep -rn 'logger\.\(info\|warning\|error\|exception\|debug\)(f"' --include="*.py" . | grep -v .venv
# 产品代码不得新增 print
grep -rn "^\s*print(" --include="*.py" . | grep -v .venv | grep -v /tests/ | grep -v .claude | grep -v .trellis
```

## 风险文件与回滚点

| 文件 | 风险 |
|---|---|
| `core/logging.py` | 基建改造，Formatter 出错会影响**全部**日志输出。步骤 1 单独验证后再往下 |
| `core/config.py` + `.env` + `.env.example` | `extra="forbid"`，三处必须同步，漏一处启动即失败 |
| `runtime/agent.py` | 1732 行、改动点最多（9 处），逐块改逐块验证，勿一次性大改 |
| `runtime/stream_assembler.py` | TTFT 标志位必须是单次 consume 内局部状态，勿污染跨调用的块 index |
| `tools/service.py` | 失败分支在事务与 hook 之间，加日志勿改 raise 时序 |

**回滚陷阱**：`git revert` 代码后，`.env` 中残留的 `AGENTOS_LOG_LEVEL` 会因
`extra="forbid"` 导致启动失败——**回滚时必须同步删除该键**。

## 开工前检查

- [ ] 步骤 0 基线测试结果已留存
- [ ] 确认 `.env` 中数据库/Redis 连接可用（端到端验证需要）
- [ ] 确认不改动既有 38 条日志文案（除 R1.3）
- [ ] 确认无 Alembic 迁移需求（本任务不碰 ORM）
