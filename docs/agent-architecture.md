# AgentOS Agent 核心架构（重构目标）

本文是 Agent 核心重构的设计依据，各阶段实现以本文为准，阶段内细化出的签名回写到本文。
思路来自 B 站视频「10分钟学会设计 Agent 架构」（BV1winH6DEgt）：先梳理流程，领域层定义系统语言，
应用层编排流程并声明端口，适配层实现端口，组装根把具体类接起来。

## 1. 分层与依赖规则

Agent 核心收进 `harness/` 包，内部按层分目录；其余业务域（session、memory、skill、user 等）仍按 feature 包组织。

| 层 | 位置 | 可以依赖 | 不得依赖 |
|---|---|---|---|
| 领域层 | harness/domain/ | 标准库、pydantic、`core.errors`（统一业务异常）、`core.events`（纯值类型） | app、adapters、其余 feature 包、SQLAlchemy、FastAPI、各家 SDK（类型标注可放 TYPE_CHECKING） |
| 应用层 | harness/app/ | domain、core（配置、错误、事件）、feature 域的 Service | adapters、任何 Repository 和 ORM 查询、各家 SDK |
| 适配层 | harness/adapters/ | 以上全部、各家 SDK | 被 domain、app 导入 |
| 组装根 | main.py（进程级单例）、harness/bootstrap.py（按数据库会话装配） | 全部 | — |

- 只有组装根实例化适配器，应用层只认端口。
- 端口只为两类东西设立：已有或已规划多种实现的外部能力，以及测试需要替身的 I/O。
  其余 feature 域照 CLAUDE.md 继续「Service 调 Service」，不额外造端口。
- 分层规则由 tests/test_import_isolation.py 做静态检查（目前覆盖领域层，按阶段扩展到 app、adapters）。
  已知缺口：`core.errors` 在模块顶部导入了 fastapi（异常处理器），领域层经它间接依赖 FastAPI；
  静态检查只看直接导入。阶段 7 把 AgentException 和 HTTP 异常处理器拆到两个模块。

## 2. 领域层（harness/domain/）

| 文件 | 内容 |
|---|---|
| messages.py | 已落地（阶段 1）。`Message = UserMessage \| AssistantMessage \| ToolResult`；`AssistantMessage(blocks)` 的块为 `AssistantBlock = ThinkingBlock \| TextBlock \| ToolCall`；`ToolCall(id, tool_name, arguments)`；`ToolResult(tool_call_id, tool_name, arguments, content, is_error)`；`message_from_row(role, content)` 按角色还原 |
| session.py | `Session(id, messages, append)`：一次上下文加载得到的会话视图，不是 ORM |
| context.py | `Context[O](instruction, tools, messages, output_type, memory, project_instructions)` |
| tool.py | `Tool`、`ToolPolicy`、`ToolContext` |
| review.py | `Decision(behavior, reason, updated_arguments)`，behavior 取 allow / ask / deny |
| roles.py | 角色 `Assistant`（主 Agent）、`SubAgent`、`Teammate`、`Compactor`，各自 `context(...) -> Context[...]` |
| budget.py | `ToolCallBudget`：循环额度，可序列化进挂起的续跑状态 |
| prompts/ | 各角色的提示文案，逐字保持现状 |

与视频有意不同的地方：
- AssistantMessage 存块序列，不是 `(text, tool_calls)`：思考块必须保持原序并带签名原样回传
  （Anthropic 要求）。text、tool_calls、thinking 是从块序列派生的只读视图。
- ToolResult 多带 tool_name、arguments、is_error，落库排障和审计要用，不发给模型。
- 落库 JSON 形状不变，assistant 块数组的判别键仍是 `kind`。字段名用领域语言，JSON 键名是落库契约，
  两者有意不同：

  | 类型 | 字段名 | JSON 键名 |
  |---|---|---|
  | ToolCall | tool_name / arguments | name / input |
  | ToolResult | tool_call_id / arguments / content | tool_use_id / input_args / output |
- Decision 是三态，ask 表示需要人工审批，本轮挂起，等审批后恢复。
- Tool 用 JSON Schema 描述参数（`parameters: dict[str, Any]`），不再以 Pydantic 类为准：
  内置工具由 `input_model` 派生 schema，MCP 工具直接用 server 下发的 inputSchema。
- Tool 自带 `policy`：原 tools/policy.py 按工具名查表，改成平移到每个工具身上，
  审核按工具元数据判定，所以外部来源的工具也能被审核。
- `execute(arguments, ctx)` 多一个 ToolContext，因为工具要用到会话、工作区、权限档位等运行上下文。
- Effort 沿用现有取值 fast / low / medium / high / xhigh / max，不照搬视频的 Off（fast 不等于关闭思考）。

## 3. 应用层（harness/app/）

端口集中在 ports.py：

| 端口 | 职责 | 现有实现 | 规划中的实现 |
|---|---|---|---|
| ModelClient | `stream(context, settings)`、`complete_structured(context, settings)` | Anthropic、OpenAI | Google |
| ModelSettings | 每次调用时传入 model_id 和 effort，档位到各家参数的翻译放在适配器里 | — | — |
| SessionStore | 按实际操作设计：加载本轮上下文、追加消息、写压缩快照；不做整存整取 | 数据库（包装 SessionService） | 测试用内存实现 |
| ToolProvider | `list_tools(scope) -> list[Tool]`，代表一个工具来源 | 内置工具 | MCP |
| MemoryProvider | `recall(request) -> MemoryRecall`，以及回合结束后的写入 | 内置 memory 域 | 外部记忆框架 |
| ToolCallReviewer | `review(...) -> Decision` | 规则链（原 7 层审核合一） | LLM 审核员 |

编排：
- `AgentService` 对外提供 new_session / list_sessions / chat / resume / cancel。主 Agent、子代理、队友
  共用唯一一份 ReAct 循环（loop.py）。
- `ToolSet` 聚合多个 ToolProvider，负责命名空间和重名校验、按模式和档位裁剪，并保证工具顺序稳定（提示缓存依赖这一点）。
- 后台执行与租约、SSE 跟随、孤儿回收、取消还原、Workflow 编排、Hook 注册表也都放在应用层。

## 4. 适配层（harness/adapters/）

| 目录 | 内容 |
|---|---|
| models/ | AnthropicClient、OpenAIClient；factory 负责解析 .env 或工作区模型配置并建客户端 |
| stores/ | DatabaseSessionStore |
| tools/ | builtin/（全部内置工具，Bash 的 shell 校验也在这里）、builtin_provider.py |
| memory/ | BuiltinMemoryProvider（包装 memory 域的召回和提取） |
| reviewers/ | PolicyReviewer（档位能力、Skill 白名单、Plan 模式、权限规则、预算等合成一条规则链） |

HTTP 路由仍放在各 feature 包的 api.py，起 WebApi 适配器的作用：路由只把请求转成对 AgentService 的调用，
不再自己 new 运行时。

## 5. 扩展点

### MCP（后续实现）
- 以 ToolProvider 接入。`McpToolProvider` 读取工作区的 MCP server 配置，连接后把每个远端工具包装成 `Tool`。
- 命名用 `mcp__{server}__{tool}`，由 ToolSet 统一做命名空间和重名校验。
- 参数直接用 server 下发的 inputSchema，这也是领域层 Tool 以 JSON Schema 为准的原因。
- 审核按工具自带的 policy 判定。MCP 工具按 annotations（readOnlyHint、destructiveHint）映射，没有标注的一律需要审批。
- 连接管理器是进程级单例，在 main.py 的 lifespan 里创建和关闭；Provider 每轮只取用连接。
- 领域层 ToolResult.content 目前是 str。MCP 返回的图片、资源怎么呈现，实现 MCP 时再定。
- MCP 工具要不要进 tools 表供 Agent 绑定，实现 MCP 时再定。

### 外部记忆框架（后续实现）
- 以 MemoryProvider 接入。主循环只认召回和回合结束写入两个动作，不关心记忆怎么存、怎么检索。
- MemoryRecall 分两部分：进系统指令的部分（使用规则、目录），进本轮用户消息前缀的部分（召回正文），
  分别对应现在的 memory_catalog 和 rendered_memories。
- 同一回合重复召回结果相同：子代理经同一个 Provider 拿到父回合冻结下来的结果，不再直接读 memory 的 Repository。
- 回合结束写入失败不得影响回合终态。内置实现「入队与回合终态同一事务、用 SAVEPOINT 隔离」的语义保留在内置适配器内部。
- 内置 memory 域的管理接口（条目增删改查、dream、保留策略）属于内置实现自己的功能，不进入端口。
- 用哪个 Provider 由组装根决定；接入第二种实现时再新增配置项，并遵守配置同步规则。

## 6. 现有代码去向

| 现位置 | 去向 |
|---|---|
| llm/types.py | domain/messages.py；流式事件类型放 app/ports.py |
| llm/base.py、llm/errors.py | app/ports.py（ModelClient 及其错误类型） |
| llm/anthropic_client.py、openai_client.py | adapters/models/ |
| llm/factory.py、runtime/model_selection.py | adapters/models/factory.py，由组装根调用 |
| tools/base.py、tools/policy.py 里的 ToolPolicy | domain/tool.py |
| tools/builtin/、tools/shell_policy.py | adapters/tools/builtin/ |
| tools/subagents/ | 子代理规格放 domain/roles.py，提示文案放 domain/prompts/ |
| tools/registry.py | app/tool_set.py + adapters/tools/builtin_provider.py |
| tools/gateway.py、policy_engine.py、policy.py | adapters/reviewers/policy_reviewer.py |
| tools/service.py 里的 ToolService | app/tool_executor.py（执行与钩子）；调用记录仍经 tools 域写入 |
| tools/ 的 ToolCatalogService、models、repository、api | 留在 tools/，工具目录和调用记录属于平台数据 |
| prompt/ | domain/prompts/，由角色负责组装 |
| hooks/ | app/hooks.py |
| runtime/agent.py、subagent.py、step.py、stream_assembler.py | app/agent_service.py、app/loop.py |
| runtime/compact.py | domain/roles.py 里的 Compactor，加上应用层的调用 |
| runtime/budget.py | domain/budget.py |
| runtime/execution.py、turn_follow.py、recovery.py、cancel_partial.py | app/ 下同名模块 |
| runtime/orchestrator.py、workflow.py | app/workflow_runner.py |
| runtime/checkpoint.py、runtime/models.py | 删除（没有调用方的死代码） |
| core/ 的事件、事件流与 SSE | 留在原位：StreamBus、RunEventStore 已经是端口 + 适配器的形态 |
| session、memory、interaction、plan、permission、model_service、skill、team、task、agent、workflow | 保留为 feature 包，由适配器或应用层通过它们的 Service 调用 |

## 7. 重构期间不变的契约

- HTTP 接口的路径和请求/响应 Schema，SSE 事件的类型和字段。
- 数据库表结构（死代码对应的表除外），session_messages、session_snapshots 里消息 JSON 的形状。
- 发给 LLM 的提示文本、工具名、工具描述、工具 JSON Schema。
- 事务边界和提交时点、租约与并发顺序、取消与回收语义。持久化仍按轮次和挂起点提交，不改成逐条提交。
- 测试可以改 import、patch 目标和构造方式；断言语义不变，新端口要补测试。
  测试不删，只有专测已删除功能的用例可以随功能一起删掉，并在汇报里逐个列出。

## 8. 阶段

| 阶段 | 内容 |
|---|---|
| 0 | 删除编排检查点死代码；agent 域的 `AgentService` 改名为 `AgentDefinitionService`，把名字让给应用层 |
| 1 | 领域类型：messages、tool（JSON Schema + policy）、review、budget；内置工具迁到 adapters |
| 2 | ModelClient 端口、ModelSettings、Context；两家客户端迁到 adapters/models |
| 3 | ToolProvider + ToolSet；ToolCallReviewer + PolicyReviewer，主循环和子代理共用 |
| 4 | MemoryProvider + BuiltinMemoryProvider；runtime 不再直接引用 memory 域 |
| 5 | 角色与 Context 组装：Assistant、SubAgent、Teammate、Compactor，prompt/ 并入 |
| 6 | SessionStore + AgentService：拆分 AgentRuntime，三份 ReAct 循环合成一份 |
| 7 | 组装根：bootstrap 装配，API 层通过依赖注入拿 AgentService，清理跨域直连 Repository |
| 8 | 更新 CLAUDE.md、AGENTS.md、README |
