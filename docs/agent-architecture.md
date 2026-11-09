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
| context.py | 已落地（阶段 2b）。`Context[O](instruction: str \| tuple[PromptBlock, ...] \| None, messages: tuple[Message, ...], tools: tuple[ToolSpec, ...] = (), output_type: type[O] \| None = None)`，以及 `PromptBlock`、`ToolSpec` 两个值类型；`messages_from_rows(rows)` 在调用边界把中立 dict 还原为领域消息。记忆和项目规范不单列字段，由角色渲染进 instruction 或 messages，模型客户端不需要懂记忆的语义 |
| tool.py | 已落地（阶段 3a）。`Tool(name, description, parameters, policy)`，抽象方法 `normalize_arguments`、`execute(arguments, ctx)`；`PydanticTool` 是内置工具基类，parameters 由 input_model 派生；`ToolPolicy`、`ToolProfile`、`ShellAccess`、`ToolContext`（db、bus 的类型只在 TYPE_CHECKING 下导入）；`tool_policy()`、`unknown_tool_policy()`、`requires_skill_allowlist()` |
| review.py | 已落地（阶段 3b）。`Decision(behavior, arguments, source, message, reason, interaction, needs_approval)`，只经工厂方法 `Decision.allow(arguments, *, source)`、`Decision.deny(message, *, arguments, source, reason, needs_approval)`、`Decision.ask(interaction, *, arguments, reason, source)` 构造，`__post_init__` 拒绝非法组合；arguments 是审核后（可能已规范化）的入参；message 回给模型，reason 供日志和被拒摘要；needs_approval 标出「需审批但无审批通道而合并成的拒绝」。`ReviewRequest(session_id, profile, agent_type, budget, workspace_id, allowed_skill_names, plan_content, tools)`，plan_content 为 None 表示不在 Plan 模式，tools 只需满足 `ToolInputValidator`（validate_input）。纯函数 `check_capability(tool_name, policy, profile, arguments, allowed_skill_names) -> str \| None` 由审核规则和 ToolService 执行层兜底共用。另有 `PLAN_MODE_ALLOWED_TOOLS`、`INTERACTION_TOOL_KINDS`、`SOURCE_*` 规则来源名 |
| roles/ | 已落地（阶段 5），一个角色一个模块，全部是纯函数，不做 I/O、不读配置、不读进程状态。`Assistant(cwd, today, skills_catalog, delegate_catalog, session_prompt, memory, plan_content, turbo)` 提供 `instruction()`、`context(messages, tools)`、`final_context(messages)`（收尾，不带工具）；`SubAgent(spec, memory_prefix)`、`Teammate(spec, session_id, name, role, memory_prefix)`；`Compactor` 提供 L1 `micro_compact` 和 L2 压缩提示词的 `context`；`SubAgentSpec`、`AgentType` 在 roles/subagent.py；子代理输出协议解析在 roles/reports.py，验收触发判定在 roles/verification_trigger.py。记忆正文 message_prefix 仍由会话加载按消息 ID 拼接（它要和 additional_context 拼在同一条消息两端），角色只用 memory.instruction |
| budget.py | 已落地（阶段 3a）。`ToolCallBudget`：循环额度，可序列化进挂起的续跑状态 |
| memory.py | 已落地（阶段 4）。`TurnRef(workspace_id, user_id, session_id, turn_id)` 是召回与写入需要的回合身份；`MemoryRecall(instruction, message_prefix)`：instruction 进系统指令末段，message_prefix 拼到本轮起始用户消息前；`MemoryWriteTicket(turn, ref)` 是写入回执 |
| prompts/ | 已落地（阶段 5）。lead、plan_mode、memory、subagents、teammate、compaction 等提示文案，逐字保持现状 |

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
| ModelClient | `stream(context: Context[None], settings) -> AsyncIterator[ModelStreamEvent]`、`complete_structured[O](context: Context[O], settings) -> O` | Anthropic、OpenAI | Google |
| ModelSettings | `ModelSettings(model_id, effort: Effort \| None, max_tokens, timeout)`，每次调用时传入；effort 为 None 表示端点默认（.env 端点沿用 {provider}_thinking_*，工作区端点按 fast）；max_tokens 为 None 时流式取 llm_default_max_tokens、结构化取 256 | — | — |
| ModelResolver | `resolve(workspace_id, user_id, scenario, *, provider_id, effort)`、`for_session(session, scenario)`、`for_frozen_job(..., model, provider_id)`，返回 `ResolvedModel(client, settings, provider_record_id, context_length)`：先查工作区模型配置，查不到再用 .env | DatabaseModelResolver（数据库 + .env） | — |
| SessionStore | 按实际操作设计：加载本轮上下文、追加消息、写压缩快照；不做整存整取 | 数据库（包装 SessionService） | 测试用内存实现 |
| ToolProvider | `list_tools() -> list[Tool]`，代表一个工具来源；目前同步、不带 scope，接入 MCP 时扩展为 `async list_tools(scope)` | BuiltinToolProvider | MCP |
| MemoryProvider | 在 `app/memory_port.py`（与模型端口无关，单独成模块）。`recall(turn)`、`inherited(turn)`、`on_turn_completed(turn) -> MemoryWriteTicket | None`、`after_commit(ticket)`；调用方持有会话并负责提交，recall 和 on_turn_completed 在调用方事务里执行、从不提交，after_commit 在提交后自管事务 | BuiltinMemoryProvider | 外部记忆框架 |
| ToolCallReviewer | `async review(call: ToolCall, request: ReviewRequest) -> Decision`；可扣减 request.budget | PolicyReviewer（规则链，阶段 3b 落地） | LLM 审核员（独立实现，或作为规则链里的一条规则） |

编排：
- `AgentService` 对外提供 new_session / list_sessions / chat / resume / cancel。主 Agent、子代理、队友
- `app/loop.py`（已落地，阶段 6a）是唯一的 ReAct 循环骨架：`run_react_loop(policy, step, *, max_rounds)`。每轮依次执行 `before_round` → `load_context` → 调模型 → `record_assistant`，之后有工具调用就走 `execute_tools` + `flush`，没有就走 `on_no_tool_calls`；钩子返回 `LoopExit` 时立即结束（结束前也会先 `flush`），跑满轮次后走 `on_exhausted`。三种循环策略都放在 runtime：`LeadLoopPolicy`（runtime/agent.py）、`SubAgentLoopPolicy` / `TeammateLoopPolicy`（runtime/subagent.py），因为它们要用到 session、team 等 feature 域的 Service。
  共用唯一一份 ReAct 循环（loop.py）。
- `ToolSet`（已落地，阶段 3a）聚合多个 ToolProvider，负责重名校验、按模式和档位裁剪，并保证工具顺序稳定（提示缓存依赖这一点）。提供 to_specs、get、validate_input、only、without、with_tools，以及按名查策略的 policy_for、callable_names、bindable_names、denied_names（吸收了原 ToolCapabilityResolver）。
  只有工具名、拿不到 ToolSet 的调用方（PermissionService、PolicyReviewer 的能力规则、ToolCatalogService、ToolService 的能力检查等）暂时调用 `builtin_tool_set()` 查策略；接入 MCP 前由组装根注入完整 ToolSet（阶段 7）。
- 后台执行与租约、SSE 跟随、孤儿回收、取消还原、Workflow 编排、Hook 注册表也都放在应用层。

## 4. 适配层（harness/adapters/）

| 目录 | 内容 |
|---|---|
| models/ | AnthropicClient、OpenAIClient：构造参数只放端点配置（地址、密钥、重试、超时、思考协议、档位映射表），effort 按调用翻译成各家参数；factory 解析 .env；builder 按工作区模型配置建客户端；resolver 实现 ModelResolver |
| stores/ | DatabaseSessionStore |
| tools/ | builtin/（全部内置工具，Bash 的 shell 校验也在这里）、builtin_provider.py |
| memory/ | 已落地（阶段 4）。BuiltinMemoryProvider 包装 memory 域的召回和提取，提取开关、SAVEPOINT、就地执行开关和日志保留在这里；AgentRuntime 与 SubAgentRunner 的构造参数 `memory` 未注入时回落到它，后台重建 runtime 时沿用注入值 |
| reviewers/ | 已落地（阶段 3b）。`PolicyReviewer(rules)` 按序执行一组规则（每条规则本身也是 ToolCallReviewer），拒绝或挂起即终局，放行可改写入参交给下一条。`for_main_agent(permission_service)`：BudgetRule → PlanModeToolsRule → ArgumentsRule → CapabilityRule → PlanModeAgentRule → InteractionToolRule → PermissionRule。`for_subagent(permission_service)`：BudgetRule → NoApprovalChannelRule(CapabilityRule, PermissionRule)，后者把挂起合并为拒绝并换成子代理文案。AgentRuntime、SubAgentRunner 构造参数 `reviewer` 未注入时用对应组装入口 |

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
- 同一回合重复召回结果相同：子代理经 `inherited` 拿到父回合冻结下来的结果，不再直接读 memory 的 Repository；子代理只继承 message_prefix，不继承目录。
- 极速模式不召回、不写入，这是运行模式的取舍，由调用方判断，Provider 不感知。
- 回合结束写入失败不得影响回合终态。内置实现「入队与回合终态同一事务、用 SAVEPOINT 隔离」的语义保留在内置适配器内部。
- 内置 memory 域的管理接口（条目增删改查、dream、保留策略）属于内置实现自己的功能，不进入端口。
- 用哪个 Provider 由组装根决定；接入第二种实现时再新增配置项，并遵守配置同步规则。

## 6. 现有代码去向

| 现位置 | 去向 |
|---|---|
| llm/types.py | domain/messages.py；流式事件类型放 app/ports.py |
| llm/base.py、llm/errors.py | app/ports.py（ModelClient 及其错误类型） |
| llm/anthropic_client.py、openai_client.py | adapters/models/ |
| llm/factory.py、runtime/model_selection.py | adapters/models/factory.py（.env）、builder.py（工作区端点）、resolver.py（ModelResolver），由组装根调用 |
| tools/base.py、tools/policy.py（ToolPolicy、档位常量、requires_skill_allowlist）、ShellAccess | domain/tool.py（已完成） |
| tools/builtin/、tools/shell_policy.py 的命令校验 | adapters/tools/builtin/（已完成） |
| tools/subagents/ | 已完成：规格类型放 domain/roles/subagent.py，四种内置规格（读配置）放 app/subagent_catalog.py，提示文案放 domain/prompts/subagents.py，输出协议放 domain/roles/reports.py，trigger 放 domain/roles/verification_trigger.py，按场景解析模型名放 adapters/models/factory.py 的 resolve_subagent_model |
| tools/registry.py、tools/policy.py 的 ToolCapabilityResolver | app/tool_set.py + adapters/tools/builtin_provider.py（已完成） |
| tools/policy_engine.py、runtime 里两份逐条审核 | adapters/reviewers/policy_reviewer.py（已完成，PolicyEngine 已删除；静态能力检查收进 domain/review.py 的 check_capability） |
| tools/gateway.py | 待定：网关冻结执行身份并在执行层兜底，随 ToolService 迁往 app/tool_executor.py 时一并处理 |
| tools/service.py 里的 ToolService | app/tool_executor.py（执行与钩子）；调用记录仍经 tools 域写入 |
| tools/ 的 ToolCatalogService、models、repository、api | 留在 tools/，工具目录和调用记录属于平台数据 |
| prompt/、plan/prompts.py、TURBO_PROMPT | 已完成：domain/prompts/，由角色负责组装 |
| hooks/ | app/hooks.py |
| runtime/agent.py、subagent.py、step.py、stream_assembler.py | app/agent_service.py、app/loop.py（循环骨架与 StepResult 已迁入，阶段 6a） |
| runtime/compact.py | 已完成：domain/roles/compactor.py 的 Compactor，加上 app/compaction.py 的 ContextCompaction 编排；快照改走新增的 SessionService.save_compact_snapshot，runtime 不再持有 SessionRepository |
| runtime/budget.py | domain/budget.py（已完成） |
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
- 发往两家 SDK 的请求参数由 tests/test_model_request_golden.py 固化（字面量取自阶段 2b 之前的代码），
  后续阶段不得改动 tests/fixtures/model_request_golden.py。
- 记忆接入主循环的行为（召回结果在系统提示和用户消息里的位置、提交与 SAVEPOINT 的调用顺序、入队参数、日志、子代理继承）由 tests/test_memory_port_golden.py 固化（27 种情况，字面量取自阶段 4 之前的代码），后续阶段不得改动 tests/fixtures/memory_port_golden.py。runtime/ 不得导入 memory 包，由 tests/test_import_isolation.py 静态检查。
- 各角色产出的系统指令与消息（主 Agent 组合矩阵、极速、Plan、收尾不带工具、子代理与队友、压缩）、子代理规格的全部字段与派生结果、输出协议解析与路由目录，由 tests/test_role_context_golden.py 固化（80 种情况，字面量取自阶段 5 之前的代码），后续阶段不得改动 tests/fixtures/role_context_golden.py。harness/domain 不得读取进程状态（cwd、当前日期时间、临时目录、环境变量、uuid、随机数），由 tests/test_import_isolation.py 静态检查。
- 主循环、子代理和队友循环发出的 SSE 事件序列、落库与状态变化序列、commit / flush 顺序、模型请求、日志和返回值，由 tests/test_react_loop_golden.py 固化（27 种情况，字面量取自阶段 6a 之前的代码），后续阶段不得改动 tests/fixtures/react_loop_golden.py。runtime/ 和 harness/app/ 不得导入任何 `*.repository` 模块（函数体和 TYPE_CHECKING 里的导入也算），由 tests/test_import_isolation.py 静态检查。
- 发给模型的工具描述（26 个内置工具的名称、描述、JSON Schema、顺序）和每个工具的 ToolPolicy 由 tests/test_tool_spec_golden.py 固化（字面量取自阶段 3a 之前的代码），后续阶段不得改动 tests/fixtures/tool_spec_golden.py。
- 主 Agent 与子代理逐个工具调用的审核结果（tool_result 文本与 is_error、done_ids、挂起与续跑状态、网关收到的入参、权限规则查询参数、被拒摘要、日志的级别/正文/extra、预算计数）由 tests/test_tool_review_golden.py 固化（49 种情况，字面量取自阶段 3b 之前的代码），后续阶段不得改动 tests/fixtures/tool_review_golden.py。

已知、有意保留的行为差异和不对称（后续单独处理，不在重构中顺手改）：
- 未知的 effort 字符串在 `Effort.parse` 处抛中文异常；之前在工作区路径会回落到默认档位，在 .env 路径会原样下发。
  Schema 只接受六个合法值，开发库里也没有非法值（2026-11-08 核查），属于禁止静默兜底的收紧。
- `ModelSettings.timeout` 在各条调用路径上的作用范围不一致：Anthropic 流式调用忽略它、只用端点超时；
  Anthropic 结构化调用作用于单次请求；OpenAI 结构化调用作用于客户端。为了保持请求逐字节一致，暂时原样保留。
- 阶段 3b 起，Plan 状态由调用方在审核前读出并放进 ReviewRequest，所以预算已耗尽的调用也会多查一次 Plan；之前是预算扣减成功后才读。写出的结果不变，黄金测试已覆盖。
- 交互工具（AskUserQuestion、EnterPlanMode、ExitPlanMode）不经过权限规则；人工审批通过后的恢复路径不会重新审核，只重新校验用户改过的入参，执行层的能力兜底仍然生效。两条都是现状，已在黄金测试中固化。
- Decision.source 取 `SOURCE_*` 常量，运行时据此决定打哪条日志；以后接入 LLM 审核员时要用新的来源名，不要复用这些常量。
- 阶段 4 起，就地执行记忆提取的 claim_scope 取回合自身的租户，而不是运行时实例上的 workspace_id / user_id；两者在所有路径上相等，黄金测试已固化。入队与就地执行的日志 logger 名从 runtime.agent 变为 harness.adapters.memory.builtin_provider，正文与 extra 不变。
- 阶段 5 起，L1 微压缩返回新列表而不是原地改写入参（主循环入参本就是副本，观测结果一致）；压缩日志的 logger 名从 runtime.compact 变为 harness.app.compaction，正文与 extra 不变。队友的系统提示不追加子代理规格的 critical_reminder，这是现状，已在黄金测试中固化。

## 8. 阶段

| 阶段 | 内容 |
|---|---|
| 0 | 删除编排检查点死代码；agent 域的 `AgentService` 改名为 `AgentDefinitionService`，把名字让给应用层 |
| 1 | 已完成：领域消息类型 harness/domain/messages.py |
| 2a | 已完成：模型端口迁到 app/ports.py，两家客户端迁到 adapters/models |
| 2b | 已完成：ModelSettings、Effort、Context 按调用传入；客户端去掉可变的思考属性；模型解析迁到 adapters/models/resolver.py，工作区端点构造迁到 builder.py，档位映射表在 reasoning.py |
| 3a | 已完成：工具领域类型（Tool 以 JSON Schema 为准、自带 ToolPolicy；ToolContext；ToolCallBudget）；内置工具迁到 adapters/tools/builtin；ToolProvider + ToolSet 取代 ToolRegistry |
| 3b | 已完成：ToolCallReviewer + Decision；PolicyReviewer 合并原 7 层审核，主循环和子代理共用；审核结果由 tests/test_tool_review_golden.py 固化 |
| 4 | 已完成：MemoryProvider + BuiltinMemoryProvider；runtime 不再直接引用 memory 域 |
| 5 | 已完成：角色与 Context 组装：Assistant、SubAgent、Teammate、Compactor，prompt/、tools/subagents/、runtime/compact.py 并入 harness |
| 6a | 已完成：主 Agent、子代理、队友共用 app/loop.py；runtime 不再直接使用别域 Repository，改走新增的 Service 公开方法：SessionService.get / get_turn / set_status，ToolService.get_call / complete_call / mark_call_awaiting / reject_awaiting_call，TeamService.create_subagent_run / succeed_subagent_run / fail_subagent_run |
| 6b | SessionStore 端口 + AgentService：拆分 AgentRuntime（准入、执行生命周期、人工交互、模型与工具配置），对外提供 new_session / list_sessions / chat / resume / cancel |
| 7 | 组装根：bootstrap 装配，API 层通过依赖注入拿 AgentService，清理跨域直连 Repository |
| 8 | 更新 CLAUDE.md、AGENTS.md、README |
