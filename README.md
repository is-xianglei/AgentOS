# AgentOS

AgentOS 是一个 Agent 会话运行的后端基座（Python 3.14 + FastAPI + PostgreSQL）。它负责多租户工作区下的会话、
消息历史与 SSE 流式返回，Agent 的 ReAct 循环、工具调用与人工审批，子代理、队友与 Workflow 编排，
上下文压缩与长期记忆，并同时支持 Anthropic 和 OpenAI 两家模型。

## 架构概览

Agent 核心在 `harness/` 内按「领域层 / 应用层 / 适配层 + 组装根」分层，依赖方向为 adapters → app → domain；
其余业务按 feature 包组织。设计、分层规则与扩展点见 [docs/agent-architecture.md](docs/agent-architecture.md)。

```text
harness/
├── domain/        领域层：消息、Context、Tool、审核结论、角色与提示文案；纯逻辑，不做 I/O
├── app/           应用层：端口（ports.py 等）、ReAct 循环（loop.py）、AgentService、ToolSet、上下文压缩
├── adapters/      适配层：端口的实现，包括模型客户端、内置工具、审核规则链、内置记忆
└── bootstrap.py   组装根：唯一实例化适配器的地方，把实现装配进运行时
runtime/           运行时编排：主循环策略、子代理与队友、Workflow、后台执行、租约与回收；只认端口
session/ memory/ skill/ user/ team/ task/ ...   业务域：models / schemas / repository / service / api
core/              配置、统一错误、事件类型、事件流与 SSE
api/               路由汇总（router.py）与依赖注入（deps.py）
database/          ORM 基类与实体登记（registry.py）；迁移在 alembic/
```

## 启动

依赖用 uv 管理。首次启动前把 `.env.example` 复制为 `.env` 并填写，再执行迁移：

```bash
uv sync
uv run alembic upgrade head
uv run uvicorn main:app --reload
```

`.env` 里的关键配置项：

- `AGENTOS_DATABASE_URL`：PostgreSQL 连接地址。
- `AGENTOS_REDIS_URL`：run 事件流。启动时会自检，连不上就启动失败；单进程本地调试可以设
  `AGENTOS_DISABLE_REDIS_EVENTS=true`，改用进程内后端。
- `AGENTOS_JWT_SECRET_KEY`：签发访问令牌的密钥。
- `AGENTOS_LLM_PROVIDER`（anthropic / openai），以及对应的 `AGENTOS_ANTHROPIC_BASE_URL`、
  `AGENTOS_ANTHROPIC_API_KEY`、`AGENTOS_ANTHROPIC_MODEL` 或 `AGENTOS_OPENAI_BASE_URL`、
  `AGENTOS_OPENAI_API_KEY`、`AGENTOS_OPENAI_MODEL`。工作区里配置了模型时优先用工作区的，这组配置只作回退。
  Anthropic 的 base_url 不带 `/v1`，OpenAI 的必须带 `/v1`。
- `AGENTOS_CREATE_TABLES`：开发时启动自动建表；正式环境用 Alembic。
- `AGENTOS_TEAM_TASK_DEBUG_ISOLATION` 与 `AGENTOS_INSTANCE_ID`：多个实例连同一个库调试时开启，
  本实例派发的团队任务只被 `AGENTOS_INSTANCE_ID` 相同的实例消费。

记忆的抽取、整理（dream）与保留清理由独立 Worker 执行：`uv run python -m memory.jobs.worker`。
接口文档在 `/docs`。

## 发送消息

接口需要 Bearer 令牌，令牌里带着当前工作区。先注册（`POST /api/auth/register`，字段 email、username、
password），再登录取令牌：

```bash
TOKEN=$(curl -s -X POST http://127.0.0.1:8000/api/auth/login \
  -H 'Content-Type: application/json' \
  -d '{"email":"<邮箱>","password":"<密码>"}' | jq -r .access_token)
```

不带 `session_id` 发送第一条消息时会自动创建会话，标题取自首条消息：

```bash
curl -N -X POST http://127.0.0.1:8000/api/sessions/messages \
  -H "Authorization: Bearer $TOKEN" \
  -H 'Content-Type: application/json' \
  -d '{"content":"你好"}'
```

继续已有会话时，在请求体里带上 `session_id`：

```bash
curl -N -X POST http://127.0.0.1:8000/api/sessions/messages \
  -H "Authorization: Bearer $TOKEN" \
  -H 'Content-Type: application/json' \
  -d '{"session_id":1,"content":"继续刚才的话题"}'
```

请求体还支持 `agent_id` 或 `workflow_id`（二选一，会话建立后不能切换）、`model_provider_id`、
`mode_slug`（standard / turbo）和 `thinking_effort`（fast / low / medium / high / xhigh / max），
Schema 见 `session/schemas.py` 的 `SessionSendMessageRequest`。

### SSE 事件

每帧是一行 `data: <JSON>`，事件类型放在 `type` 字段；长时间没有事件时会发 `: heartbeat` 注释行保活。
事件定义在 `core/events.py`：

| 类别 | type | 说明 |
|---|---|---|
| 执行边界（RunEvent） | `run_started`、`run_end` | 第一帧是带 `turn_id` 的 `run_started`；`run_end` 的 `status` 取 completed / awaiting_interaction / failed / interrupted，读到它即结束 |
| 会话级（RuntimeEvent） | `session_ready`、`task_snapshot` | 会话元信息；任务全量快照 |
| 产出者事件（StreamEvent，`EventType`） | `turn_start`、`turn_end`、`block_start`、`block_stop`、`thinking_delta`、`text_delta`、`tool_use`、`tool_result`、`interaction_request`、`error`、`workflow_progress` | 带 `session_id`、`actor`、`sequence` |

```text
data: {"type":"text_delta","session_id":1,"actor":{"role":"orchestrator","name":"orchestrator"},"sequence":5,"block":{"index":0},"text":"你好"}
```

- `actor.role` 取 orchestrator（主循环）、subagent（Agent 工具派出的一次性子代理）、teammate（团队成员）。
- `workflow_progress` 只出现在 Workflow 会话里：`progress.phase` 取 member / routing / merging，
  `progress.status` 取 running / done / failed；成员事件的 `actor.agent_id` 用来把事件归到工作流图的节点。
- `interaction_request` 表示本轮挂起、等待人工答复，此时 `run_end` 的 status 为 awaiting_interaction。

相关接口：

- `GET /api/sessions/{session_id}/active-turn`：查询最新轮次和执行状态。
- `GET /api/sessions/{session_id}/turns/{turn_id}/subscription`：断线重连。先发一帧 `run_snapshot`，再接着推送后续事件。
- `POST /api/sessions/{session_id}/turns/{turn_id}/cancel`：取消执行中的轮次。
- `POST /api/interactions/sessions/{session_id}/requests/{request_id}/resolve`：答复人工交互，恢复执行。

客户端断开只会停止推送，后台执行不受影响。

## 测试

```bash
uv run pytest
```

PostgreSQL、Redis 集成测试（`tests/test_*_pg.py` 等）读取 `.env` 里的连接，连不上时自动跳过。

- 黄金测试：八份 `tests/test_*_golden.py` 用 `tests/fixtures/*_golden.py` 里的期望字面量逐项比对对外行为
  （含字典键序），字面量取自重构前代码的实际产出。分别固化发给模型的请求（model_request）、
  工具描述（tool_spec）、审核结果（tool_review）、记忆接入（memory_port）、角色上下文（role_context）、
  循环事件与落库（react_loop）、HTTP 入口（agent_service），以及 MCP 工具发给模型的 tools、
  审批推导与结果转换（mcp_tool，`tests/test_mcp_tool_golden.py`）。改这些行为时要先说明原因，
  并重新生成字面量，不允许手改。
- 分层静态检查：`tests/test_import_isolation.py` 用 AST 检查导入关系，例如 runtime、app、domain
  不得导入 adapters，runtime 和 app 不得导入 Repository，领域层不得读取 cwd、日期等进程状态；
  还会逐个起子进程导入全部模块，防止加载期出现循环导入。

## MCP

AgentOS 作为 MCP 客户端，按工作区接入远程 MCP server（只支持 Streamable HTTP），设计见
[docs/mcp-design.md](docs/mcp-design.md)。

- 开关：`AGENTOS_MCP_ENABLED`（默认 true）。关闭时回合不加载任何 MCP 工具，管理接口返回 403。
  其余超时与上限见 `.env.example` 的 MCP 段。
- 管理接口前缀 `/api/mcp`：`GET/POST /servers`、`GET/PUT/DELETE /servers/{id}`、
  `POST /servers/{id}/probe`（重新探测，成功后本实例下一回合即用新清单）、`GET /servers/{id}/tools`。
  新建时先探测，探测成功才写库。
- 共享 server（`visibility=workspace`）由工作区 owner / admin 配置，成员都能用；私有 server
  （`visibility=private`）由成员自己配置，只出现在本人的回合里，别人看不到，地址必须是公网地址。
- 工具名为 `mcp__{server_key}__{tool}`，排在内置工具之后。审批按 server 的 `approval_mode`
  （默认每次审批）；MCP 工具审批时不能改参数，「始终允许」选全局作用域要求 owner / admin。
- 谁能用 MCP 工具：默认 Agent 加载工作区全部已启用的共享 server 和本人的私有 server；
  自建 Agent 只加载经 `PUT /api/agents/{id}/mcp-servers` 绑定的共享 server（可带工具白名单，
  私有 server 不能绑定）；委派取被委派 Agent 的绑定与父回合可见工具的交集；general-purpose
  子代理和队友继承父回合可见的 MCP 工具，Explore、Plan、verification 不带；Workflow 成员各按
  自己的绑定加载。极速模式、Plan 模式下不可用。删除 server 时同时解除全部 Agent 绑定。

## 扩展

- **接入新的作用域工具来源**：实现 `harness/app/ports.py` 的 `ScopedToolProvider`，按工作区和用户
  把每个远端工具包装成 `harness/domain/tool.py` 的 `Tool`，再经 `Harness.scoped_tools` 注入（MCP 即
  `harness/adapters/mcp/provider.py` 的 `McpToolProvider`）。主 Agent 运行时每回合由 `TurnToolResolver`
  解析一次（Workflow 运行时在执行体开头解析一次），作用域工具排在内置工具之后；
  `platform_tool_set()` 只放进程级工具。
- **接入外部记忆**（尚未实现）：设计见 [docs/agent-architecture.md](docs/agent-architecture.md) 第 5 节。
  实现 `harness/app/memory_port.py` 的 `MemoryProvider`（recall、inherited、
  on_turn_completed、after_commit），在 `harness/bootstrap.py` 里替换 `Harness` 的 `memory` 字段
  （如 `dataclasses.replace(builtin_harness(), memory=...)`）。主循环只认召回和回合结束写入两个动作，
  运行时代码不用改。
