你正在维护 AgentOS。这是一个 Python 3.14 后端项目，主要技术栈为 FastAPI、
Pydantic v2、SQLAlchemy 2 Async、PostgreSQL、Alembic、Anthropic SDK、OpenAI SDK
和 pytest，依赖使用 uv 管理。当前仓库没有前端工程，不要虚构前端技术栈。

编码前先阅读相邻模块，复用已有分层和抽象，保持改动小而聚焦，不做无关重构，
不要覆盖用户已有改动，不要自动执行 git push。

架构与职责：
1. 保持 API -> Service -> Repository -> ORM Model 的分层依赖关系，Schema 与 ORM 分离。
   代码按两种方式组织：
   - 业务域（session、user、memory、skill 等）按 feature 包组织，每个域一个顶层包，
     内含 models.py、schemas.py、repository.py、service.py、api.py；分层是依赖约定，
     不要新建 models/、services/、repositories/、schemas/ 这类按层划分的顶层目录。
   - Agent 核心在 harness/ 内按层分包，依赖方向为 adapters → app → domain，外加组装根：
     - harness/domain（领域层）：系统语言，消息、Context、工具、审核结论等值类型与角色，
       纯逻辑、不做 I/O；新的值类型和纯规则放这里。
     - harness/app（应用层）：声明端口并编排流程（ReAct 循环、AgentService、ToolSet）；
       新的端口和不依赖具体实现的编排放这里。
     - harness/adapters（适配层）：端口的具体实现（模型 SDK、内置工具、审核规则链、内置记忆）；
       对接外部系统的新实现放这里。
     - harness/bootstrap.py（组装根）：唯一实例化适配器、把实现接到运行时上的地方。
     runtime/ 是 Agent 运行时的编排层（循环策略、子代理、Workflow、后台执行与租约），只认端口。
     谁能导入谁以 docs/agent-architecture.md 第 1 节为准，由 tests/test_import_isolation.py
     静态检查。
2. API 层保持轻薄：负责路由、Depends 注入、认证/权限入口、Schema 转换和响应包装；
   业务校验与跨模块编排放在 Service，SQL 查询和持久化放在 Repository。
3. Repository 持有 AsyncSession，写操作通常使用 add/flush/refresh，不得自行 commit。
4. 普通 HTTP 请求由 get_db 统一管理事务：成功 commit，异常 rollback。
5. 只有流式 Agent 或后台运行时为了增量持久化才能分步 commit，并应注释生命周期原因。
6. 所有数据库、LLM、事件流和工具 I/O 使用 async/await；异步流标注 AsyncIterator。
7. 新增 ORM 后必须登记到 database/registry.py，并提供显式 Alembic upgrade/downgrade 迁移。
   实体分散在各 feature 包，仅靠导入单个模型不会连带注册其余实体；
   registry 是唯一汇总入口，漏登记会导致 autogenerate 把缺失实体误判为删表，
   跨包字符串式 relationship 也会解析失败。database/engine.py 已副作用导入 registry，
   保证「能拿到 session 就一定已注册全部映射」。
8. feature 包之间只允许 Service 调 Service，不得跨域直接使用别的域的
   Repository 或自行拼写别域表的 SQL；本域 Service 才能访问本域 Repository。
9. 需要读写别域数据时，在数据所属域的 Service 上补公开方法，由该域负责
   参数校验、业务规则和事务内的副作用；调用方只做转发，不重复实现规则。
10. 判断归属看表的所有者：操作哪张表就归哪个域，不看谁先需要这个功能。
    例如任务认领由 team 域触发，但 tasks 表属 task 域，方法就应放在 task 域。
11. 跨域转发方法不要重复上游已做的校验（如会话存在性），避免同一次调用
    重复查询；校验放在数据所属域的 Service，转发层保持空壳。
12. 跨域调用产生的状态变更，其事件推送责任属数据所属域的 Service，
    调用方按需透传 bus，不自行 emit 别域的事件。
13. ORM 实体可以跨域导入（外键与 relationship 天然跨域），
    但仅限类型引用和关系声明，不得据此绕过 Service 直接改别域数据。
14. 确实需要跨域读模型对象时优先返回本域 Schema；
    返回别域 ORM 实体仅限内部编排，不要直接透出到 API 响应。

Agent 核心（harness）约定：
1. 新的外部能力先在 harness/app 定义端口，实现放 harness/adapters，只在 harness/bootstrap.py
   实例化；换实现用 `dataclasses.replace(default_harness(), 字段=...)`，不在调用方 new 适配器。
2. runtime/ 与 harness/app 的端口类依赖（reviewer、memory、sessions、models、tools 等）由组装根
   注入，构造参数必填，不得写「参数为 None 就现建默认实现」的回落。可选的 llm 只是测试替身的
   覆盖项：注入时直接用它，不注入时经已注入的 ModelResolver 解析，而不是现建客户端。
   测试经 tests/fixtures/harness_factory.py 走组装根的同一套工厂，不直接构造运行时。
3. 已有端口：ModelClient、ModelResolver、ToolProvider、ScopedToolProvider、ToolCallReviewer、
   SubAgentLauncher 在 harness/app/ports.py；MemoryProvider 在 harness/app/memory_port.py；
   SessionStore 及 TurnContextStore、TurnStateStore、TurnLeaseStore 等窄端口在
   harness/app/session_port.py；McpServerConnector、McpServerRegistry（及窄接口 McpServerSource）
   在 harness/app/mcp_port.py。
   端口只为多实现的外部能力和需要替身的 I/O 设立，其余 feature 域照旧 Service 调 Service。
4. 扩展点：接 MCP 时实现 ScopedToolProvider，经 Harness.scoped_tools 注入，platform_tool_set()
   只放进程级工具；接外部记忆时实现 MemoryProvider，在 harness/bootstrap.py 里替换 Harness 的
   memory 字段。MCP 以 docs/mcp-design.md 为准，外部记忆以 docs/agent-architecture.md 第 5 节为准。
5. 角色（harness/domain/roles）是纯函数，只产出 Context，不做 I/O，不读配置；整个 harness/domain
   都不得读取进程状态（cwd、当前日期时间、临时目录、环境变量、uuid、随机数），由运行时读好后传入。
6. 主 Agent、子代理、队友共用 harness/app/loop.py 的 run_react_loop，差异写在 runtime 的
   LeadLoopPolicy、SubAgentLoopPolicy、TeammateLoopPolicy 里，不要再复制一份循环。
7. 运行时对会话的读写只经 SessionStore 端口；runtime/ 里只有 runtime/execution.py 可以构造
   SessionService（lease_store(db)）。别域数据经该域 Service 的公开方法读写。
8. HTTP 路由经 api/deps.py 的 get_agent_service 拿 AgentService，不得自己构造运行时；
   Agent 工具经 ToolContext.subagents（SubAgentLauncher）启动子代理，不得导入 runtime。
9. 分层与导入禁令由 tests/test_import_isolation.py 静态检查，新增或移动模块后先跑它。
   最常踩的几条连函数体和 TYPE_CHECKING 内的导入也算：runtime/、harness/app、harness/domain
   导入 harness.adapters；runtime/、harness/app 导入任何 *.repository；runtime/ 导入 memory 包；
   在 harness/adapters/models 之外导入 anthropic / openai；在 harness/adapters/mcp 之外导入 mcp
   SDK（mcp、mcp_types）；runtime/、harness/app、harness/domain 导入 mcp_server 包；
   mcp_server 包导入 harness.adapters 或 mcp SDK。
10. 运行时与审核链按名查工具策略一律查本回合的策略全集（TurnConfiguration.policies：平台工具加
    本回合的作用域工具）；harness/app/tool_set.py 的 platform_tools() 只留给只有工具名、没有回合的
    管理场景（工具目录、绑定校验、权限规则配置），不要自建工具集。任何新的进程入口（脚本、Worker）
    只要用到 tools 或 permission，必须先导入 harness.bootstrap（导入即登记进程级工具集），
    否则取用时抛中文异常。
11. 七份黄金测试固化对外行为：发给模型的请求 tests/test_model_request_golden.py、工具描述
    tests/test_tool_spec_golden.py、审核结果 tests/test_tool_review_golden.py、记忆接入
    tests/test_memory_port_golden.py、角色上下文 tests/test_role_context_golden.py、循环事件与
    落库 tests/test_react_loop_golden.py、HTTP 入口 tests/test_agent_service_golden.py。
12. 改动黄金测试覆盖的行为时，先说明为什么要改，再用改动后代码的实际产出重新生成
    tests/fixtures/ 下对应的 *_golden.py（带 SHA-256 的一并更新），不允许手改字面量。
13. 已登记的已知依赖：memory/jobs/*、memory/recall/service.py、model_service/service.py
    直接使用模型适配器（create_model_client、resolve_scenario_model、DatabaseModelResolver、
    build_resolved_model）。memory job 由独立 Worker 运行，改成注入要动 Worker 的装配与生命周期，
    收益不抵风险（见 docs/agent-architecture.md 第 7 节）；不要以此为先例在别处直接引用适配器。

命名与类型：
1. 文件、函数和变量使用 snake_case；类使用 PascalCase；常量使用 UPPER_SNAKE_CASE。
2. 沿用语义后缀：*Record、*Repository、*Service、*Request、*Response、*Tool、*Input。
3. 公共方法、数据字段和返回值补齐类型注解，优先使用现代写法：
   T | None、list[T]、dict[str, Any]；动态 JSON 数据才使用 Any。
4. SQLAlchemy 使用 Mapped[T] + mapped_column；Pydantic 使用 model_validate、
   model_dump 和 Field，不使用 v1 API。
5. ORM 响应 Schema 设置 model_config = {"from_attributes": True}。
6. 内部协议和值对象优先使用类型化 dataclass；不可变对象使用 frozen=True，
   通过工厂方法和 to_dict() 控制合法状态及序列化。

格式与文档：
1. 遵循 4 空格缩进、100 字符行宽和 Python 3.14 语法。
2. 导入按“标准库、第三方库、项目内绝对导入”分组，组间留空行。
3. 长参数、调用和容器使用括号分行，并保留尾逗号。
4. docstring、业务错误、Field 描述和关键注释优先使用简体中文。
5. 注释解释契约、原因、事务、并发、兼容性和恢复语义，不逐行复述代码。
6. 编写 Markdown 文档时尽可能使用简体中文。

API、错误与日志：
1. 普通 JSON API 使用 ApiResponse[T] 和 ok(data, request)，维持
   {data, error, request_id} 响应外壳；SSE StreamingResponse 属于例外。
2. 业务错误统一使用 raise AgentException.message("中文消息", details)，
   不要新增裸 HTTPException 或临时错误响应结构。
3. 包装底层异常时使用 raise ... from exc 保留异常链。
4. 新代码使用标准 logging，并通过 extra 传递 request_id 等结构化上下文；
   不要复制历史代码中的 print 调试方式。

数据与工具：
1. ORM 表和列使用中文 comment；外键明确 ondelete；常用查询字段按需建立索引。
2. 所有 ORM 默认继承 Base 的审计字段和软删除机制，不要随意物理删除。
3. JSON 可变字段使用 MutableDict/MutableList 和 default=dict/list，
   禁止使用可变字面量作为默认值。
4. 内置工具继承 PydanticTool（harness/domain/tool.py），用 Pydantic BaseModel 定义输入，
   声明自身的 policy（ToolPolicy），通过 ToolContext 获取上下文，实现 async run，
   并在 harness/adapters/tools/builtin/__init__.py 导出、在 BuiltinToolProvider
   （harness/adapters/tools/builtin_provider.py）中登记，列表顺序即发给模型的工具顺序；
   组装根的 platform_tool_set() 经它装配进程级工具集。
5. 文件类工具使用 pathlib.Path、显式 UTF-8，并先校验路径和参数。
6. 发给模型的提示文案放 harness/domain/prompts/，由 harness/domain/roles/ 的角色
   （Assistant、SubAgent、Teammate、Compactor）组装成 Context；角色不做 I/O，不读配置、
   工作目录与日期，这些由运行时读好后传入。内置子代理规格在 harness/app/subagent_catalog.py，
   上下文压缩编排在 harness/app/compaction.py，压缩快照经会话端口 CompactSnapshotWriter 写入。

测试与交付：
1. 使用 pytest，测试放在 tests/test_*.py；覆盖成功路径、失败路径和关键副作用。
2. 局部 Service/事务测试可使用 Fake、monkeypatch 和调用次数断言；
   JSONB、锁、RLS、租户隔离等 PostgreSQL 特性必须使用真实 PostgreSQL 集成测试。
3. 修改后运行可用的相关测试和完整测试，如实报告命令、通过项、失败项及原因，
   不得在未执行时声称验证通过。
4. 不要机械复制历史遗留写法，包括 Optional[T]、裸 dict/list、datetime.utcnow()、
   print、MD5 密码哈希，以及未使用统一响应外壳的旧接口。
5. 仓库代码与当前回归测试优先于过时设计文档；遇到冲突时先说明证据再决定。

注释与日志：
1. 注释和 docstring 使用简体中文，尽量言简意赅，通常用一至两句话说明。
2. 注释重点解释代码意图、关键约束和必要原因，不逐行复述代码，不写冗长背景。
3. 日志内容统一使用简体中文，并包含定位问题所需的关键上下文。
4. 普通日志使用标准 logging；结构化字段通过 extra 传递。
5. 捕获异常时应记录异常类型、错误信息和相关业务上下文。
6. 错误日志优先使用 logger.exception("处理任务失败，任务 ID=%s", task_id)，
   自动保留完整异常堆栈；不在异常处理中使用 print。
7. 日志不得输出密码、Token、密钥、完整连接串或其他敏感信息。
8. 详细异常信息用于服务端日志，不应直接泄漏到 API 响应或模型输出中。

配置与本地环境：
1. 新增配置项时，必须沿用项目现有的配置方式和命名风格，并同时更新 `.env` 与
   `.env.example`，避免代码、实际环境和配置模板不一致。
2. `.env` 写入当前本地环境实际使用的配置值；`.env.example` 只写配置键、
   安全的示例值或空占位符，不得写入真实密码、Token、密钥和数据库凭证。
3. 环境变量继续使用项目已有的 `AGENTOS_` 前缀，并在 `core/config.py` 的
   `Settings` 中声明对应字段，不要引入另一套配置加载机制。
4. 开始开发或测试前先读取项目已有配置，直接使用 `.env` 中配置的数据库连接和
   Redis 连接，不要自行修改为其他地址。
5. 不得擅自在本地安装、启动或初始化 PostgreSQL、Redis、MinIO 等外部服务，
   也不要通过 Docker、Homebrew 或其他工具创建替代服务。
6. 如果 `.env` 中的外部服务不可连接，应先报告具体错误和相关配置项名称，
   不要擅自安装服务或覆盖现有配置。
7. 需要登录系统但不知道账号密码时，先阅读项目中的注册和登录接口及其 Schema，
   按实际 API 契约执行一次注册流程，然后尝试登录，不要绕过认证逻辑或直接修改数据库。
8. 系统默认登录账号和密码如下：
   - 账号：is_xianglei@163.com
   - 密码：is_xianglei@163.com
9. 使用默认账号前应优先尝试正常登录；如果账号不存在，再通过项目注册接口创建。
   如果注册提示账号已存在，则直接回到登录流程，不要重复插入用户数据。
10. 密码、Token、数据库连接串和 Redis 密钥属于敏感信息，不得写入日志、
    API 错误响应、测试快照或提交到版本库。
11. 执行注册、登录或连接外部服务后，应如实报告操作结果；错误日志可记录异常类型、
    堆栈和非敏感上下文。

配置同步规则：
1. `.env`、`.env.example` 与 `core/config.py` 中的 `Settings` 配置类必须始终保持同步。
2. 新增、删除、重命名配置项，或修改配置类型、默认值时，必须同时检查并更新以上三处。
3. `.env` 保存本地实际配置值；`.env.example` 保存相同配置键的安全示例值或空占位符；
   `Settings` 声明对应字段、类型和代码默认值。
4. 不得只修改其中一处，不得保留无代码读取的废弃环境变量，也不得让代码引用未声明的配置。
5. 环境变量继续使用 `AGENTOS_` 前缀，并确保名称能正确映射到 `Settings` 中的字段。
6. 修改配置后，应验证 `Settings` 能成功加载，并检查必填项、类型转换和默认值是否符合预期。
7. `.env.example` 不得包含 `.env` 中的真实密码、Token、密钥、数据库连接串或 Redis 凭证。

多 Provider LLM 配置：
1. AgentOS 同时支持 Anthropic 和 OpenAI 两个 LLM provider，由 `llm_provider`
   （环境变量 `AGENTOS_LLM_PROVIDER`，取值 anthropic / openai）全局切换；
   该开关必填且无默认值，配置固定、重启生效，不支持运行时切换或按场景混用 provider。
2. 每个 provider 一组连接配置：`{provider}_base_url`、`{provider}_api_key`、
   `{provider}_model`（主模型，所有场景的回落终点）、`{provider}_max_retries`
   和 `{provider}_timeout_seconds`；重试与超时直接使用 SDK 内置机制，不要自造重试框架。
3. 场景覆盖配置为 `{provider}_{scenario}_model`，scenario 共八个：agent、subagent、
   subagent_fast、compact、memory_selector、memory_extractor、memory_dream、workflow；
   场景覆盖未配置时回落到 `{provider}_model` 主模型。新增或调整这些配置项时，
   同样遵守上文「配置同步规则」。
4. token 上限读取 `llm_default_max_tokens`（流式调用）和
   `llm_memory_selector_max_tokens` 等场景专用配置，不要在代码中硬编码 max_tokens。
5. 运行时和应用层经组装根注入的 ModelResolver 端口（harness/app/ports.py）拿到
   ResolvedModel(client, settings)，不得自行导入 harness/adapters/models 下的任何实现。
   其实现是 harness/adapters/models/resolver.py 的 DatabaseModelResolver：工作区模型配置优先
   （builder.py 按 ModelProviderRecord 构造端点），查不到再由 factory.py 的
   create_model_client(scenario=...) 按 .env 建客户端。类型注解使用 ModelClient，不要直接实例化
   AnthropicClient / OpenAIClient。测试替身经组装根工厂方法的 llm 覆盖项注入，注入后不走解析器，
   调用参数用 ResolvedModel.from_client(llm) 取替身自带的模型名。只需模型名而不建客户端时使用
   factory.py 的 resolve_scenario_model(scenario)；子代理规格的模型用 resolve_subagent_model(spec)，
   运行时经组装根的 subagent_model 拿到它，不直接导入。
6. 无静默兜底：provider 未配置或取值不支持、对应 api_key 缺失、场景名未知、
   模型解析不出，一律抛具体中文 AgentException，不得回落到默认 provider 或硬编码模型。
7. 需要判断当前使用哪家 provider 时，读客户端实例的 provider 属性
   （ModelClient 的抽象属性），不要用 isinstance 判断具体客户端类，
   也不要直接读 settings.llm_provider，注入测试替身时这两种方式都会失真。
8. 两家 SDK 对 base_url 的拼接规则相反：Anthropic 的 base_url 不带 /v1，
   SDK 内部路径已是 /v1/messages，多写会拼成 /v1/v1/messages 报 404；
   OpenAI 的 base_url 必须带 /v1，SDK 内部路径是 /chat/completions，
   少写会让网关返回 HTML，SDK 报出 'str' object has no attribute 'choices'
   这类看似无关的错误。
9. `{provider}_timeout_seconds` 留空表示交给 SDK 自动计算超时；
   给 SDK 显式传 None 会被理解为禁用超时，现有代码统一转成 NOT_GIVEN 规避，
   新代码沿用该写法；ModelSettings.timeout 为 None 时同样沿用客户端构造时的超时。
10. 两家客户端均已实现 stream() 和 complete_structured()，主 Agent、SubAgent、Compact
    三个流式场景两家 provider 都可用。OpenAI 侧流式走 create(stream=True) 裸 chunk 路径，
    不用 SDK 的 .stream() helper：后者会强制校验工具的 strict 字段，在发出请求前就抛
    ValueError，且 get_final_completion() 在截断时抛异常而非返回已生成内容。
11. LLM 消息体使用中立形状，provider wire 形状只存在于各家客户端 _encode_messages()
    之后。ModelClient 的签名是 stream(context, settings) 与 complete_structured(context,
    settings)：context 为 harness/domain/context.py 的 Context（instruction、messages、
    tools、output_type），messages 是领域消息元组，由调用边界用 messages_from_rows() 从
    中立 dict 还原；session_messages.content 和 session_snapshots.messages 一律是中立形状，
    不得写入或读取任何一家的 wire 形状。
12. assistant 消息 content 为块数组，判别键用 kind 而非 type：
    {"kind": "text", "text": ...} 和 {"kind": "tool_use", "id": ..., "name": ..., "input": ...}，
    经 harness/domain/messages.py 的 AssistantMessage.from_turn() 构造、
    AssistantMessage.from_content() 解析。
    不要改用 type——两家 wire 的 type 取值恰好都叫 tool_use、入参字段也都叫 input，
    用 type 会让残留的 provider 形状数据被静默误读并潜伏到运行时。
13. tool 消息沿用 ToolResult，本身即自有协议，形状不变。
14. 出站编码是 stream() 的内部步骤，_encode_messages / _encode_system / _encode_tools
    是各家客户端的私有方法，不要提升到 ModelClient 抽象成员（维持 provider、stream、
    complete_structured 三个）；也不要在调用方翻译消息形状。
15. 模型名、思考强度和输出上限一律通过 ModelSettings(model_id, effort, max_tokens, timeout)
    按调用传入，不得在客户端实例上修改（不得写 client.max_tokens、client.thinking_enabled
    之类的属性）。客户端构造参数只放端点配置；effort 为 None 表示端点默认（.env 端点沿用
    {provider}_thinking_*，工作区端点按 fast 处理），档位到各家参数的翻译在客户端内现场完成，
    档位收敛（normalize_effort）由 builder 在构造工作区端点的档位映射表时完成。

Anthropic SDK 使用规范：
1. 新增或修改 Anthropic SDK 相关代码前，必须先查阅 Anthropic 官方文档：
   https://platform.claude.com/docs/
2. 优先搜索官方最佳实践、API Reference、教程和 Demo 示例，确认当前推荐的调用方式后再实现。
3. 使用示例代码前，应核对项目实际安装的 Anthropic SDK 版本，确保类名、参数、
   返回结构、流式事件和异常类型与当前版本兼容。
4. 官方文档和项目现有实现不一致时，不要直接照搬示例；应结合项目依赖版本、
   异步架构、工具系统、事件协议和事务边界进行适配。
5. 优先使用官方提供的异步客户端、流式接口、类型定义和工具调用格式，
   不要自行猜测 SDK 接口或手工模拟已有能力。
6. 涉及流式响应、Tool Use、Prompt Caching、Token Usage、错误重试等能力时，
   必须分别确认官方推荐用法和限制。
7. 仅将 Anthropic 官方文档作为 SDK 用法的首要依据；第三方博客和旧示例只能辅助参考。
8. 实现完成后，应针对正常响应、流式事件、工具调用、超时和 SDK 异常补充验证，
   并如实说明参考的官方文档页面及验证结果。
9. 不得在代码、日志、测试、文档或提交记录中暴露 Anthropic API Key。

功能删除与代码清理：
1. 删除某项功能时，必须完整删除与其关联的实现，不得遗留死代码、空类、空方法、
   占位文件、无效分支、pass、TODO、临时兼容层或不可达逻辑。
2. 删除前应搜索该功能的全部引用，确认其 API、Schema、Service、Repository、ORM、
   工具、事件、Hook、Prompt、注册表、依赖注入和跨域调用关系。
3. 同步清理相关路由注册、模块导出、配置项、`.env`、`.env.example`、Settings 字段、
   常量、依赖包、测试夹具、Mock、示例代码和文档。
4. 删除工具时，同步清理 PydanticTool 实现（含其 policy）、Input Schema、builtin 导出、
   BuiltinToolProvider 登记、权限配置及相关 Prompt 描述。
5. 删除 ORM 实体时，同步清理 relationship、外键引用和 database/registry.py 登记，
   并新增 Alembic 迁移删除对应数据库对象。
6. 已经进入迁移历史的 Alembic 文件不得直接删除或篡改；数据库结构变更应通过新的
   upgrade/downgrade 迁移完成。
7. 如果删除功能导致某个模块、依赖、配置或抽象不再被使用，应一并移除，
   不要为了“以后可能使用”保留无实际调用的骨架。
8. 除非用户明确要求保留兼容性，否则不要留下 deprecated 接口、旧参数转发、
   空响应或仅抛出“功能已删除”的占位实现。
9. 清理完成后使用代码搜索确认旧名称、路由、配置键和注册项已无有效引用，
   并运行相关测试及完整测试。
10. 最终说明删除了哪些入口、实现、配置、数据结构、测试和文档；
    对因迁移历史或外部兼容契约必须保留的内容，应明确说明原因。