# AgentOS MCP 接入设计

> 状态：设计稿（2026-11-11），按第 13 节分阶段实现中。拍板过的决策记在第 14 节，并已写入正文。实现阶段细化出的签名回写到本文。
> 上位文档：[agent-architecture.md](agent-architecture.md)（分层规则、端口约定、组装根）。本文只写 MCP 的增量。
> 本文推翻了上位文档和 CLAUDE.md 里关于 MCP 的几条预设，具体列在第 15 节；M0 开始时同步修改那几处，
> 修改之前以本文为准。其余分层规则仍以上位文档为准。
> 调研依据：HugAgentOS 源码、8 个 Agent 框架、5 个多租户平台、MCP 规范 2026-07-28，出处见附录 A。

## 术语

| 术语 | 含义 |
|---|---|
| MCP server | 按 MCP 协议对外提供工具的远程服务。例如 GitHub 的 MCP server 提供「建 issue」「查 PR」等工具。AgentOS 是连接它的客户端 |
| server | 本文里指工作区中配置好的一个 MCP server，即 `mcp_servers` 表的一行：地址、认证方式、审批模式、工具过滤 |
| server_key | 添加 server 时起的短标识，如 `github`、`jira`，工作区内唯一。它是工具名的中段：GitHub server 的 `create_issue` 工具，发给模型时叫 `mcp__github__create_issue`。创建后不能改，因为权限规则和审批记录都按工具名保存，改了就对不上 |
| 共享 server / 私有 server | 共享 server 由管理员配置，工作区成员都能用；私有 server 由成员自己配置，只有本人能看到和使用（7.2 节） |
| 作用域工具 | 随工作区和用户变化的工具，目前就是 MCP 工具。内置工具是进程级的，对所有人都一样 |
| 授权分区 | 凭证归谁：共享 server 的静态请求头归工作区（`ws:{id}`），OAuth token 和私有 server 的凭证归用户（`user:{id}`）。连接池和缓存都按它隔离 |

## 0. 结论速览

- AgentOS 作为 **MCP 客户端**，按工作区接入远程 MCP server。远程工具包装成领域层的 `Tool`，
  与内置工具走同一条审核、执行、落库、SSE 链路。本期不把 AgentOS 暴露成 MCP server。
- server 分两种：管理员配置、全工作区共用的共享 server，以及成员自己配置、只有本人可见的私有 server。
- 沿用 harness 的六边形分层（端口与适配器）：
  - 领域层新增 `harness/domain/mcp.py`：命名算法、注解到审核策略的映射、server 配置值类型，全是纯函数。
  - 应用层新增出站端口 `ScopedToolProvider`（按身份异步列工具）、`McpServerConnector`（探测与授权）和 `McpServerRegistry`（配置读写），
    新增编排 `TurnToolResolver`（每回合解析工具集）和入站服务 `McpAdminService`（管理入口）。
    应用层不认识 MCP SDK。
  - 适配层新增 `harness/adapters/mcp/`，这是全仓唯一导入 `mcp` SDK 的地方，包含：
    连接池、结果转换、带 SSRF 防护的出站 HTTP，以及 OAuth（token 读取、持锁刷新、两段式授权）。
  - 业务域新增 feature 包 `mcp_server/`：server 配置、凭证、OAuth token 的持久化与管理接口。
    **包名不能叫 `mcp`**，否则会遮蔽 SDK。
  - 组装根持有进程级的 MCP 运行时单例（惰性创建，在 lifespan 里关闭）；`Harness` 新增作用域工具来源字段。
- 工具集从「进程级、静态」变为「平台工具（静态）+ 作用域工具（每回合异步解析）」。
  作用域工具在后台执行体里每回合解析一次，并冻结到回合配置。
- 只支持 Streamable HTTP，不对用户开放 stdio。
- 工具名为 `mcp__{server_key}__{tool}`：不超过 64 字符，只含 `[A-Za-z0-9_-]`，由 (server_key, 原名) 唯一决定，
  不受同一 server 上其他工具的影响。清洗有损或超长时，加上由原名算出的哈希后缀。
- 审批：server 级三档模式，默认全部需要审批。只有管理员显式选择「信任注解」时，注解才参与判定；
  工作区权限规则照旧可以覆盖。
- 必须先做前置重构 M0，分三件事：
  - 按名查策略的来源，从「进程级工具集」改成「本回合的策略全集」，即平台工具加上本回合解析出的全部作用域工具，**不按档位裁剪**；
  - 作用域工具对象能一路传到子代理、队友和委派；
  - `Tool.execute` 和 `ToolService.run` 改为返回 `ToolOutput`，用来承载 `is_error`。

  M0 不改变任何对外行为，七份黄金测试的期望字面量保持不变。需要改动的测试驱动段在 12.3 节逐条列出，并写明理由。

## 1. 目标与非目标

目标：
1. 工作区 owner/admin 配置共享 server（地址、认证、审批模式、工具过滤），工作区成员在对话中使用；
   成员也可以配置只有自己可见的私有 server（7.2 节）。
2. 默认 Agent 和自建 Agent 都能调用 MCP 工具。自建 Agent 按 server 绑定，可选再配工具白名单。
3. MCP 工具经现有 `ToolCallReviewer` 审核；需要审批时，走现有的 ask 挂起与恢复。
4. 支持两种认证：静态请求头（工作区共享）和 OAuth 2.1（按用户授权）。
5. 任何一个 server 不可用都不影响回合，本回合只是缺少它的工具。
6. 多实例部署不需要会话亲和。凭证明文存储（7.1 节），不进日志，接口不回显。

非目标（本期不做，部分列入后续阶段）：
- 把 AgentOS 暴露为 MCP server；MCP 市场、server 版本管理与审核提交。
- stdio 传输和用户自定义命令；旧的 HTTP+SSE 传输（规范已标为 Deprecated）。
- sampling、roots、logging（规范 2026-07-28 已弃用这三项）。
- resources、prompts 能力；把工具结果里的图片发给模型。
- 渐进式工具发现（工具很多时先只给模型一个检索工具，按需再加载工具定义）。本期工具数量靠 server 绑定和
  `tool_allowlist` / `tool_denylist` 控制，出处见附录 A。

## 2. 六边形视图

MCP 接入不新开架构，只是在 harness 这个六边形上新增一个驱动端口（`McpAdminService`）和三个被驱动端口
（`ScopedToolProvider`、`McpServerConnector`、`McpServerRegistry`）。
里圈是领域层，中圈是应用层，外圈是适配器。依赖一律指向里圈，SDK 只出现在最外圈。

```text
        ┌───────────────── 驱动侧适配器（入站） ─────────────────┐
        │ session/api.py      发消息、答复审批                    │
        │ mcp_server/api.py   server 管理、探测、OAuth 授权       │
        │ main.py lifespan    关闭 MCP 运行时                     │
        └──────┬──────────────────────┬──────────────────────────┘
               ▼                      ▼
        AgentService            McpAdminService                    ← 应用层入口（驱动端口）
               │                      │
        runtime/（编排层）            │
               ▼                      │
        TurnToolResolver              │                            ← 应用层编排
               │                      │
     ┌─────────┴──────┐        ┌──────┴────────────┐
     ▼                ▼        ▼                   ▼
 ToolProvider   ScopedToolProvider  McpServerConnector  McpServerRegistry   ← 被驱动端口（harness/app）
     │                │        │                   │
     ▼                ▼        ▼                   ▼
 BuiltinTool-   McpToolProvider  McpConnector     McpServerService         ← 适配器 / 业务域
 Provider             └────┬─────┘                 │（mcp_server/）
                           ▼                       ▼
            McpRuntime（进程单例：连接池、共享响应缓存、      PostgreSQL
            带 SSRF 防护的出站 HTTP、OAuth token 刷新）
                           ▼
                  远程 MCP server（Streamable HTTP）

 领域层 harness/domain：Tool、ToolPolicy、ToolOutput、mcp.py（命名、注解→策略）；所有层都依赖它，它不依赖任何层
```

| 角色 | 位置 | 内容 |
|---|---|---|
| 领域层 | `harness/domain/mcp.py`（新）、`tool.py`（改） | `mcp.py`：命名算法、注解到 `ToolPolicy` 的映射、server key 校验、`McpToolRef`；`tool.py`：`ToolOutput`、`ToolScope`、`ToolBinding` 与 `select_scoped`、`ScopedGrant`（这几项 M0 就要用） |
| 应用层：驱动端口 | `harness/app/mcp_admin.py`（新） | `McpAdminService`：server 增删改、探测、工具列表、OAuth 发起与完成 |
| 应用层：编排 | `harness/app/tool_resolver.py`（新） | `TurnToolResolver`：合并平台工具与作用域工具，产出本回合的策略全集、可见集和故障清单 |
| 应用层：被驱动端口 | `harness/app/ports.py`（改）、`harness/app/mcp_port.py`（新） | `ScopedToolProvider`、`McpServerConnector`、`McpServerRegistry` |
| 适配层 | `harness/adapters/mcp/`（新） | 全仓唯一导入 `mcp` SDK 的包，内容见第 5 节 |
| 业务域 | `mcp_server/`（新 feature 包） | ORM、Repository、`McpServerService`、HTTP 路由；不做网络 I/O |
| 组装根 | `harness/bootstrap.py`（改） | `mcp_runtime()` 进程单例、`Harness.scoped_tools` / `mcp_connector`、`mcp_admin_service(db, ...)` |
| 编排层 | `runtime/`（改） | 回合开始时解析工具，把作用域工具传给子执行，挂起时记录 `mcp_refs`，恢复时核对；不导入任何 MCP 相关模块 |

依赖规则（新增的静态检查见第 12 节）：
- `mcp` SDK 只能在 `harness/adapters/mcp/` 导入，规则同 anthropic / openai。实测 `import mcp` 会连带加载
  starlette、uvicorn、httpx2，放进应用层或领域层会破坏现有的分层检查。
- `mcp_server/` 是普通 feature 包，遵守 API → Service → Repository。它只做持久化和业务规则，
  不导入 SDK，也不导入 `harness.adapters`；需要网络的用例由应用层的 `McpAdminService` 编排。
- `McpAdminService` 经 `McpServerRegistry` 端口读写 server 配置，`McpServerService` 结构化地满足该端口，
  和 `SessionService` 满足 `SessionStore` 是同一种做法，不写转发器。设这个端口是为了让应用层测试可以换成内存替身。
- 适配器 `McpToolProvider` 不持有回合的数据库会话：每回合解析时，经组装根注入的 `session_factory` 开一个短会话，
  调用 `McpServerService` 读配置和凭证，读完立即关闭，不占用回合的事务。
  这和 `BuiltinMemoryProvider` 不同，后者直接用调用方传进来的 db。

## 3. 领域层（harness/domain）

全部是纯函数和不可变值类型：不做 I/O，不读配置，不读进程状态，不导入 `mcp` SDK。

### 3.1 `tool.py` 的改动

```python
@dataclass(frozen=True)
class ToolOutput:
    """工具执行结果。is_error 表示执行失败，但失败内容仍交给模型自行纠正（MCP isError 也走这里）。"""
    text: str
    is_error: bool = False

    @classmethod
    def ok(cls, text: str) -> ToolOutput: ...
    @classmethod
    def error(cls, text: str) -> ToolOutput: ...

class Tool(ABC):
    ...
    source: str | None = None        # 新增：None 表示内置；MCP 工具为 "mcp:{server_key}"
    @abstractmethod
    async def execute(self, arguments: dict[str, Any], ctx: ToolContext) -> ToolOutput: ...

@dataclass(frozen=True)
class ToolScope:
    """解析作用域工具所需的身份，由运行时从可信的回合身份构造。"""
    workspace_id: int
    user_id: int
```

- `source` 用 None 表示内置工具，和 `ToolInfo.source`、`tool_calls.source` 采用同一种表示，不需要来回映射。
- `ToolScope` 只放身份。按档位和 Agent 绑定选择哪些工具，由 `ToolBinding` 表达（见 6.2 节）。
  这样同一个身份解析出的「策略全集」可以在一个回合里共用。
- `PydanticTool.run` 仍返回 `str`，由 `PydanticTool.execute` 包成 `ToolOutput.ok(...)`。
  26 个内置工具的实现和描述一律不改，`tool_spec` 黄金测试不受影响。
- 主路径 `execute_one`（`runtime/tool_execution.py:207,219`）和子代理路径（`runtime/subagent.py:626-636`）的 `is_error` 现在都写死为 False。
  改成取 `ToolOutput.is_error` 后，内置工具在任何情况下都得到 False，行为不变。
  `ToolService` 自己生成的能力拒绝和 Hook 拦截文本（`tools/service.py:397-420,452-456`）**继续用 `ok`**，不改变现有输出。
- `ToolResult.content` 仍是 `str`。MCP 结果中的非文本内容由适配器转成文本（见 5.5 节），
  图片进模型属于后续阶段，届时再扩展 content 的类型。

### 3.2 `mcp.py`（新）

```python
SERVER_KEY_PATTERN = r"^[a-z](?:_?[a-z0-9])*$"  # 小写字母开头，不允许连续或结尾下划线
SERVER_KEY_MAX = 24                               # 工作区内唯一，不可修改；决定工具名前缀
MODEL_TOOL_NAME_MAX = 64                        # 取两家上限中较小的：OpenAI Chat Completions 为 64
MCP_PREFIX = "mcp__"

class ApprovalMode(StrEnum):
    ALWAYS = "always"               # 每次调用都需要审批（默认）
    TRUST_ANNOTATIONS = "trust_annotations"  # 按注解推导：只读免审，其余需审批
    NEVER = "never"                 # 全部免审（仅限可信的内部 server）

@dataclass(frozen=True)
class McpToolAnnotations:
    """MCP ToolAnnotations 的领域投影。None 表示 server 没给，判定时按规范默认值处理。
    只保留参与判定的两项；idempotentHint、openWorldHint 本期不影响任何行为，不建模。"""
    read_only: bool | None
    destructive: bool | None

@dataclass(frozen=True)
class McpToolRef:
    """模型可见名到远程工具的路由键。调用时用 remote_name，不反解 model_name。"""
    server_id: int
    server_key: str
    config_rev: int
    remote_name: str
    model_name: str
    schema_sha256: str              # inputSchema 规范化序列化后的摘要，挂起快照核对用

def model_tool_names(server_key: str, remote_names: Iterable[str]) -> dict[str, str]:
    """把一个 server 的工具原名映射为模型可见名（原名 → 模型名），结果与输入顺序无关。"""

def requires_approval(mode: ApprovalMode, annotations: McpToolAnnotations) -> bool:
    """审批模式与注解 → ToolPolicy.requires_approval。"""

def mcp_tool_policy(model_name: str, *, requires_approval: bool) -> ToolPolicy:
    """MCP 工具的能力策略：category="mcp"，callable_profiles = ALL_PROFILES_AND_DELEGATE
    （即全部 7 个档位，含 orchestrator 和 delegate），bindable_profiles 为空。
    哪些档位能看到 MCP 工具由 ToolBinding 决定（见 6.2 节），不靠档位策略。Agent 按 server 绑定，
    不经 tools 表的 tool_id 绑定，所以不需要 bindable。"""
```

**命名算法 `model_tool_names`**（参考 codex `codex-mcp/src/tools.rs` 与 openai-agents `mcp/util.py`）：

1. 清洗：原名中 `[A-Za-z0-9_-]` 以外的字符（包括 `.`）一律替换为 `_`，结果记为 `clean`。
2. 拼接：`candidate = f"mcp__{server_key}__{clean}"`。
3. 只要满足以下任一条件，就在末尾加后缀 `_{h}`，其中 `h = sha1(remote_name).hexdigest()[:8]`：
   - 清洗有损，即 `clean != remote_name`；
   - `candidate` 超过 64 个字符。这种情况先从右侧截短 `clean`，再加后缀，保证总长不超过 64。
4. 一个工具的模型名**只由 `(server_key, 它自己的原名)` 决定**，与同一 server 上的其他工具无关，也与 server 返回工具的顺序无关。
   这样 server 新增或删除工具时，已有工具的模型名不会变，按模型名配置的权限规则（9.1 节）也不会悄悄失效。
5. 同一 server 内两个原名算出相同的模型名，比如 `a.b` 和 `a_b_{同样的哈希}` 这种极端情况，就把这两个工具都剔除，
   记一条 invalid fault 和一条日志，不靠重新哈希来消歧。重新哈希会让模型名依赖整组工具，违背第 4 条。
6. `server_key` 最长 24 个字符，前缀 `mcp__{key}__` 因此最多 31 个字符，给工具名留出至少 33 个字符。

这样做的结果：
- 模型名可以直接当 `ToolSet` 的唯一键。内置工具不以 `mcp__` 开头，不会和内置工具撞名。
- 两个不同的 server_key 不可能产生相同的模型名：前缀里用 `__` 分隔，而 server_key 里不允许出现 `__`
  （`SERVER_KEY_PATTERN` 不允许连续下划线，也不允许以下划线结尾）。
- 路由只查映射表，不对模型名做字符串拆分。

**审批推导 `requires_approval`**：

| 模式 | 规则 |
|---|---|
| `always` | 一律需要审批 |
| `never` | 一律不需要审批 |
| `trust_annotations` | `destructive is True` 需要审批；否则 `read_only is True` 不需要审批；其余情况按规范默认值（destructive 和 open_world 缺省都为 true）需要审批，也就是只有显式标了只读的工具才免审 |

规范要求把来自不可信 server 的注解视为不可信，所以注解只在管理员显式选择 `trust_annotations` 时才用。
私有 server 的审批模式由创建者自己选，三档都可以，只影响本人。
`idempotentHint` 本期不影响任何行为：超时一律判为「结果未知」，不自动重试（见 5.4 节）。
工作区权限规则（`permission_rules`）对 MCP 工具同样生效，而且优先级高于这里推导出的默认值。
推导结果在每回合解析工具时现算，不进工具目录缓存，所以管理员改了审批模式，所有实例从下一回合起都会生效。

## 4. 应用层（harness/app）

应用层只认端口，不认识 MCP SDK，也不认识 `mcp_server` 包里的 ORM。

### 4.1 被驱动端口

`ports.py` 新增 `ScopedToolProvider`；`ToolProvider` 保持同步、无参数，只给内置这类进程级来源用。

```python
class ScopedToolProvider(ABC):
    """按身份异步列出工具的来源（MCP）。每回合调用一次，结果冻结进回合配置。
    返回该身份能用的全部工具，不按档位和绑定裁剪；超时由实现按来源内部的单个 server 控制。"""

    @abstractmethod
    async def list_tools(self, scope: ToolScope) -> ScopedTools: ...

@dataclass(frozen=True)
class ToolSourceFault:
    """某个来源本回合不可用的原因，只放能给用户看的文本，不放异常细节和凭证。"""
    source: str                  # "mcp:{server_key}"
    server_id: int | None        # 前端据此发起授权或跳到管理页；后台不预先生成授权地址
    kind: Literal["unavailable", "auth_required", "timeout", "invalid"]
    message: str                 # 中文，可以直接展示

@dataclass(frozen=True)
class ScopedTools:
    tools: tuple[Tool, ...]
    faults: tuple[ToolSourceFault, ...] = ()
```

不另设 `ToolProvider.list_tools(scope)` 的异步版本，原因是：
- 内置工具是进程级的纯内存对象，启动时还要用同一份清单同步工具目录和官方预置。改成异步会把 lifespan、
  `platform_tools()` 这些同步调用点全部牵连进去。
- 两类来源的生命周期不同：一个是进程级、全局唯一的静态集合，另一个是按回合、按身份解析的动态集合。
  分成两个端口，这个区别在类型上就能看出来。

`mcp_port.py`（新）放 MCP 专属、不涉及 SDK 类型的端口：

```python
class McpServerConnector(ABC):
    """对远程 server 的一次性动作：探测、OAuth 的两个半程、缓存失效。连接池与目录缓存不在这里暴露。"""

    async def probe(self, config: McpServerConfig, secret: McpSecret | None) -> McpProbeResult: ...
    async def begin_oauth(self, config: McpServerConfig, *, workspace_id: int, user_id: int) -> str: ...
    async def complete_oauth(self, callback: OAuthCallback, *, workspace_id: int, user_id: int) -> int: ...
    async def invalidate(self, server_id: int) -> None: ...   # 配置变更后让本进程的缓存和连接失效

class McpServerRegistry(Protocol):
    """server 配置的读写，McpServerService 结构化满足；方法签名以它为准，阶段 M1 回写到本文。"""
```

- `begin_oauth` 返回授权地址，流程状态由适配器写进 Redis。
- `OAuthCallback(state, code, iss)` 是前端从授权回跳地址上取下、再转交给后端的参数。
  `complete_oauth` 核对 state 里记录的工作区和用户与当前登录身份是否一致，成功后返回 server_id（流程见 8.2 节）。
- `invalidate` 只作用于当前进程。其他实例靠 `config_rev` 变化让旧键自然失效：每回合解析时都会读取最新的 `config_rev`。

`McpServerConfig`（地址、认证方式、审批模式、工具过滤、超时、`owner_user_id`）、`McpSecret`（静态请求头）、
`McpProbeResult`（server 信息、工具清单摘要、错误）都是领域值类型，放在 `harness/domain/mcp.py`。

### 4.2 编排：`TurnToolResolver`

```python
@dataclass(frozen=True)
class ResolvedScope:
    """一个身份在本回合解析出的作用域工具，同一回合的主执行和子执行共用。"""
    policies: ToolSet                       # 策略全集：平台工具 + 本身份的全部作用域工具，不裁剪
    scoped: tuple[Tool, ...]                # 本身份的全部作用域工具，按模型名排序
    faults: tuple[ToolSourceFault, ...]

    @classmethod
    def platform_only(cls, platform: ToolSet) -> ResolvedScope: ...   # 无作用域来源时用：policies 就是 platform

class TurnToolResolver:
    def __init__(self, platform: ToolSet, sources: Sequence[ScopedToolProvider],
                 *, total_timeout: float) -> None: ...

    async def resolve(self, scope: ToolScope) -> ResolvedScope: ...
```

- **为什么要把 `policies` 和可见集分开**：
  - 现在的审核员和执行层是故意按「完整工具集」查策略的，见 `policy_reviewer.py:160-162` 和 `tools/service.py:253-255` 的注释：
    被裁掉的工具如果按可见集去查，会被当成未知工具，从而跳过能力检查。
  - 黄金用例 `subagent/skill_in_allowlist` 固化了这个行为：Explore 子代理调用不在自己可见集里的 SkillRun 时，
    能力检查放行，调用交给网关。
  - 所以按名查策略只能查 `policies`。对内置工具，它就是进程级工具集，查出的结论和现在完全一样；
    对 MCP 工具，它能查到这个身份可以使用的全部 MCP 工具。
- **可见集仍然叫 `TurnConfiguration.tools`**，不改名：`test_model_request_golden.py:698` 用 `replace(runtime.configuration, tools=...)`
  替换可见集，改名会打破这份黄金测试。`TurnConfiguration` 只新增两个字段，都带默认值：`policies`（默认等于 `tools`）和 `scoped`（默认 `ResolvedScope.platform_only`）。
- **`resolve` 只管解析，不管裁剪**：
  - 各个来源并发解析，总时长上限是 `total_timeout`。单个 server 的超时由 `McpToolProvider` 自己控制（5.3 节），
    一个慢 server 不会拖垮其他 server。超时或失败只记一条 fault，不会让回合失败。
  - 按档位、模式和 Agent 绑定来裁剪可见集，仍然放在现有的 `TurnConfiguration._assemble`（`runtime/turn_config.py:121-188`）
    和 `tools_for_mode` 里。`_assemble` 多接收一个 `ResolvedScope` 参数，用 `select_scoped(resolved.scoped, binding)` 选出作用域工具，
    再并入可见集，不另写第二套裁剪逻辑。
  - `configure_turn` 多接收一个 `resolved: ResolvedScope` 参数，默认值为 `platform_only`；user_agent 的绑定在这里拿到，再传给 `_assemble`。
- **合并顺序**：内置工具在前，作用域工具在后，作用域工具内部按模型名排序，保证工具定义逐字节稳定。对提示缓存的影响见 6.4 节。
- **重名**：作用域工具和平台工具重名时直接报错。命名算法保证正常情况下不会发生，真发生了就说明有 bug，
  按「无静默兜底」的约定处理。
- **谁持有解析器**：只有 `AgentRuntime` 和 `WorkflowRuntime` 持有 `TurnToolResolver`。子代理、队友、委派不重新解析，
  直接用父执行传下来的结果（6.3 节）。

### 4.3 驱动端口：`McpAdminService`

供 `mcp_server/api.py` 调用的应用层入口，负责编排「持久化 + 网络」两件事：

| 方法 | 编排 |
|---|---|
| `create_server(workspace_id, actor, request)` | 校验角色（共享 server 要 owner / admin；私有 server 要活跃成员，且没超过每人上限）、重名和地址（SSRF）→ 探测 → 探测成功才写库 → 返回工具清单摘要 |
| `update_server(...)` | 地址或认证有变化时重新探测 → 写库（`config_rev` 加一）→ `connector.invalidate` |
| `delete_server(...)` | 软删 server 并级联软删 Agent 绑定 → `invalidate`；已绑定的 Agent 从下一回合起就看不到这个 server |
| `refresh_tools(...)` | 强制重新拉取工具清单，返回和上次快照的差异（新增、删除、schema 变化） |
| `begin_oauth(...)` / `complete_oauth(...)` | 见第 8 节 |

管理接口照常走 `get_db` 管理请求事务，顺序固定为：先在库里校验角色和重名，再做网络探测，最后写库。

- 探测包括建连和翻页拉取工具清单，整体用 `mcp_probe_timeout_seconds` 设上限。探测期间这个请求的事务一直空闲挂着，
  最长挂这么久。管理操作频率低，可以接受这个代价，不为此破例分步提交。
- 和 HugAgentOS 一样，探测成功之后才写库，失败的探测不会留下半条配置。
- 「先查重名 → 探测 → 写库」之间存在并发竞态：两个请求可能都通过了重名检查。
  最后由部分唯一索引兜底，Service 捕获 `IntegrityError` 后转成 `AgentException.message("server_key 已存在", status_code=409)`，
  不让它变成 500。

## 5. 适配层（harness/adapters/mcp）

```text
harness/adapters/mcp/
├── runtime.py        McpRuntime：进程单例，持有连接池与共享的响应缓存，提供 aclose()
├── pool.py           ConnectionPool：按身份持有 SDK Client，每个 Client 由一个专属 task 托管
├── provider.py       McpToolProvider（ScopedToolProvider 的实现）：按 server 并发列工具、命名、过滤、推导策略
├── tool.py           McpTool（Tool 的子类）：调用时按 McpToolRef 路由
├── results.py        CallToolResult → ToolOutput
├── connector.py      McpConnector（McpServerConnector 的实现）：探测与 OAuth 的两个半程
├── http.py           出站 HTTP 客户端工厂：SSRF 校验、按 IP 连接、请求头清洗
└── oauth.py          OAuth：授权的两个半程、McpTokenAuth（httpx2.Auth）、持锁刷新的回调
```

### 5.1 SDK 版本

- 现在锁定的是 `mcp 1.28.1`，仓库里还没有任何代码导入它。实现前改为 `mcp>=2.3,<3`。v2 原生支持 2026-07-28 规范，
  并能向下协商旧版本；v1 的 `streamablehttp_client` 在 v2 里已删除，只能用 `streamable_http_client(url, http_client=...)`。
  请求头、超时和认证都配在 SDK 依赖的 `httpx2.AsyncClient` 上。
- v2 依赖 `httpx2`，和 openai 3.0.0 依赖的是同一个包，不会多引入一套 HTTP 栈。
- 依赖从 `mcp[cli]` 改成 `mcp`：服务端用不到 CLI 那一套。
- 升级前，先在 uv.lock 里核对新增和变化的传递依赖：`mcp-types`、`opentelemetry-api`、starlette、sse-starlette、uvicorn，
  确认与项目现有版本不冲突。
- `jsonschema` 要在 `ArgumentsRule` 里直接使用（9.1 节），改成直接依赖。`pyjwt` 现在是 mcp 带进来的传递依赖，
  而 `auth/service.py`、`api/deps.py` 直接 `import jwt`，借这次改依赖一并补成直接依赖。
- 本文写到的 SDK 行为都以 v2.3 源码为准（本地克隆 commit `77001c9`），升级到新的小版本时要重新核对 5.3、5.6 和 8.2 节。

### 5.2 连接生命周期

规范 2026-07-28 去掉了协议级会话（`Mcp-Session-Id`）和 `initialize` 握手，每个请求自带协议版本。
连接因此只是一个「复用 HTTP 连接池加上协商结果」的句柄，不承载会话状态。对旧版 server，SDK 仍然会握手，
并在连接上保存 session id。

- **连接池的键**：`(server_id, config_rev, auth_partition)`。
  - `config_rev` 是配置版本号，地址、认证、过滤、超时、启用状态任一变更就加一，旧连接随之作废。
    审批模式不参与，它每回合现算（3.2 节）。
  - `auth_partition` 是授权分区：共享 server 的静态请求头取 `ws:{workspace_id}`，私有 server 和 OAuth server 取 `user:{user_id}`，
    与 SDK 响应缓存的 `partition` 是同一个值（5.3 节）。
  - 不同用户的凭证绝不共用一条连接。
- **由专属 task 托管**：anyio 要求 `async with` 的进入和退出在同一个 task 里。每条连接由 `pool.py`
  起一个专属 task，在这个 task 里 `async with Client(...)` 并一直持有；调用方通过队列提交请求，
  等待结果时同时监控「结果返回」和「托管 task 结束」，谁先发生算谁。这样连接在后台断掉时，调用方会马上收到错误，
  而不是等到读超时。参考 FastMCP 的 `_await_with_session_monitoring` 和 HugAgentOS 的 `session.py:112-151`。
- **建连**：在托管 task 内用 `anyio.fail_after(mcp_connect_timeout_seconds)` 包住，和 SDK 内部的 cancel scope 保持同一套机制。
  建连过程一律在 `AsyncExitStack` 里完成，全部成功才 `pop_all()` 交出去，中途失败的部分会被自动清理
  （参考 pydantic-ai `mcp.py:1384-1461`）。
- **回收**：空闲超过 `mcp_idle_seconds`（默认 300）就关闭；有调用正在执行的连接不回收。
  池的总容量是 `mcp_pool_max_connections`，超出时按 LRU 淘汰空闲连接；全部连接都忙时，新请求等待，
  不会无限新建。
- **不自动重连，也不重放**：连接断开后标记为失效，下一次调用新建连接；已经发出的调用不重发。
  重发的条件见 5.4 节。规范 2026-07-28 已删除 `ping`，所以不做周期性心跳。
- **关闭**：`McpRuntime.aclose()` 通知所有托管 task 退出，关闭时用 `CancelScope(shield=True)` 加
  `move_on_after(3)` 收尾，避免关闭过程被外层的取消打断。lifespan 退出时，在 `shutdown_executions()` 之后调用它。
- **多实例**：连接和缓存都是进程本地的，不需要会话亲和。同一个用户的两个回合落在不同实例上，各自建连。

### 5.3 工具发现与缓存

SDK v2 的 `Client` 按会话维护工具状态：
- `x-mcp-header` 到 `Mcp-Param-*` 请求头的映射、outputSchema 校验，都要求这个工具在当前会话里列出过
  （`client/session.py:1067-1070,1110-1133`）；
- `Client.list_tools` 自带按授权分区隔离的响应缓存，缓存命中时会把结果回灌给会话（`client/client.py:967-987`）。

所以**不自建工具目录缓存**，直接用 SDK 的缓存：

- **缓存**：所有 Client 共用一个进程级的 `InMemoryResponseCacheStore`，通过 `CacheConfig(store=..., partition=...)` 传入。
  - `partition` 就是授权分区，由可信身份派生，取值同 5.2 节。
  - `default_ttl_ms` 取 `mcp_tools_cache_seconds`；server 返回了 `ttlMs` 时以 server 为准，但不超过这个上限。上限由适配器包装 store 时截断。
  - SDK 会按 `cacheScope` 区分 public 和 private 条目，不同用户之间不会串数据。
  - AgentOS 要自己构造 httpx2 客户端（SSRF 防护和认证都挂在上面），所以传给 `Client` 的是 Transport，不是 URL。
    这种情况下，共享 store 必须显式给 `CacheConfig.target_id`，否则 SDK 直接抛 ValueError（`client/client.py:408-421`）。
    取值为 `target_id = f"{server_id}#rev={config_rev}"`：任何配置变更都会让 `config_rev` 加一，从而换一个缓存键，
    旧条目不用等 TTL 过期就不会再被用到。
    `target_id` 里不放 URL，也就不会把 URL 里可能带的凭证混进缓存键。
- **时机**：每个回合开始时，`McpToolProvider.list_tools(scope)` 并发对每个可用的 server 调用 `Client.list_tools`（缓存命中就不发请求），
  单个 server 用 `mcp_list_timeout_seconds` 控制超时。
  这个超时必须大于 `mcp_connect_timeout_seconds`，否则建连还没超时就先被它打断了。
- **分页**：沿着 cursor 翻页，上限 50 页、1000 个工具，遇到重复的 cursor 就记一条 invalid fault，不会死循环。
  这里不用 SDK 的 `ClientSessionGroup`，它只取第一页。
- **list_changed**：本期不订阅（2026 版需要通过 `subscriptions/listen` 订阅长连接）。
  工具变化靠 TTL 过期和管理员手动刷新感知，回合内的工具集冻结不变，见 6.3 节。
- **刷新失败时保留旧目录**：适配器在进程内为每个 `(server_id, partition)` 保存最近一次成功的工具清单。
  缓存过期后重新拉取失败，就继续用这份旧清单，同时记一条 `unavailable` fault（参考 codex `tool_list_changes.rs`）。
  只有从来没成功拉到过清单时，这个 server 才在本回合缺席。
  用旧清单发起的调用，如果连接上的会话没列出过这个工具，`x-mcp-header` 映射会缺失；
  这种情况由 SDK 自己按需重新列出（`_relist_tool`），适配器不另做处理。
- **schema 预处理**：
  - `inputSchema` 缺少 `properties` 时补上空对象；
  - 不做 OpenAI strict 转换，客户端本来就不开 strict；
  - 单个 schema 序列化后超过 `mcp_max_schema_bytes`（默认 16 KiB）就剔除这个工具，记一条 invalid fault 和一条日志；
  - `description` 超过 2000 个字符就截断；
  - 存在 `x-mcp-header` 映射但不合法的工具定义，按规范要求剔除。
- **过滤**：管理员配置的 `tool_allowlist` / `tool_denylist` 按 **MCP 原名**匹配，在命名之前就过滤掉。

### 5.4 调用

`McpTool.execute(arguments, ctx)` 的流程：

1. 用 `ctx.workspace_id`、`ctx.user_id` 重新算出授权分区，取得连接。冻结在工具里的 `McpToolRef` 只决定路由，
   不携带凭证，所以工具对象可以安全地复用。
2. `call_tool(remote_name, arguments, read_timeout_seconds=...)`。超时取 server 配置和全局 `mcp_call_timeout_seconds`
   中较小的那个。超时或者被取消时，SDK 会按传输方式通知 server：HTTP 关闭响应流，stdio 发 `notifications/cancelled`。
3. 结果交给 `results.py` 转换，见 5.5 节。
4. 失败按下表映射，**全部转成 `ToolOutput.error(...)` 交给模型，不往外抛异常**。这样单个 server 故障不会让回合变成 failed：

| 情况 | 返回给模型的文本 | 是否重试 |
|---|---|---|
| `isError: true` | server 返回的错误内容 | 否 |
| 调用超时 | 「MCP 工具 {model_name} 调用超时（{n} 秒），结果未知」 | 否，是否重调由模型自己决定 |
| 连接断开或传输错误 | 「MCP server {key} 连接失败」 | 只有请求确实没有发出去（建连阶段就失败）时，才自动重试 1 次 |
| 401 且 OAuth 刷新失败 | 「需要重新授权 MCP server {key}」，同时把这个用户在这个 server 上的 token 标记为失效 | 否 |
| JSON-RPC 协议错误 | 错误码和错误信息 | 否 |
| outputSchema 校验失败 | 「MCP 工具返回的结构不符合其声明的 outputSchema」 | 否 |

- 写操作超时就判为「结果未知」，绝不自动重发。HugAgentOS 和 ADK 都是这么做的，因为写操作有副作用，重发可能执行两次。
- 本回合人工审批等待期间不计时：审批挂起时，回合已经结束，工具根本没有发出调用；恢复之后才真正调用，
  超时从那时开始算。MRTR 的 `input_required` 本期不支持（见 9.2 节）。
- 本期不发进度通知。规范规定进度只能由 server 发给 client，收到的进度通知只记 debug 日志。
- 落库：`isError` 和表里其他错误一样，经 `ToolOutput.error` 返回。`ToolService` 遇到 `is_error=True` 时，
  把 `tool_calls.status` 记为 `failed`，`error_message` 存截断后的文本，PostToolUse 钩子的载荷里带上 `is_error`。
  这样按 `source` 就能统计每个 server 的调用失败率。「工具执行完成」日志里的状态字段也按 `is_error` 取值。
  内置工具不会返回 `is_error=True`，现有的落库和日志都不变。

### 5.5 结果转换（`results.py`）

| MCP 内容类型 | 转成的文本 |
|---|---|
| `text` | 原文 |
| `image` / `audio` | 占位文本 `[图片 image/png，约 12 KB，未发送给模型]`，二进制数据丢弃 |
| `resource_link` | `[资源链接 {name}: {uri}]` |
| 内嵌 `resource` | 文本资源取它的 text；二进制资源用占位文本 |
| `structuredContent` | 只在 `content` 里没有文本块时，才用 `json.dumps(ensure_ascii=False)` 补成文本，避免内容重复 |

多个块之间用空行连接。总长度超过 `mcp_max_output_chars`（默认 20000）时，保留开头和结尾，中间截掉，
并注明「已截断 N 字符」。其余内容类型一律用 `model_dump_json()` 序列化，不能用 `str()`，后者会输出 Python repr。

### 5.6 出站 HTTP 与 SSRF（`http.py`）

用户填写的地址会由服务端去连接，所以必须做 SSRF 防护。只有 mcp-context-forge 做得比较完整，
HugAgentOS 漏掉了 `100.64.0.0/10`（阿里云元数据地址 `100.100.100.200` 就在这个段里）。

- **只允许 https**。开发环境可以通过 `mcp_allow_insecure_http=true` 放开 http，生产环境必须为 false。
- 地址中不允许出现 userinfo、片段，也不允许 `${VAR}` 这样的占位符（参考 LibreChat GHSA-4pcc）。
- **地址判定**：DNS 解析出的**每一个**地址都必须通过 `is_public_address(a)`，用白名单思路判定：
  1. 先还原内嵌的 IPv4 地址：IPv4 映射地址（`::ffff:a.b.c.d`）、IPv4 兼容地址（`::a.b.c.d`）、
     NAT64 地址（`64:ff9b::/96`、`64:ff9b:1::/48`）、6to4 地址（`2002::/16`）、Teredo 地址；
  2. 还原后的地址要满足 `is_global`，同时不能是 `is_multicast`、`is_reserved`、`is_unspecified`。
  
  只看 `is_global` 不够。在 Python 3.14.4 上实测：
  - `224.0.0.1`（组播）的 `is_global` 为 True；
  - `64:ff9b::a9fe:a9fe`（NAT64 映射到元数据地址 169.254.169.254）、`::7f00:1` 的 `is_global` 也为 True，只有 `is_reserved` 能把它们识别出来；
  - `100.100.100.200`、`169.254.169.254`、`10.0.0.1`、`::ffff:127.0.0.1`、`fd00::1` 的 `is_global` 为 False。
  
  这组地址全部写进测试用例。
- **按 IP 连接，防 DNS rebinding**：校验用的那份解析结果，就是连接时用的地址。传输层固定连到校验过的 IP，
  同时保留原主机名用于 SNI 和 Host 头。具体实现方式（自定义 `httpx2` transport，或者自定义 resolver）在 M1 阶段确定，
  并回写到本文。固定 IP 按主机生效：OAuth 元数据、授权服务器这类不同主机的请求，各自解析、各自校验、各自固定。
- **重定向**：SDK v2 不看客户端的 `follow_redirects`，自己决定怎么跟随（`client/streamable_http.py:725-733`）。规则是：
  - 只跟随同源、且请求方法不变的重定向（POST 只认 307/308）；
  - 其他重定向一律不跟随，并报错；
  - SDK 的 OAuth provider 也用同一条规则。
  
  因为只跟随同源重定向，目标主机和已经校验、固定的主机是同一个，不会因此出现新的 SSRF 面。
  适配器自己发出的请求（探测元数据、换取 token）把 `follow_redirects` 设为 False，收到 3xx 就当作失败。
- 内网 server 需要运维显式放行：`mcp_allowed_private_hosts` 是一份主机名精确匹配的白名单，默认为空。
  白名单只对共享 server 生效，私有 server 的地址必须是公网地址。
- 每个请求的请求头只来自 server 配置和 OAuth，不转发入站请求的任何头。请求头的值禁止包含 CR 或 LF。

## 6. 回合内的工具集

### 6.1 现状假设与必须先拆掉的地方（M0）

现在的代码默认工具集是**进程级、静态、同步**的，而且执行路径和审核路径都**按名字查进程级的 `platform_tools()`**。
一个只在某个工作区存在的 MCP 工具，即使放进了本回合的 ToolSet，也会被下面这些地方挡住：

| 位置 | 现在的行为 | 对 MCP 工具的后果 |
|---|---|---|
| `tools/gateway.py:165` `from_registry`（严格模式） | 先按 `platform_tools().callable_names` 过滤 | 抛 `ValueError`，回合配置失败 |
| `tools/gateway.py:83` `__post_init__` | 用非严格模式过滤 | 静默丢掉 MCP 工具 |
| `tools/gateway.py:111` `create` | 以 `platform_tools()` 为底，取 `bindable_names` | 未知工具抛「工具不存在或未注册」 |
| `tools/service.py:255` `ToolService.policy_source` | 执行层兜底时按进程级工具集查策略 | 查到 `unknown_tool_policy`，判为不能调用 |
| `permission/service.py:59` `default_behavior` | 按 `platform_tools()` 查 `requires_approval` | 未知工具默认 **allow**，MCP 工具自带的审批要求失效 |
| `permission/service.py:23` `dangerous_tools()`，调用方 `runtime/interaction_flow.py:212` | 同上，用来生成审批原因文案 | MCP 工具的审批原因一律显示「该工具已被配置为需审批」 |
| `bootstrap.py:277-282` 两个审核员的 `policy_source=tools` | 构造时就冻结成进程级工具集 | `CapabilityRule` 判为不能调用 |
| `policy_reviewer.py:60-61,84-85` | `policy_source is None` 时回落到 `builtin_tool_set()` | 违反「端口依赖必填、不写 None 回落」，M0 一并删掉 |
| `runtime/tool_execution.py:207,219`、`runtime/subagent.py:626-636` | `is_error` 写死为 False | MCP 的 `isError` 无法传给模型 |
| `tools/gateway.py:238`、`tools/service.py:352` | `run` 返回 `str` | 没有通道把 `is_error` 带出执行层 |
| `runtime/subagent.py:324-336` `_resolve_tools`、`:420-425` 档位二次裁剪 | 以 `self.tools`（进程级）为底 | 子代理、委派、Workflow 成员看不到 MCP 工具 |
| `runtime/agent.py:758-767` 唤醒队友 | 只传 `parent_allowed_tool_names`（名字） | 队友拿不到 MCP 工具对象 |
| `harness/app/ports.py:323-335` `SubAgentLauncher.run`、`ToolContext`（`harness/domain/tool.py:140-143`） | 只带工具名 | Agent 工具没法把父回合的 MCP 工具对象交给子代理 |
| `runtime/agent.py:404-425` → `runtime/interaction_flow.py:298-321` | 审批响应的请求路径只用平台工具配置回合，再 `validate_input` | MCP 工具的改参审批抛「工具不存在」 |

M0 是纯重构，不引入任何 MCP 代码，对外行为不变，七份黄金测试的期望字面量保持不变：

1. **按名查策略改用「本回合的策略全集」**（4.2 节的 `ResolvedScope.policies`），不再查进程级工具集：
   - `ReviewRequest` 新增 `policies: ToolPolicyLookup | None = None`。`ToolPolicyLookup` 是一个只有 `policy_for(name)` 一个方法的 Protocol，
     ToolSet 天然满足。新字段放在最后并带默认值，现有构造它的测试不用改。
     两处生产代码要显式传值：主路径 `runtime/tool_execution.py:100` 传 `configuration.policies`，
     子代理路径 `runtime/subagent.py:613` 传网关的 `policies` 字段。
   - `CapabilityRule` 改为从请求中取 `policies`；请求里没有时，用审核员构造时注入的平台工具集。审核员的 `policy_source` 改为必填，
     删掉 `policy_reviewer.py:60-61,84-85` 里 None 时回落到 `builtin_tool_set()` 的分支。
   - `PermissionService.evaluate` 新增**必填**关键字参数 `policy: ToolPolicy`，由 `PermissionRule` 查好后传入；
     `default_behavior` 改为读这个参数，不再查 `platform_tools()`。生产代码里调用 `evaluate` 的只有 `policy_reviewer.py:230` 一处。
     `tests/test_permission_service.py` 的两处调用同步补上这个参数。
   - 删掉 `dangerous_tools()`（`permission/service.py:23-30`）。它唯一的调用方 `interaction_flow.py:212` 的审批原因文案，
     改为按 `configuration.policies.policy_for(name).requires_approval` 判断。对内置工具，两种判断给出的结果相同。
   - **`ToolGateway` 把 `policies` 和 `scoped` 作为构造字段**，和现有的 `subagents` 字段一样（`tools/gateway.py:42`）。
     `__post_init__`、`create`、`from_registry` 改为查 `policies`，`ToolService` 的 `policy_source` 也从网关取这个字段。
     **`gateway.run` 和 `gateway.start_call` 的签名一律不加参数**：tool_review 黄金测试的 `_FakeGateway.run` 会把所有关键字参数
     原样记进字面量（`test_tool_review_golden.py:481`）。
     网关由 `TurnConfiguration._assemble` 和 `SubAgentRunner.run` 构造，在构造时传入这两个字段。
   - `SubAgentRunner._resolve_tools` 和 `:420-425` 的档位二次裁剪中的 `callable_names`，改为查 `policies`。
2. **`platform_tools()` 只留给「只有工具名、没有回合」的管理场景**：工具目录同步、Agent 的 tool_id 绑定校验、
   市场与预置的 tool_key 解析、权限规则配置接口、`ToolResponse.bindable_profiles`。这些场景只涉及内置工具，
   MCP 工具不走这些路径（6.2 节、9.1 节）。12.1 节新增的静态检查会限制它的调用位置。
3. **执行结果带上 `is_error`**：
   - `Tool.execute`、`ToolGateway.run`、`ToolService.run` 都改为返回 `ToolOutput`；
   - 主路径和子代理路径改为读取 `output.is_error`；
   - `ToolService` 落库时把 `ToolOutput.text` 交给 `tool_repo.succeed`，落库内容不变。
4. **作用域工具对象的传递通道**（6.3 节）：
   - `ToolContext` 新增 `scoped: ScopedGrant | None = None`，由 `ToolService.run` 从网关字段读出后放进去；
   - `SubAgentLauncher.run`、`IsolatedSubAgentLauncher.run`、`SubAgentRunner.run` / `run_teammate` 新增关键字参数 `scoped`，默认 None；
   - `SubAgentRunner` 用两份工具集：
     - 查策略用 `self.tools.with_tools(*scoped.tools)`；
     - 可见集在平台工具裁剪结果的基础上，加上 `scoped.visible` 里列出的作用域工具；
   - `BaseOrchestrator`（Workflow）新增参数 `resolved`，带默认值。`test_model_request_golden.py:754-758` 直接构造了编排器，不能因此报错。
   - 所有新参数为 None 时，行为和现在完全一样。
5. **回合工具解析的接口**：
   - 新增 `ToolScope`、`ToolBinding`、`ScopedGrant`（都放在 `harness/domain/tool.py`，M0 就要用到）、`ScopedToolProvider`、`ResolvedScope`、`TurnToolResolver`；
   - `AgentRuntime._produce` / `_produce_resume` 和 WorkflowRuntime 在 `_configure_agent` 之前先 `resolve`；
   - `Harness.scoped_tools` 默认 `()`，此时 `resolve` 直接返回 `ResolvedScope.platform_only`，没有任何 I/O。

替换之后，内置工具查到的策略和原来一样：`scoped_tools=()` 时，`policies` 就是进程级工具集本身。
M0 必须修改的测试见 12.3 节。

### 6.2 绑定模型：哪些作用域工具进入本回合

MCP 工具**不进** `tools` 表。这张表是全局的，`tool_key` 和 `runtime_name` 全局唯一，没有 workspace_id，
启动时 `sync_builtins` 还会软删不在名单里的 builtin 行。把按工作区动态变化的远程工具塞进去，
会和这三条全部冲突。所以绑定放在 server 这一级：

| 档位 | 本回合可见的作用域工具 | 由谁决定 |
|---|---|---|
| `lead`（默认 Agent） | 工作区里所有已启用的共享 server，加上本人的私有 server，且当前用户能用（OAuth 已授权）；受 `tool_allowlist` / `tool_denylist` 约束 | `ToolBinding.all()` |
| `user_agent`（自建 Agent） | 只包括 `agent_mcp_servers` 里绑定的共享 server；一条绑定可以再带 `tool_names` 白名单（MCP 原名），NULL 表示该 server 的全部工具 | 该 Agent 的绑定 |
| `delegate`（委派自建 Agent） | 被委派 Agent 自身的绑定，与父回合本轮可见的工具取交集（与现有「三者取交集」一致） | Agent 工具按被委派 Agent 的绑定选出 |
| `workflow_node` | 成员 Agent 自身的绑定 | WorkflowRuntime 按成员的绑定选出 |
| `subagent`（一次性子代理） | 规格没有工具白名单（general-purpose）时，继承父回合本轮可见的 MCP 工具；有白名单（Explore、Plan、Verification）时一个都不给 | Agent 工具按规格决定 |
| `teammate` | 规则同 `subagent`，取决于队友成员的规格 | AgentRuntime 唤醒队友时决定 |
| `orchestrator` | 不加载 | `ToolBinding.none()` |
| turbo 模式 | 不加载 | `ToolBinding.none()` |
| Plan 模式 | 解析照常进行，但按 `PLAN_MODE_ALLOWED_TOOLS` 白名单裁剪可见集时会被剔除，所以 Plan 模式下不可用 | 现有的 `tools_for_mode` |

- `ToolBinding` 是领域值类型，有三种构造：`ToolBinding.all()`、`ToolBinding.servers({server_id: frozenset[str] | None})`、`ToolBinding.none()`。
  选择函数 `select_scoped(tools, binding) -> tuple[Tool, ...]` 是纯函数。两者都放在 `harness/domain/tool.py`，M0 就要用到。
  因为领域层的 `Tool` 不认识 server_id，`select_scoped` 改为按 `Tool.source`（`mcp:{server_key}`）配合映射表来选。
  具体签名在 M0 实现时回写到本文。
- 主回合的绑定，由 `configure_turn` 根据 `AgentDefinitionService.get_runtime_config` 的结果构造。
  `AgentRuntimeConfig` 新增字段 `mcp_bindings`，必须带默认值：四份黄金测试会用全部字段构造这个类型，
  分别在 react_loop :1141、tool_review :344、role_context :225、model_request :660，而且都在「不许改」段里。
- MCP 绑定**不放进 `SubAgentSpec`**。`role_context` 黄金测试逐字段固化了 `fields(SubAgentSpec)`；
  而且绑定是运行时数据，不是规格的一部分。
- 规格白名单（`allowed_tools`）只对平台工具生效，作用域工具由调用方按上表选好后再传进来。
  这样「Explore 不带 MCP」和「委派 Agent 只带它绑定的 MCP」都由调用方一处决定，`SubAgentRunner` 不用区分档位。
- 自建 Agent 的绑定存在新表 `agent_mcp_servers`（见第 7 节）。市场快照和官方预置**本期不导出 MCP 绑定**：
  server 是工作区私有的，地址和凭证都不能跨工作区带走。Agent 导出导入的方案目前搁置，到时再一起设计。
- **M1 到 M2 之间的过渡**：M1 还没有绑定表，这段时间只有 `lead` 用 `ToolBinding.all()`，其他所有档位一律 `ToolBinding.none()`。

### 6.3 解析时机、冻结、传递与恢复

**在哪里解析**：后台执行体里每个回合解析一次。

- 位置在 `AgentRuntime._produce` 里，那次 `_configure_agent` 之前：先 `resolver.resolve(scope)` 得到 `ResolvedScope`，
  再把它作为参数传给 `_configure_agent` → `configure_turn` → `_assemble`。`_assemble` 按绑定选出作用域工具，并入可见集，
  结果写进 `TurnConfiguration`：`tools` 是可见集，另有 `policies` 和 `scoped` 两个字段；faults 留给发事件用（4.2 节）。
- 请求路径（`start`）不解析：`start` 要尽快返回 `run_started`，而解析远程工具可能要几秒；后台执行体本来就是流式的。
  `start` 里那次 `_configure_agent` 用的是 `ResolvedScope.platform_only`，只做准入校验，不触发任何 MCP I/O。
- 恢复路径 `_produce_resume` 同样在 `_configure_agent` 之前解析。WorkflowRuntime 在自己的执行体开头解析一次，全部成员共用这一份结果。

**回合内冻结**：一个回合里的多轮 ReAct 共用同一份工具集，中途不重新拉取。这对应 HugAgentOS 的「run 内冻结 schema」。

**故障通知**：有 fault 时，在 `turn_start` 之后发一条新事件 `RuntimeEvent.tool_sources`，内容是 `faults[{source, server_id, kind, message}]`。
- `auth_required` 不携带授权地址。授权地址只能由用户自己发起请求来获取（8.2 节），前端拿到 `server_id` 后调用授权接口。
- 这是新增的事件类型，现有事件不变。

**传给子代理、队友、委派**：同一个回合里身份不变，所以这些子执行都不重新解析。
- 父回合把本身份的全部作用域工具（`ResolvedScope.scoped`），连同按 6.2 节为子执行选出的可见名单，一起往下传，
  打包成值类型 `ScopedGrant(tools: tuple[Tool, ...], visible: frozenset[str])`，放在 `harness/domain/tool.py`。
- 传递通道是 M0 新增的那几个参数：
  - `ToolContext.scoped`：父回合的 `ScopedGrant`，交给 Agent 工具使用；
  - `SubAgentLauncher.run(..., scoped=...)`、`SubAgentRunner.run / run_teammate(..., scoped=...)`：子执行收到的 `ScopedGrant`。
- `SubAgentRunner` 用两份工具集：
  - 查策略用 `self.tools.with_tools(*scoped.tools)`；
  - 可见集是「平台工具按规格和父名单裁剪的结果」，再加上 `scoped.tools` 里名字在 `scoped.visible` 中的那些工具。
- 来自 Workflow 的成员调用时，由 WorkflowRuntime 按成员的绑定构造 `ScopedGrant`。

**挂起与恢复**：
- ask 挂起时，只有当待执行的调用里有 MCP 工具，`continuation` 才新增 `mcp_refs` 这个键，
  值是这些调用的 `McpToolRef`，包括 `config_rev` 和 `schema_sha256`。没有 MCP 调用时，`continuation` 的形状和现在完全一样，
  黄金测试里固化的形状不受影响。
- 恢复时重新解析，通常命中缓存，不需要网络 I/O。然后逐个核对每个待执行的 MCP 调用：它的路由键是否还在，
  `config_rev` 和 schema 摘要是否都一致。
  - 不一致的情况包括：server 已删除、地址或认证改了、工具已下线、schema 已变化。
  - 遇到不一致，这个调用记为拒绝，文本为「MCP 工具已变更，本次审批的调用未执行」，然后继续处理剩下的调用。
  - 不会在用户没批准过的地址或 schema 上执行。
- 现在审批通过之后**不再重新审核**，这是已知行为，黄金测试也已固化。MCP 工具同样沿用这个行为，
  靠上面的核对，保证「批准的」和「执行的」是同一个工具、同一个地址、同一份 schema。

**审批时改参数**：
- 审批响应这条请求路径只装配了平台工具（`runtime/agent.py:404-425`），没法对 MCP 工具做 `validate_input`。
- 所以本期 MCP 工具的 `tool_approval` 载荷里，`can_update_input` 为 False。
- 检查放在 `InteractionFlow._normalize_response`（`runtime/interaction_flow.py:298-321`）：这个函数本来就会读出已落库的
  `request_payload`。如果载荷里 `can_update_input is False`，而请求又带了 `updated_input`，就抛
  `AgentException.message(..., status_code=422)`。判断依据是载荷里的标志，不是工具名前缀；检查也不放在路由层，
  这样不会碰到 agent_service 黄金测试替身化的路由依赖。
- 内置工具的载荷不变，`can_update_input` 仍为 True。要支持修改 MCP 工具的参数，需要把校验推迟到后台恢复时再做，这一项列入 M4。

### 6.4 提示缓存

- Anthropic 渲染请求的顺序是 tools → system → messages，现有的缓存断点只打在 system 段上
  （`harness/adapters/models/anthropic_client.py:430-447`）。所以只要 `tools` 有任何变化，整段前缀缓存都会失效，
  MCP 工具排在内置工具后面也带来不了局部收益。
- 能做到的是让 `tools` 尽量不变：
  - 回合内冻结；
  - 顺序确定：内置工具按 BuiltinToolProvider 的顺序在前，MCP 工具按模型名字典序在后；
  - 命名与 server 返回工具的顺序无关。

  同一个用户连续两个回合，只要 MCP 工具没有变化，`tools` 就逐字节相同，跨回合的缓存仍然有效。
- MCP 工具的描述只放在 `tools` 里，不进系统提示。server 的使用说明（`instructions`）本期不注入，原因见 9.3 节。

## 7. 数据模型（mcp_server 域）

所有表都继承 Base 的审计字段和软删除，带中文 comment，外键显式写 `ondelete`，在 `database/registry.py` 登记，
用新的 Alembic 迁移创建。

**`mcp_servers`**：server 配置，共享 server 和私有 server 都在这张表里，不放凭证。

| 列 | 类型 | 说明 |
|---|---|---|
| id | PK | |
| workspace_id | FK workspaces，CASCADE，索引 | 租户 |
| owner_user_id | FK users，CASCADE，可空，索引 | NULL 表示共享 server；非空表示私有 server，值为创建者（7.2 节） |
| server_key | String(24) | 工作区内唯一（部分唯一索引，`is_deleted = false`），共享和私有一起算，创建后不可修改，决定工具名前缀 |
| name / description | String(120) / Text | 展示用；description 由配置者填写，不取 server 自报的内容 |
| url | String(2048) | 规范化后的 https 地址（canonical URI，不带尾斜杠） |
| auth_type | String(16) | `none` / `static_headers` / `oauth` |
| approval_mode | String(24) | `always`（默认）/ `trust_annotations` / `never` |
| tool_allowlist / tool_denylist | JSONB，MutableList，default=list | MCP 原名；allowlist 为空表示不限制 |
| call_timeout_seconds | Integer，可空 | 为空时用全局配置，非空时与全局配置取较小值 |
| config_rev | Integer，默认 1 | 地址、认证、过滤、超时、启用状态任一变更就加一，作为连接池的键和挂起快照的核对依据 |
| is_enabled | Boolean | |
| last_probe_at / last_probe_error | 时间戳 / Text | 最近一次探测的时间和结果，错误文本已脱敏 |
| tools_snapshot | JSONB，MutableList，default=list | 最近一次成功拉到的 `[{name, description, input_schema_sha256, annotations}]`，只用于管理界面展示、差异对比和权限规则的工具名校验，**运行时不读** |
| created_by | FK users，SET NULL | |

`tools_snapshot` 的来源：
- 静态请求头或无认证的 server，由管理员探测时写入；
- OAuth server 的管理员探测不带 token（8.4 节），拿不到工具清单。所以每当有用户授权成功或者回合解析成功时，
  只要清单和快照不一致，就由 `McpServerService.record_tools_snapshot` 在独立短事务里回写快照，
  内容只有工具元数据，不含任何用户数据。快照为空时，权限规则改为按 server 前缀校验（9.1 节）。

**`mcp_server_secrets`**：静态请求头，一个 server 一行。

| 列 | 说明 |
|---|---|
| server_id | FK mcp_servers，CASCADE；部分唯一索引（`is_deleted = false`） |
| headers | JSONB，MutableDict，default=dict；`{header_name: value}`，明文 |

**`mcp_oauth_clients`**：OAuth 客户端注册信息，一个 server 一行。issuer 的绑定和校验由 SDK 处理：issuer 记在 client_info 里，
授权服务器一换，SDK 会清空旧的 client_info 并重新注册（`auth/oauth2.py:684-694`）。SDK 的 `TokenStorage` 接口不带 issuer 参数，
所以表上不以 issuer 作键。

| 列 | 说明 |
|---|---|
| server_id | FK mcp_servers，CASCADE；部分唯一索引（`is_deleted = false`） |
| issuer | String，普通列，用于排查问题和展示 |
| client_info | JSONB；内容是 SDK 的 `OAuthClientInformationFull`，可能包含 client_secret，明文 |

**`mcp_oauth_tokens`**：按用户保存的 token。

| 列 | 说明 |
|---|---|
| server_id | FK mcp_servers，CASCADE |
| user_id | FK users，CASCADE；与 server_id 组成部分唯一索引（`is_deleted = false`），撤销授权（软删）之后可以重新授权 |
| issuer | String，普通列 |
| token | JSONB；内容是 SDK 的 `OAuthToken`，明文 |
| expires_at / scope | 明文，用于提前刷新和排查问题 |

**`agent_mcp_servers`**：自建 Agent 绑定的 server。按「表归谁就由谁的域管」的约定，这张表归 **agent 域**，
和 `agent_tools` 放在一起（`agent/models.py`）。

| 列 | 说明 |
|---|---|
| agent_id | FK agents，CASCADE |
| server_id | FK mcp_servers，CASCADE；与 agent_id 组成部分唯一索引 |
| tool_names | JSONB，MutableList，可空；MCP 原名白名单。只有 NULL 表示这个 server 的全部工具，写入空列表 `[]` 时报中文错误，避免「空等于全部」这种放权方向的歧义 |

跨域调用和导入方向：
- agent → mcp_server：写入绑定时，`AgentDefinitionService` 调用 `McpServerService.require_usable(workspace_id, server_ids)`，
  校验这些 server 都属于本工作区、已启用，并且是共享 server（私有 server 不能绑定到 Agent，7.2 节）。这是模块级导入。
- mcp_server → agent：软删 server 时，`McpServerService` 调用 `AgentDefinitionService.unbind_mcp_server(server_id)`，
  由 agent 域软删相关绑定。这条在函数体内局部导入，用来打破导入环，仓库里已有先例 `harness/adapters/tools/builtin/agent.py:140-141`。
  `tests/test_import_isolation.py` 只检查模块级导入环。

### 7.1 凭证存储

MCP 凭证明文存储，与 `model_providers.api_key` 的做法一致（2026-11-12 拍板）。涉及三样东西：
静态请求头的值、OAuth 的 access_token 和 refresh_token、OAuth 客户端注册信息里的 client_secret。

- 凭证单独放在 `mcp_server_secrets`、`mcp_oauth_clients`、`mcp_oauth_tokens` 三张表里，不和 `mcp_servers` 放在一起。
  列表、详情这类展示接口只读 `mcp_servers`，响应 Schema 用 `from_attributes` 转换时不会把凭证带出去；
  只有建连和 OAuth 流程才读凭证表。
- 凭证不写日志、API 错误响应、SSE 事件、`tool_calls` 和测试快照。
- 已知风险：能读数据库或备份的人可以直接拿到这些凭证。规范要求客户端在传输和存储中对 refresh token 保密
  （`basic/authorization/index.mdx:304-306`，MUST），明文存储时这一条只能靠数据库的访问控制来保证。
  以后如果要加密，改动集中在这三张表和 `McpServerService` 读写凭证的几个方法上，外加一次数据迁移。

接口回显：
- 请求头只返回名字，值统一显示为 `********`；更新时如果客户端回传的是掩码，就保留原值。
- OAuth 只返回「已授权 / 未授权 / 已过期」和 scope，不返回 token。

### 7.2 私有 server

工作区成员可以给自己配置 MCP server，只有本人能看到和使用（2026-11-11 拍板）。私有 server 和共享 server 在同一张表里，
`owner_user_id` 非空即为私有。

**可见性**：

- 私有 server 只出现在 owner 本人的回合里，`lead` 档位通过 `ToolBinding.all()` 加载（6.2 节）。
  回合的身份是发起人：会话只有创建者能写（`session/service.py:168-177`），审批恢复也会核对发起人
  （`runtime/interaction_flow.py:287`），所以私有 server 不会被别人的回合用到。
- 管理接口只对 owner 本人返回私有 server；工作区管理员也看不到别人的私有 server，因为它的地址可能带着个人信息。
- **自建 Agent 不能绑定私有 server**：Agent 是工作区共享资源，别的成员用这个 Agent 时并没有这个 server。
  `McpServerService.require_usable` 遇到私有 server 直接报中文错误。
- 子代理、队友、委派沿用 6.2 节的规则，从父回合继承，所以只会出现在 owner 本人的执行链里。

**凭证**：

- 私有 server 的静态请求头属于个人凭证，授权分区取 `user:{owner_user_id}`，不与工作区共享（5.2 节）。
- 私有 server 也可以用 OAuth，流程同 8.2 节，只有 owner 本人能发起授权。
- 凭证同样存在 `mcp_server_secrets`，按 server_id 一行。server 本身已经归属到人，不需要再按用户分行。

**安全约束**：

- SSRF 规则相同，但私有 server **不适用** `mcp_allowed_private_hosts` 白名单，地址必须是公网地址。
  白名单是运维为共享 server 放行内网服务用的，普通成员不能借它访问内网。
- 审批模式由 owner 自己选（3.2 节），只影响本人。
- 每人的私有 server 数量上限为 `AGENTOS_MCP_MAX_PRIVATE_SERVERS_PER_USER`（默认 10），避免回合开始时并发列工具的数量失控。
- 权限规则：私有 server 的工具名同样以 `mcp__{server_key}__` 开头。工作区管理员不知道私有 server 的存在，
  不会给它的工具配全局规则；owner 在审批时选 `always_scope=global` 仍按 9.1 节，需要 owner / admin 身份。
- `server_key` 和共享 server 在同一个命名空间里唯一。这样不同 server 的工具名不会重复，权限规则也不会误伤。
  代价是建 server 时可能因为 key 已被别人的私有 server 占用而失败，管理员建共享 server 时也一样。错误信息只说「已被占用」，不透露是谁。

**成员离开工作区**：私有 server 保留在库里但不再生效，因为每个请求都会实时校验工作区成员身份（`api/deps.py:55-80`）。用户被删除时按外键 CASCADE 清理。

## 8. 认证

### 8.1 静态请求头（`auth_type=static_headers`）

- 共享 server 由管理员配置，整个工作区共用，授权分区是 `ws:{workspace_id}`，适合 API Key 这类服务账号凭证。
  私有 server 的请求头归 owner 个人，授权分区是 `user:{owner_user_id}`（7.2 节）。
- 请求头名称不能是 `Host`、`Content-Length`、`Mcp-*`、`Cookie`，值不能包含 CR 或 LF。
- 请求头里不支持 `{{user_id}}` 这类模板变量：这是请求头注入和凭证窃取的常见入口（LibreChat GHSA-pmw7）。
  需要按用户区分身份的 server，请走 OAuth。

### 8.2 OAuth 2.1（`auth_type=oauth`）

授权分两条路径，复用 SDK 的程度不一样。下文提到的 SDK 行为都以 v2.3 源码（`client/auth/oauth2.py`）为准。

**为什么不能把调用路径整个交给 `OAuthClientProvider`**：

- SDK 的 provider 初始化时，用 `_initialize` 从存储读出 token（`:564-568`），但没有设置过期时间。
  `is_token_valid` 又把「没有过期时间」当成有效（`:186-192`），结果库里读出来的过期 token 会被原样发出去。
- 收到 401 后，SDK 直接走完整授权流程，不先尝试刷新。这个过程中还会重新发现元数据；如果发现 issuer 对不上，
  会清空 client_info 并重新做 DCR，改写共享的 client 记录。最后才因为缺少 `redirect_handler` 报错（`:385-443,624-787`）。
- provider 初始化后把 token 缓存在内存里，只有刷新失败时才重新读存储（`:620`）。别的实例刷新过的 token，它看不到。
- 浏览器授权码流程在同一个协程里先调 `redirect_handler(url)`，再 `await callback_handler()` 等授权码（`:385-443`）。
  这是为桌面客户端设计的：本地起一个回调服务器。到了多 worker 的服务端，发起授权和收到回调是两次独立的 HTTP 请求，
  可能落在不同进程上。如果让发起授权的协程挂着等，就会退化成 HugAgentOS 那种「worker 轮询 Redis、一重启流程就丢」的做法。

**AgentOS 的做法**：token 的生命周期（读取、刷新、写回）由 AgentOS 自己掌握。协议细节则复用 SDK 导出的工具函数
（`auth/utils.py` 里构造 PRM / AS 元数据发现 URL 的函数、`validate_metadata_issuer`、`validate_authorization_response_iss`、
`create_client_registration_request`、`should_use_client_metadata_url`，以及 `auth/oauth2.py:115` 的 `PKCEParameters`），不自己重写协议。
M3 开始时先做一次 spike，确认具体能复用哪些函数，结论回写到本文。

1. **`McpTokenAuth(httpx2.Auth)`**：调用路径上的认证插件，按 `(server, user)` 构造，挂在这个用户的 MCP 连接上。
   - 首次请求时从 `McpServerService` 读出 token 缓存在内存里，后续请求直接用缓存；发现 `expires_at` 已过期就先刷新。
     收到 401 时，先**重新读库**：如果别的实例已经刷新过，直接换用新 token 重发；否则自己刷新一次再重发。
     这样不用每个请求都查一次库，又能感知其他实例的刷新。
   - 收到 401 就刷新一次再重发；刷新也失败，就返回 401。适配器把它映射成 `auth_required`（5.4 节）。
     这里不发起完整授权，因为服务端不能替用户打开浏览器。
   - 收到 403 `insufficient_scope`（step-up）同样映射成 `auth_required`，提示用户重新授权更大的 scope。
2. **刷新加锁**：
   - 刷新放在一个**专用的短事务**里完成，不借用回合的会话。事务里依次做这几步：
     1. `SET LOCAL lock_timeout = '5s'`；
     2. `SELECT … FROM mcp_oauth_tokens WHERE server_id=… AND user_id=… FOR UPDATE`，给这一行加锁；
     3. 再读一次 token，如果已经被别的实例刷新过就直接用；
     4. 否则向授权服务器发刷新请求，在同一事务里写回，提交时锁随之释放。
   - 用行锁，不用会话级的 `pg_advisory_lock`。会话级锁在协程被取消时可能跟着连接回到连接池，导致后续刷新一直阻塞；
     行锁在事务提交或回滚时一定会释放。`lock_timeout` 到期就报 `auth_required`，不无限等待。
   - 职责划分：加锁读、写回的 SQL 放在 mcp_server 的 Repository；刷新的 HTTP 请求由适配器以回调形式传给
     `McpServerService.refresh_token_locked(server_id, user_id, refresh: Callable[[OAuthToken], Awaitable[OAuthToken]])`。
     mcp_server 域自己不做网络 I/O。
   - 这个事务覆盖刷新的整个 HTTP 往返。它不借用回合的会话：回合一旦回滚，新 token 也不能跟着丢，
     因为授权服务器那边旧的 refresh_token 可能已经失效。这属于事务规则的例外，代码注释里要写明生命周期原因。
   - 加锁是为了防止多个实例同时用同一个 refresh_token 刷新，被授权服务器当成重放攻击而吊销整组授权（LibreChat #15977）。
   - 提前刷新：回合开始解析工具时，如果离过期不到 `mcp_oauth_refresh_skew_seconds`（默认 300），就主动刷新，
     并加 0～30 秒的随机抖动，避免大量用户同时到期时集中刷新（LibreChat #12802）。
3. **授权的两个半程**：
   - 回跳地址指向**前端路由** `{AGENTOS_PUBLIC_BASE_URL}/mcp/oauth/callback`，不指向后端。
     后端只认 Bearer 认证（`api/deps.py:17-29`），仓库里没有 cookie 会话，浏览器从授权服务器跳回来时也不会带 Bearer，
     所以「核对当前登录用户」这一步只能由前端带着 Bearer 来完成。
   - `POST /api/mcp/servers/{id}/oauth/authorize`（前半程，Bearer 鉴权）：
     - 依次做 PRM 发现、AS 元数据发现和 issuer 校验、客户端注册（优先 CIMD，其次 DCR，结果写入 `mcp_oauth_clients`），
       最后生成 PKCE 参数和 state；
     - 把 `{state → (workspace_id, user_id, server_id, issuer, code_verifier)}` 写入 Redis，TTL 10 分钟；
     - 返回授权地址，地址里带 `resource` 参数（RFC 8707）。
   - `POST /api/mcp/oauth/complete {state, code, iss}`（后半程，Bearer 鉴权）：
     - 前端从回跳地址上取下这三个参数，带着 Bearer 调用这个接口；
     - 后端从 Redis 用 GETDEL **一次性取出** state，**核对 state 里记的 workspace_id、user_id 是否就是当前登录用户**，
       一致才继续，以此防止「把受害者的授权绑到攻击者账户上」（LibreChat GHSA-vf7j）；
     - 校验 `iss`（RFC 9207），再带着 code_verifier 和 `resource` 换 token，写入 `mcp_oauth_tokens`；
     - 哪个 worker 收到这次请求，就由它完成后半程。
   - 新增配置项 `AGENTOS_PUBLIC_BASE_URL`（前端的对外地址），用到 OAuth 时必填，要同步更新 `.env`、`.env.example` 和 Settings。
   - Redis 在项目里已经是必需依赖（run 事件流，启动时自检）。`disable_redis_events=true` 时 run 事件流退回进程内后端，
     这时 OAuth state 也退回进程内存储，只适合单进程本地调试，和 run 事件流的取舍一致。
4. **不实现 SDK 的 `TokenStorage`**：调用路径用的是 `McpTokenAuth`，授权的两个半程复用的是 `auth/utils.py` 里的工具函数，
   这些函数都不读写存储，所以两条路径都用不到 `TokenStorage` 协议。

### 8.3 缺少授权时的体验

- 回合开始解析工具时，如果某个 OAuth server 没有当前用户的有效 token，或者刷新失败，这个 server 本回合不提供工具，
  同时记一条 `auth_required` fault（带 `server_id`），通过 `tool_sources` 事件推给前端，前端据此引导用户去授权。
  回合本身照常进行。
- 本期不做「缺少授权就挂起回合、等用户授权完再恢复」这种 ask 形态（ADK 的 `_toolset_auth.py` 是这么做的），原因有两个：
  - 模型还没决定要不要用这个 server，就先把回合挂起，打断得太早；
  - 现有的 ask 挂起以单次工具调用为单位，不以工具来源为单位。

  2026-11-11 拍板：只提示用户去授权，不挂起回合。
- 调用过程中收到 401 并且刷新失败：返回 `ToolOutput.error("需要重新授权…")`，同时把这个用户在这个 server 上的
  token 标记为失效。

### 8.4 健康状态区分身份

- 「server 挂了」和「这个用户没有凭证」是两回事，401 / 403 不能当作网络不可达。mcp-context-forge #4495
  就因为用管理员身份探测按用户授权的 server，把这类 server 全部误标成了离线。
- 管理员探测 OAuth server 时，只验证 PRM 和 AS 元数据能否发现，不需要 token；`last_probe_error` 只记录与身份无关的错误。

## 9. 审核与审批

### 9.1 接入现有审核链

经过 M0 之后，MCP 工具不需要任何专门的审核规则，`PolicyReviewer` 现有的规则链原样适用：

| 规则 | 对 MCP 工具的作用 |
|---|---|
| BudgetRule | 和内置工具一样计入工具调用预算 |
| PlanModeToolsRule | Plan 模式下，不在白名单里的工具一律拒绝 |
| ArgumentsRule | 调用 MCP 工具的 `normalize_arguments`：按 inputSchema 做 JSON Schema 校验（jsonschema，方言 2020-12），不通过就拒绝 |
| CapabilityRule | 从 `policies` 查到 `mcp_tool_policy`（3.2 节），全部档位都放行 |
| PermissionRule | 优先级依次为：会话规则、工作区 global 规则（`permission_rules.tool_name` 写模型名）、内置 Agent 的默认规则、按工具推导的默认值 |

- 按工具推导的默认值，就是 3.2 节由 `approval_mode` 和注解推导出的 `requires_approval`：需要审批就是 ask，否则是 allow。
- 需要审批的调用沿用现有的 `tool_approval` 交互。MCP 工具的载荷有三处不同：
  - `tool.source = "mcp:{server_key}"`，前端据此标注「来自外部 MCP server」；
  - `can_update_input` 为 False（6.3 节）；
  - `reason` 按 `policies` 判定，不再使用 `dangerous_tools()`（6.1 节）。

  内置工具的载荷保持不变。
- **「始终允许」的作用范围**：现在用户在审批时可以选 `always_scope=global`（`interaction/schemas.py:98-111`），
  运行时就会写入一条工作区级的 allow 规则（`runtime/interaction_flow.py:584-590` → `permission/service.py:100-116`），
  而这条路径只校验会话写权限。通过权限规则接口写全局规则则要求管理员身份（`permission/api.py`）。
  如果 MCP 工具沿用这个行为，任何成员审批一次，就能把管理员设的 `always` 变成全工作区放行。
  所以对 MCP 工具：
  - `always_scope=global` 只允许工作区 owner 或 admin 使用，其他成员返回 403；
  - 默认作用域 `session` 不受影响；
  - 检查同样放在 `InteractionFlow._normalize_response`：它读出 `request_payload` 后就能拿到工具名和 `tool.source`。
    当 `source` 以 `mcp:` 开头、`decision=always_allow` 且 `always_scope=global` 时，调用
    `WorkspaceService.require_workspace_role(workspace_id, user_id, {"owner", "admin"})`，这个方法自带 403。
  - 检查不放在 `interaction/api.py`：路由层只有会话写权限校验，拿不到工具名。
    另外，agent_service 黄金用例 `resolve/tool_approval_always_allow_updated_input` 带了 `always_scope=global`，
    它的驱动只替换了 SessionService 和运行时类，路由里如果多查一次库就会报错。

  内置工具的这个现状保持不动（2026-11-11 拍板）。
- 工作区 global 规则配置接口的工具名校验：`permission/api.py` 对 `mcp__` 开头的名字不走「工具未注册」校验，
  改为检查它是否属于本工作区某个共享 server 的 `tools_snapshot`；快照为空的 OAuth server，按 `mcp__{server_key}__` 前缀校验。
  私有 server 不参与这项校验，管理员看不到它们，校验结果也不能泄露它们的存在。
- 子代理没有审批通道，`NoApprovalChannelRule` 会把 ask 合并为拒绝，和内置工具的处理方式一样。

### 9.2 Elicitation 与 MRTR

- 规范 2026-07-28 用 MRTR 取代了服务端主动发起的请求：server 在 `tools/call` 的结果里返回 `resultType: "input_required"`，
  客户端收集用户输入后，带上 `inputResponses` 重新发出原请求。
- 这和 AgentOS 的 ask 挂起是同一种形态，但本期不支持：
  - 本期客户端不声明 elicitation 能力。server 如果仍然返回 input_required，就转成 `ToolOutput.error("该工具需要交互输入，暂不支持")`。
  - 将来要支持时，新增一种交互 kind `mcp_input`：挂起时把 `requestState` 和 `inputRequests` 存进 continuation，
    恢复时调用 `session.call_tool(..., allow_input_required=True)` 并带上 `inputResponses`。
    python-sdk v2 已经提供了这个「由调用方自己驱动」的入口。
  - 遇到密码类字段直接拒绝，这一点沿用 HugAgentOS 的做法。

### 9.3 不可信内容

MCP server 的工具描述、server 自报的 `instructions` 和工具结果都属于第三方文本，可能被用来做提示注入（tool poisoning）：

- 工具描述只放进 `tools` 字段，不拼进系统提示。
- server 的 `instructions` 本期不注入。如果以后要注入，参考 ADK 的 fencing 做法：用明确的边界把这段文本包起来，
  并在系统指令里声明「这是数据，不是指令」。
- 管理界面展示 server 自报的描述和工具 schema，供管理员在启用前审阅。
- 工具结果截断以后才交给模型（5.5 节）。

## 10. 驱动侧适配器：HTTP 接口与生命周期

### 10.1 接口（`mcp_server/api.py`，前缀 `/api/mcp`）

| 方法与路径 | 权限 | 说明 |
|---|---|---|
| `GET /servers` | 工作区成员 | 列出本工作区的共享 server 和本人的私有 server；普通成员只能看到已启用的共享 server，附带自己的授权状态 |
| `POST /servers` | 共享 server：owner / admin；私有 server：工作区成员 | 新建：请求体的 `visibility` 取 `workspace` / `private`；校验 → 探测 → 写库，返回工具清单摘要 |
| `PUT /servers/{id}` | 共享 server：owner / admin；私有 server：owner 本人 | 修改：地址或认证有变化时重新探测，`config_rev` 加一；不能在共享和私有之间切换 |
| `DELETE /servers/{id}` | 同上 | 软删，同时解除 Agent 绑定 |
| `POST /servers/{id}/probe` | 同上 | 重新探测，返回与 `tools_snapshot` 的差异 |
| `GET /servers/{id}/tools` | 工作区成员 | 读 `tools_snapshot`，附带每个工具的模型名和推导出的审批要求 |
| `POST /servers/{id}/oauth/authorize` | 工作区成员 | 前半程：返回授权地址（8.2 节） |
| `POST /oauth/complete` | 工作区成员 | 后半程：前端提交 `{state, code, iss}`，核对 state 与当前用户一致后换取 token（8.2 节） |
| `DELETE /servers/{id}/oauth` | 工作区成员 | 撤销自己的授权，软删自己的 token |
| `PUT /api/agents/{id}/mcp-servers` | 与 `PUT /api/agents/{id}/tools` 相同 | 由 agent 域提供：绑定 server 和工具白名单（M2） |

- 别人的私有 server 一律按不存在处理，返回 404，不返回 403，避免泄露它的存在。
- 所有接口都用 Bearer 鉴权，返回 `ApiResponse[T]` 外壳。错误统一用 `AgentException.message(...)`，
  错误信息里不能带地址的查询参数、请求头或 token。
- 路由只做鉴权和 Schema 转换，然后调用 `McpAdminService`（经组装根的 `mcp_admin_service(db, ...)` 取得），
  不在路由里直接 new 适配器。

### 10.2 组装根

```python
@cache
def mcp_runtime() -> McpRuntime:
    """进程级 MCP 运行时：连接池与共享响应缓存。首次取用时惰性创建，不建任何连接。"""

@dataclass(frozen=True)
class Harness:
    ...
    scoped_tools: tuple[ScopedToolProvider, ...] = ()       # 新增；builtin_harness() 在 MCP 开启时为 (McpToolProvider(...),)
    mcp_connector: Callable[[], McpServerConnector] | None = None   # 新增；只有管理入口用到
```

- 只有 `agent_runtime(...)` 和 `workflow_runtime(...)` 会构造 `TurnToolResolver(self.tools, self.scoped_tools, ...)` 并注入运行时；
  `subagent_runner(...)` 不构造，子执行直接用父执行传下来的 `ScopedGrant`（6.3 节）。
- `tests/fixtures/harness_factory.py` 现在直接调用 `default_harness()`（`:25,38`），自己不带默认值，
  所以要改成 `replace(default_harness(), scoped_tools=())`，显式关掉作用域来源。测试需要 MCP 时再注入内存版假来源，
  完全不碰网络和数据库。
- `IsolatedSubAgentLauncher.run` 把 `scoped` 参数原样转交给 `SubAgentRunner.run`（6.3 节）。
- 新增 `mcp_admin_service(db, *, user_id, workspace_id) -> McpAdminService`。`mcp_connector` 为 None 时，
  调用它会抛中文 AgentException。
- `main.py` lifespan：
  - 启动时不连接任何 MCP server，server 不可用不影响启动；
  - 退出时，在 `shutdown_executions()` 之后、`close_run_event_store()` 之前调用 `await mcp_runtime().aclose()`；
  - 只有运行时已经创建过才去关闭，免得为了关闭反而先把它创建出来。
- memory worker 不用 MCP，不导入这部分。

### 10.3 落库与事件

- 调用入参原样写入 `tool_calls`，不做脱敏（2026-11-11 拍板）。
- `tool_calls` 表新增 `source` 列：`String(64)`，可空，NULL 表示内置工具；MCP 工具的值为 `mcp:{server_key}`。
  - MCP 工具的 `tool_id` 为 NULL，`tool_name` 记模型名。
  - `source` 不通过 `ToolRepository.start` 的参数写入，而是建出记录后、仅当 `tool.source` 非空时再赋值。
    react_loop 黄金测试的 `ToolRepository.start` 替身签名是固定的，这样写不会碰到它。
  - 按 `source` 加 `status` 就能统计每个 server 的调用量和失败率（`is_error` 的落库方式见 5.4 节）。
- SSE 的 `ToolInfo` 新增可选字段 `source`。内置工具的 `source` 为 None，按「只输出非 None 字段」的规则（`core/events.py:170-177`）
  不会被序列化，所以现有事件逐字节不变，react_loop 黄金测试不受影响。
- 新增 `RuntimeEvent.tool_sources(session_id, faults)`，只在有 fault 时发出。

## 11. 配置项

新增到 `core/config.py` 的 Settings，同步更新 `.env` 和 `.env.example`（`.env.example` 只放占位值）：

| 环境变量 | 类型与默认值 | 说明 |
|---|---|---|
| `AGENTOS_PUBLIC_BASE_URL` | str \| None = None，用到时必填 | 前端的对外访问地址，用来拼 OAuth 回跳地址 |
| `AGENTOS_MCP_ENABLED` | bool = true | 总开关；关闭时 `scoped_tools=()`，管理接口返回中文错误 |
| `AGENTOS_MCP_CONNECT_TIMEOUT_SECONDS` | float = 10 | 建立连接的超时 |
| `AGENTOS_MCP_LIST_TIMEOUT_SECONDS` | float = 15 | 每回合解析单个 server 工具的超时（包括建连和翻页），必须大于建连超时 |
| `AGENTOS_MCP_RESOLVE_TIMEOUT_SECONDS` | float = 20 | 每回合解析全部作用域工具的总超时 |
| `AGENTOS_MCP_PROBE_TIMEOUT_SECONDS` | float = 30 | 管理员探测（建连加翻页拉取完整清单）的总超时 |
| `AGENTOS_MCP_CALL_TIMEOUT_SECONDS` | float = 120 | 单次工具调用超时的上限 |
| `AGENTOS_MCP_IDLE_SECONDS` | int = 300 | 连接空闲多久后回收 |
| `AGENTOS_MCP_POOL_MAX_CONNECTIONS` | int = 64 | 进程内连接总数上限 |
| `AGENTOS_MCP_TOOLS_CACHE_SECONDS` | int = 300 | 工具清单缓存的 TTL：server 没给 `ttlMs` 时用它，给了也不超过它 |
| `AGENTOS_MCP_MAX_OUTPUT_CHARS` | int = 20000 | 单次结果交给模型的字符上限 |
| `AGENTOS_MCP_MAX_SCHEMA_BYTES` | int = 16384 | 单个工具 schema 的大小上限 |
| `AGENTOS_MCP_OAUTH_REFRESH_SKEW_SECONDS` | int = 300 | 提前刷新的时间窗 |
| `AGENTOS_MCP_ALLOW_INSECURE_HTTP` | bool = false | 只用于开发，允许 http 地址 |
| `AGENTOS_MCP_ALLOWED_PRIVATE_HOSTS` | str = "" | 逗号分隔的内网主机名白名单，只对共享 server 生效 |
| `AGENTOS_MCP_MAX_PRIVATE_SERVERS_PER_USER` | int = 10 | 每人在一个工作区里的私有 server 数量上限 |

- `AGENTOS_PUBLIC_BASE_URL` 缺失时，Settings 仍然能加载，但第一次用到 OAuth 就会抛具体的中文错误，并指出缺的是哪个配置项。
  这样既不影响不用 MCP 的部署启动，也不会静默回落。
- 超时之间必须满足 `CONNECT < LIST < RESOLVE`，Settings 校验时检查，不满足就启动失败。

## 12. 测试策略

### 12.1 新增的静态检查（`tests/test_import_isolation.py`）

| 规则 | 范围 |
|---|---|
| `mcp` 只能在 `harness/adapters/mcp` 中导入 | 任何位置都算，包括函数体和 TYPE_CHECKING 块 |
| `mcp_server` 包不得导入 `harness.adapters`，也不得导入 `mcp` | 业务域不能直接接触适配器 |
| `runtime/` 不得导入 `mcp_server` | 运行时只能经 `ScopedToolProvider` 端口拿到 MCP 工具 |
| `harness/app`、`harness/domain` 不得导入 `mcp_server` | 应用层经 `McpServerRegistry` 端口访问，领域层完全不认识它 |
| `runtime/`、`tools/gateway.py`、`tools/service.py` 中的 `ToolService` 整个类（包括 `__init__`）、`harness/adapters/reviewers/` 不得调用 `platform_tools()` | M0 之后，按名查策略只能查本回合的 `policies` |
| 全仓不得出现 `dangerous_tools` | M0 已删除这个函数，防止有人加回来 |

每条规则都要配一个自测：故意写一段违规代码，确认检查确实会报错。

### 12.2 替身与集成

- **领域层**：
  - 命名算法做性质测试：结果与输入顺序无关、总长不超过 64、只含合法字符、不同 server 之间不撞名、已知输入得到固定的期望名字；
  - `requires_approval` 测完整真值表；
  - `select_scoped` 测各种绑定组合。
- **适配层**：
  - 用 python-sdk v2 的 `mcp.Client(server)` 在进程内连接一个用 `MCPServer` 写的假 server，不占端口；
  - 覆盖分页、`isError`、结构化结果、图片、超时和取消、连接断开、schema 超限、缓存按 partition 隔离；
  - HTTP 和 OAuth 相关测试用 `httpx2.ASGITransport` 挂载假 server 和假授权服务器（参考 pydantic-ai `tests/test_mcp.py`）。
- **SSRF**：固定一组 DNS 解析结果，覆盖：
  - 100.64/10、169.254/16、组播地址；
  - IPv4 映射、IPv4 兼容、NAT64 地址；
  - 多条 A 记录里混有私网地址；
  - 跨源重定向；
  - rebinding（校验时和连接时解析结果不同）；
  - 私有 server 的地址命中 `mcp_allowed_private_hosts` 时仍然拒绝。
- **运行时**：`tests/fixtures/` 新增一个内存版 `ScopedToolProvider`，测试时经 `harness_factory` 注入，验证：
  - 模型请求里 tools 的顺序；
  - fault 事件；
  - 审批挂起、恢复和快照核对（server 删除、`config_rev` 变化、schema 变化三种情况）；
  - 带 `updated_input` 的 MCP 审批返回 422；
  - 子代理、队友、委派、Workflow 成员按 6.2 节拿到的 MCP 工具；
  - Explore 子代理调用父回合有、自己不可见的 MCP 工具时，审核结论与内置工具的同类情况一致；
  - 非管理员用 `always_scope=global` 审批 MCP 工具时返回 403；
  - 私有 server 只出现在 owner 本人的回合里，自建 Agent 绑定私有 server 时报错。
- **PostgreSQL 集成**：
  - 部分唯一索引，包括撤销授权后重新授权、`server_key` 并发创建时返回 409；
  - 两个连接同时刷新同一个 token，确认只发出一次刷新请求；
  - 软删 server 时级联解除绑定；
  - 私有 server 的可见性：管理接口对别人返回 404，列表里看不到；`server_key` 与别人的私有 server 冲突时返回 409，信息里不带 owner。

### 12.3 黄金测试

**期望字面量不变**：M0 不改变对外行为，七份黄金测试的期望字面量（`tests/fixtures/*_golden.py`，包括 SHA-256）**一个字都不改**。

**一次性破例：修改「不许改」段里的替身**。下面三处替身位于黄金测试声明「不许改」的段落里，
而 M0 改了它们所替身的接口，签名不跟着改，测试会直接失败（不是比对不一致，而是类型错误）。
这三处替身只改签名和返回类型，记录下来的观测内容一项不动。修改前，按 CLAUDE.md harness 约定第 12 条在提交说明里写明理由：

| 替身 | 所在段 | 改法 | 为什么非改不可 |
|---|---|---|---|
| `tests/test_tool_review_golden.py:595-619` `_install_permissions._evaluate` | 观测（`:380` 起，不许改） | 增加关键字参数 `policy`，不记录 | `evaluate` 新增了必填参数 `policy`，不改就是 TypeError |
| `tests/test_react_loop_golden.py:884-899` `_Fakes.evaluate` | 替身与记录（`:433` 起，不许改） | 同上 | 同上 |
| `tests/test_tool_review_golden.py:460-484` `_FakeGateway.run` | 观测（不许改） | 返回值从 `f"执行结果:{tool_name}"` 改成 `ToolOutput.ok(同一文本)`，`executions` 的记录内容不变 | 网关改为返回 `ToolOutput`，运行时要读 `.is_error` |

这次破例到此为止，以后不再援引。

**不用改的地方**：下列改动在设计上就避开了黄金测试，「不许改」段和驱动段都不需要动：
- 网关的 `run` 和 `start_call` 不加参数，`policies` 和 `scoped` 都是网关的构造字段（6.1 节第 1 步）。
  `_FakeGateway.run` 会把收到的关键字参数原样写进 `executions` 字面量，所以加参数就会改变字面量。
- 新增字段一律带默认值，包括 `AgentRuntimeConfig.mcp_bindings`、`ReviewRequest.policies`、`TurnConfiguration.policies` / `scoped`、
  `BaseOrchestrator` 的 `resolved`、`ToolContext.scoped` 和各个 `scoped` 参数。
- 可见集仍然叫 `TurnConfiguration.tools`，`test_model_request_golden.py:698` 的 `replace(..., tools=...)` 照常可用。
- continuation 只在有 MCP 调用时才多出 `mcp_refs` 这个键。
- `ToolInfo.source` 为 None 时不会被序列化。
- `tool_calls.source` 不通过 `repo.start` 的参数写入。
- react_loop 黄金测试里，`tool.succeed` 记录的是 `ToolService` 解包后的文本，记录内容不变。
- `harness_factory` 改为 `replace(default_harness(), scoped_tools=())`，显式关掉作用域来源（10.2 节）。
  不这样做的话，从 M1 起 `AGENTOS_MCP_ENABLED` 默认为 true，驱动 `_produce` 的黄金测试会走到真实的 McpToolProvider，去连真实数据库。

**非黄金测试中需要跟着改的地方**（只改签名和返回类型，断言的语义不变）：
- `Tool.execute` 改为返回 `ToolOutput`：
  - `tests/test_tool_set.py:76,152,178`
  - `tests/test_tool_identity.py` 中 `FakeTool` 的三处（`:135,237,294`）
  - `tests/test_agent_delegation_tool.py:137,236`
  - `tests/test_delegate_policy_and_team_spawn.py:103`
- 网关替身改为返回 `ToolOutput`：
  - `tests/test_agent_delegation_runtime.py:231`
  - `tests/test_agent_delegation_runner.py:261`
  - `tests/test_policy_reviewer.py:346`
  - `tests/test_session_store_in_memory.py:113`
- 构造签名变化：
  - `tests/test_tool_identity.py` 中构造 `ToolService(...)` 的四处
  - `tests/test_turn_cancel_pg.py:563,607`
  - `tests/test_tool_gateway.py` 中的全部 `create(...)` 调用
  - `tests/test_agent_delegation_tool.py:74-81` 的 `_fake_dispatch`
  - `tests/test_policy_reviewer.py:45` 构造的 `ReviewRequest`，以及构造审核员时 `policy_source` 改为必填
  - `tests/test_permission_service.py:49,74` 补上 `policy` 参数

以上清单是复审时查出来的。M0 的第一个提交还要跑一遍完整测试，再核对一次，遗漏的补进提交说明。

**MCP 落地之后**：
- 没有 MCP 来源（`scoped_tools=()`）时，七份黄金测试的输出保持不变。
- M1 新增第八份黄金测试 `tests/test_mcp_tool_golden.py`，固化三样东西，字面量取自 M1 实现的实际产出：
  - 一组假 server 工具经过命名和 schema 处理后，发给两家模型的 tools 字面量；
  - 审批推导矩阵；
  - 结果转换的输出。
- 同时把 CLAUDE.md、AGENTS.md、README 里的「七份」改成「八份」（第 15 节）。

## 13. 阶段

| 阶段 | 内容 | 完成判据 |
|---|---|---|
| M0 | 前置重构，即 6.1 节的第 1～5 步：按名查策略改为查本回合的 `policies`；`ToolOutput` 贯穿执行层；作用域工具的传递通道；`ToolScope`、`ToolBinding`、`ScopedGrant`、`ScopedToolProvider`、`ResolvedScope`、`TurnToolResolver`；删除审核员的 None 回落和 `dangerous_tools()`；12.3 节列出的一次性替身破例；修改第 15 节列出的上位文档 | 七份黄金测试字面量不变；用内存假来源跑通「解析 → 审核 → 执行 → 挂起 → 恢复」，以及子代理继承；完整测试通过 |
| M1 | SDK 升级到 v2；`harness/domain/mcp.py`；`harness/adapters/mcp/`（连接池、McpTool、结果转换、SSRF 防护）；`mcp_server` 域的 `mcp_servers`、`mcp_server_secrets` 两张表、迁移和静态请求头认证；共享与私有 server（7.2 节）；管理接口与 `permission/api.py` 对 `mcp__` 名字的校验；组装根与 lifespan；`tool_sources` 事件；`tool_calls.source`、`ToolInfo.source` 和审批载荷里的 `tool.source`；挂起快照核对；MCP 审批 `can_update_input=False` 与 422；`always_scope=global` 的管理员限制；第八份黄金测试 `test_mcp_tool_golden.py`。本阶段只有 `lead` 加载 MCP 工具，其余档位一律 `ToolBinding.none()` | 进程内假 server 端到端、SSRF、新增静态检查、第八份黄金测试全部通过 |
| M2 | 自建 Agent 绑定 server（`agent_mcp_servers`、agent 域接口）；委派、Workflow 成员、子代理、队友按 6.2 节加载 | 绑定、裁剪、各档位继承的运行时测试通过 |
| M3 | OAuth：先做 spike（8.2 节），确认复用 SDK 的哪些工具函数；再建 `mcp_oauth_clients`、`mcp_oauth_tokens` 两张表和迁移；实现 `McpTokenAuth`、行锁刷新、授权的两个半程、`auth_required` fault，以及 OAuth server 的快照回填 | 用 ASGI 假授权服务器跑通授权码 + PKCE、刷新、并发刷新，以及撤销后重新授权 |
| M4（后续） | MRTR / elicitation 接入 ask 挂起；订阅 list_changed；图片进模型；resources / prompts；审批时允许修改 MCP 工具的参数 | 单独立项 |

每个阶段单独提交。实现中细化出的签名回写到本文。

## 14. 决策记录与待定事项

**已拍板**：

| 日期 | 事项 | 结论 | 落在哪里 |
|---|---|---|---|
| 2026-11-11 | 缺少 OAuth 授权时 | 只提示用户去授权，不挂起回合 | 8.3 节 |
| 2026-11-11 | 模型服务的 `api_key` | 保持明文存储 | 7.1 节 |
| 2026-11-11 | 成员自己的 server | 允许配置只有本人可见的私有 server | 7.2 节 |
| 2026-11-11 | 调用记录中的入参 | 原样写入 `tool_calls`，不做脱敏 | 10.3 节 |
| 2026-11-11 | 内置工具的 `always_scope=global` | 保持现状，只对 MCP 工具加管理员限制 | 9.1 节 |
| 2026-11-12 | MCP 凭证 | 不加密，明文存储，单独成表，接口掩码，不进日志 | 7.1 节 |
| 2026-11-12 | 渐进式工具发现 | 本期不做，工具数量靠 server 绑定和工具过滤控制 | 第 1 节非目标 |

**待定**：

1. **server_key 长度上限 24**：`server_key` 是给 server 起的短标识（如 `github`），会成为工具名的中段
   `mcp__github__create_issue`。工具名总长不能超过 64 个字符（OpenAI 的上限），前缀 `mcp__{key}__` 占得越多，
   留给原始工具名的就越少。24 个字符时前缀最多 31 个字符，原始工具名还能用 33 个字符，超出的部分截短再加哈希后缀。
   放宽到 32，原始工具名只剩 25 个字符，截短会更常见。没有另行决定就按 24 实现。

## 15. 与上位文档的偏离

以下几处现有文档和注释对 MCP 做了预设，本文的设计推翻了这些预设。M0 的第一个提交要同步修改这些地方，
修改完成之前以本文为准：

| 位置 | 原来的预设 | 本文的设计 | 何时改 |
|---|---|---|---|
| CLAUDE.md、AGENTS.md「Agent 核心（harness）约定」第 3 条（已有端口，`:58-61`） | 端口清单里没有作用域来源 | 补上 `ScopedToolProvider`（ports.py）；M1 再补 `McpServerConnector`、`McpServerRegistry`（mcp_port.py） | M0 / M1 |
| 同上第 4 条（扩展点） | 接 MCP 时实现 `ToolProvider`，并在 `platform_tool_set()` 里追加 | 实现 `ScopedToolProvider`，经 `Harness.scoped_tools` 注入；`platform_tool_set()` 只放内置工具 | M0 |
| 同上第 9 条（导入禁令，`:73-76`） | 只禁 anthropic / openai | 补上：`mcp` 只能在 `harness/adapters/mcp` 导入；runtime、app、domain 不得导入 `mcp_server` | M1 |
| 同上第 10 条（`:77-79`） | 只有工具名的 feature 域经 `platform_tools()` 查策略 | 收窄为只用于管理场景；运行时与审核链一律查本回合的 `policies` | M0 |
| 同上第 11 条（`:80`）、README.md:122 | 七份黄金测试 | 八份（新增 `test_mcp_tool_golden.py`） | M1 |
| `docs/agent-architecture.md:23`（组装根一段） | `Harness` 字段清单里没有作用域来源；「以后 MCP 连接管理器需要关闭钩子时再接入 lifespan」 | 补上 `scoped_tools`、`mcp_connector`；`mcp_runtime()` 惰性创建，由 lifespan 关闭 | M0 / M1 |
| `docs/agent-architecture.md:68`（ToolProvider 一行） | 接 MCP 时扩展为 `async list_tools(scope)` | ToolProvider 保持同步，另设 `ScopedToolProvider`（4.1 节） | M0 |
| `docs/agent-architecture.md:72`（SubAgentLauncher 一行） | 签名里没有作用域工具 | 增加 `scoped: ScopedGrant \| None` | M0 |
| `docs/agent-architecture.md:77`（ToolSet 一段） | 接 MCP 时只需在 `platform_tool_set()` 里追加 | 同上；运行时按名查策略改为查 `policies` | M0 |
| `docs/agent-architecture.md:96-102`（第 5 节 MCP） | 未标注的工具一律需要审批；连接管理器在 lifespan 里创建；ToolResult 和 tools 表待定 | 改为指向本文：server 级三档审批（3.2 节）；惰性创建、lifespan 关闭（10.2 节）；结果转成文本、不进 tools 表（5.5、6.2 节） | M0 |
| `harness/app/ports.py:294-296`（ToolProvider 的 docstring） | 接 MCP 时签名改为 `async list_tools(scope)` | 改为指向 `ScopedToolProvider` | M0 |
| `harness/app/tool_set.py:6-8`（模块说明） | 平台工具集即完整工具集 | 说明平台工具集只含进程级工具，回合的策略全集见 `ResolvedScope.policies` | M0 |
| `harness/bootstrap.py:48`（`platform_tool_set` 的 docstring） | 接 MCP 时在这里追加工具来源 | 改为「只放进程级工具；作用域来源见 `Harness.scoped_tools`」 | M0 |
| README.md:133-142（「扩展」一节） | 同 CLAUDE.md 第 4 条；设计链接指向 agent-architecture.md 第 5 节 | 同上；链接改为指向本文 | M0 |

## 附录 A：调研出处

调研报告不进版本库，以下是关键出处（仓库 + 文件），实现时可以回查：

| 设计点 | 出处 |
|---|---|
| 渐进式工具发现（本期不做） | MCP 开发指南 `docs/docs/2026-07-28/develop/clients/client-best-practices.mdx`；Anthropic「Tool search tool」；OpenAI「Tool search」（只支持 Responses API）；pydantic-ai `toolsets/_tool_search.py`；codex `mcp_tool_exposure.rs:158-162` |
| 规范 2026-07-28：无会话、MRTR、ttlMs、注解默认值、工具名约束 | `modelcontextprotocol/modelcontextprotocol` `docs/specification/2026-07-28/`（changelog、server/tools、basic/authorization） |
| SDK v2 API：`Client`、`streamable_http_client`、`TokenStorage`、`OAuthClientProvider`、`allow_input_required` | `modelcontextprotocol/python-sdk` `src/mcp/client/client.py`、`streamable_http.py:713`、`auth/oauth2.py:130-148, 292-309` |
| 确定性命名 + 哈希后缀 | `openai/codex` `codex-rs/codex-mcp/src/tools.rs:122, 274-325`；`openai/openai-agents-python` `src/agents/mcp/util.py:63, 436-443` |
| 注解 → 默认审批 | `openai/codex` `codex-rs/core/src/mcp_tool_call.rs:2746-2763` |
| 专属 task 托管连接、调用与连接赛跑 | `PrefectHQ/fastmcp` `client/client.py:1259-1333`；`google/adk-python` `tools/mcp_tool/session_context.py`；HugAgentOS `core/llm/mcp/session.py:112-151` |
| 按身份分池、空闲回收 | `google/adk-python` `mcp_session_manager.py:975-1000, 1135-1192` |
| 目录刷新失败保留旧目录 | `openai/codex` `codex-rs/rmcp-client/src/tool_list_changes.rs` |
| 写操作超时不重放、人工等待不计时、run 内冻结 schema | HugAgentOS `core/llm/mcp/deadline.py`、`client.py:121-133` |
| AsyncExitStack 全部成功才提交 | `pydantic/pydantic-ai` `pydantic_ai_slim/pydantic_ai/mcp.py:1384-1461` |
| SSRF：全地址校验、固定 IP、不跟随重定向 | `IBM/mcp-context-forge` `gateway_service.py` `_validate_url` |
| OAuth 回调绑定会话、校验 resource、请求头占位符窃取 token | LibreChat GHSA-vf7j、GHSA-gvpj、GHSA-pmw7 |
| 多副本并发刷新导致授权被吊销、重连风暴 | LibreChat issue #15977、#12802 |
| 按用户授权的 server 被误判离线 | mcp-context-forge issue #4495 |
| 硬删工具导致自定义信息丢失 | mcp-context-forge `_reconcile_gateway_catalog` |
| stdio 在多租户下的结构性风险 | Flowise 的十余条 GHSA（`packages/components/nodes/tools/MCP/core.ts`） |

## 附录 B：与各参考项目的取舍

| 主题 | 采用 | 不采用 |
|---|---|---|
| 工具名 | codex 的清洗 + 哈希 + 按原名排序；openai-agents 的 64 字符上限 | HugAgentOS 原名透传；单下划线拼接（langchain、ADK） |
| 连接 | 按 (server, 配置版本, 授权分区) 分池，专属 task 托管 | langchain 每次调用新建；跨用户共享连接 |
| 发现 | 按回合解析 + SDK 按授权分区的响应缓存 + 刷新失败保留旧清单 | 落库快照作为运行时依据（Dify 的工具变化不实时）；每轮都 list 且不缓存（Open WebUI） |
| 工具数量 | server 绑定 + `tool_allowlist` / `tool_denylist` | 渐进式发现（MCP 开发指南的 `search_tools`、Anthropic tool search），本期不做 |
| 审批 | 默认全部审批；管理员显式选择后才信任注解；工作区权限规则可以覆盖 | 无条件信任注解；MCP 写工具一律放行（HugAgentOS） |
| 凭证 | 单独成表；接口掩码；不进日志 | 加密存储（Dify 每租户 RSA、mcp-context-forge AES-GCM），本期不做 |
| 传输 | 只支持 Streamable HTTP | 对用户开放 stdio（Flowise）；旧的 HTTP+SSE |
| OAuth | 协议细节复用 SDK 的工具函数；token 生命周期由 AgentOS 掌握；前端带 Bearer 完成后半程；持锁刷新；提前刷新加抖动 | 把调用路径整个交给 SDK provider（读库 token 不刷新、不跨实例）；进程内轮询等回调（HugAgentOS）；无分布式刷新锁（Dify、Open WebUI） |
| 管理 | 先探测成功再写库 | 先写库再异步探测 |

