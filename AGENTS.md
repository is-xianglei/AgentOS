你正在维护 AgentOS。这是一个 Python 3.14 后端项目，主要技术栈为 FastAPI、
Pydantic v2、SQLAlchemy 2 Async、PostgreSQL、Alembic、Anthropic SDK 和 pytest，
依赖使用 uv 管理。当前仓库没有前端工程，不要虚构前端技术栈。

编码前先阅读相邻模块，复用已有分层和抽象，保持改动小而聚焦，不做无关重构，
不要覆盖用户已有改动，不要自动执行 git push。

架构与职责：
1. 保持 API -> Service -> Repository -> ORM Model 的分层结构，Schema 与 ORM 分离。
2. API 层保持轻薄：负责路由、Depends 注入、认证/权限入口、Schema 转换和响应包装；
   业务校验与跨模块编排放在 Service，SQL 查询和持久化放在 Repository。
3. Repository 持有 AsyncSession，写操作通常使用 add/flush/refresh，不得自行 commit。
4. 普通 HTTP 请求由 get_db 统一管理事务：成功 commit，异常 rollback。
5. 只有流式 Agent 或后台运行时为了增量持久化才能分步 commit，并应注释生命周期原因。
6. 所有数据库、LLM、事件流和工具 I/O 使用 async/await；异步流标注 AsyncIterator。
7. 新增 ORM 后同步更新 models/__init__.py，并提供显式 Alembic upgrade/downgrade 迁移。

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
4. 工具继承 BaseTool，用 Pydantic BaseModel 定义输入，通过 ToolContext 获取上下文，
   实现 async run，并在 tools/builtin/__init__.py 和 ToolRegistry 中登记。
5. 文件类工具使用 pathlib.Path、显式 UTF-8，并先校验路径和参数。

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
   - 账号：is.xianglei@gmail.com
   - 密码：is.xianglei@gmail.com
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