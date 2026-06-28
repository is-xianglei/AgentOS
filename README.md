# AgentOS

AgentOS 是一个面向 Agent 会话运行的后端基座。第一阶段聚焦会话主链路：创建会话、发送消息、SSE 流式返回、记录消息历史、记录工具调用、上下文压缩与恢复。

## 当前范围

- 已实现 FastAPI 应用入口：`app.main:app`
- 已实现 Session、Message、Snapshot、ToolCall、Task、Team 数据模型
- 已实现统一响应和统一错误格式
- 已实现示例工具 `echo`
- 已实现 `/docs` 和 `/openapi.json`

## 启动

```bash
uv run uvicorn app.main:app --reload
```

默认会读取 `.env` 中的 `DATABASE_URL`，且必须是 PostgreSQL 连接地址。正式建表请使用 Alembic：

```bash
uv run alembic upgrade head
```

如需在本地临时开发时自动建表，可以设置 `AGENTOS_CREATE_TABLES=1`。

运行态状态通过主会话消息接口的 SSE 返回；持久化状态由消息、工具调用、任务、团队和子代理运行表分别记录。

多台本地或远程实例连接同一个数据库调试子代理时，可以开启团队任务消费隔离：

```bash
AGENTOS_TEAM_TASK_DEBUG_ISOLATION=1
AGENTOS_INSTANCE_ID=local-dev
```

开启后，本实例通过 Team/Agent 派发的 `task` 消息只会被相同 `AGENTOS_INSTANCE_ID`
的实例消费，避免远程进程误取本地调试任务。

## 示例

发送第一条消息会自动创建会话，标题由首条消息浓缩生成：

```bash
curl -N -X POST http://127.0.0.1:8000/api/sessions/messages \
  -H 'Content-Type: application/json' \
  -d '{"content":"你好"}'
```

继续已有会话时，在请求体中带上 `session_id`：

```bash
curl -N -X POST http://127.0.0.1:8000/api/sessions/messages \
  -H 'Content-Type: application/json' \
  -d '{"session_id":1,"content":"继续刚才的话题"}'
```

SSE 使用单层 JSON 数据，事件类型放在 `type` 字段：

```text
data: {"id":12,"type":"task_created","session_id":1,"data":{"subject":"写文档"}}
```

触发示例工具：

```bash
curl -N -X POST http://127.0.0.1:8000/api/sessions/messages \
  -H 'Content-Type: application/json' \
  -d '{"session_id":1,"content":"/tool echo hello"}'
```

触发旧工具适配后的天气工具：

```bash
curl -N -X POST http://127.0.0.1:8000/api/sessions/messages \
  -H 'Content-Type: application/json' \
  -d '{"session_id":1,"content":"/tool Weather 上海"}'
```

创建数据库任务：

```bash
curl -N -X POST http://127.0.0.1:8000/api/sessions/messages \
  -H 'Content-Type: application/json' \
  -d '{"session_id":1,"content":"/tool Task {\"action\":\"create\",\"subject\":\"写文档\",\"description\":\"补充API文档\"}"}'
```

创建团队成员并分派子代理任务：

```bash
curl -N -X POST http://127.0.0.1:8000/api/sessions/messages \
  -H 'Content-Type: application/json' \
  -d '{"session_id":1,"content":"/tool Agent {\"name\":\"Researcher\",\"role\":\"资料员\",\"prompt\":\"整理上海天气信息\"}"}'
```

同名子代理会被复用，新的 prompt 会追加为新的 task 消息。

运行子代理待处理任务：

```bash
curl -N -X POST http://127.0.0.1:8000/api/sessions/messages \
  -H 'Content-Type: application/json' \
  -d '{"session_id":1,"content":"/tool Agent {\"action\":\"run_pending\",\"name\":\"Researcher\"}"}'
```

查询子代理运行历史：

```bash
curl http://127.0.0.1:8000/api/teams/subagent-runs?session_id=1
```

真实 LLM 返回 `tool_use` 时，后端会通过 `ToolRunner` 执行工具，通过 SSE 返回 `tool_start/tool_done/tool_error` 状态，记录 `tool_calls`，并把 `tool_result` 回灌给模型继续生成。

## 测试

```bash
uv run pytest
```
