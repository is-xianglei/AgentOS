# 设计：插件化架构（微内核 + 能力 seam + 声明式组合树）

> ⛔ **已作废（2026-08-15）**：本任务经用户决定作废，从未进入实施阶段。
> 归档时 `status` 被 `task.py archive` 强制写为 `completed`，那是脚本行为，
> 不代表完成。**本文不作为任何实施依据**，详见同目录 `prd.md` 顶部横幅。

范围：L0–L3 全量（决策 D2）。本文只写技术设计，需求与验收在 `prd.md`，一手证据在 `research/`。

## 一、总体形状

```
组合层  cordis.yml + 分层 patch  ←  运维/租户在这里重组，不改代码
   ↓  Loader（本身也是插件）
内核    kernel/  Context · Fiber · Service · Events · Effect     ← 对 Agent 一无所知
   ↓  provide / inject / on
能力层  每个 seam = Definition 包 + N 个 Provider 包 + N 个 Consumer 包
   ↓  事件词汇（agent 包拥有）
循环    agent_loop 包 —— 唯一实现，任何包不得依赖它
```

四条不可违背的边界：

1. **内核不认识 Agent**。`kernel/` 里不得出现 session、tool、llm、prompt 等任何业务词。
2. **消费方只依赖 Definition**，绝不 import 具体 provider —— 由 import-linter 在 CI 强制（R-S-2）。
3. **循环没有反向依赖**。除组合文件外，任何模块不得 import `agent_loop`（A2 用 grep 验收）。
4. **一切注册都是 effect**，插件卸载即撤销。没有「注册了但撤不掉」的东西。

## 二、L0 内核设计

参照 `research/cordis-kernel-spec.md` 的七条公理逐条落地。Cordis 是 2693 行 TS，
本内核预期 **2000–3000** 行 Python（Pydantic v2 顶掉 schemastery，省掉一整层；
原估 1500–2500 已按原型实证上修，理由见第十三节风险 10）。

### 2.1 Context

```python
class Context:
    def __getattr__(self, name: str) -> Any: ...        # 服务解析，缺失抛 ServiceNotProvided
    def extend(self, **attrs: Any) -> "Context": ...
    def isolate(self, name: str, label: object | None = None) -> "Context": ...
    def intercept(self, name: str, config: dict[str, Any]) -> "Context": ...

    def provide(self, name: str, value: Any) -> Disposer: ...
    def plugin(self, plugin: PluginLike, config: Any = None) -> "Fiber": ...   # 可 await
    def effect(self, execute: Callable[[], Disposer | Awaitable[Disposer]]) -> Disposer: ...
    def on(self, event: str, listener: Callable, *, prepend: bool = False) -> Disposer: ...

    def emit(self, *args) -> None
    async def parallel(self, *args) -> None
    async def serial(self, *args) -> Any
    def bail(self, *args) -> Any
    async def waterfall(self, *args) -> Any
    def get[T: Service](self, definition: type[T]) -> T | None   # 软依赖用
    def require[T: Service](self, definition: type[T]) -> T       # 类型化取服务；另有 str 重载，见 2.3
```

`Proxy` → `__getattr__`；原型链 → 显式 `parent` 指针逐级查找（层数是插件树深度，个位数，无需 ChainMap）；
`Symbol(name)` → `object()` 哨兵实例做隔离标签，按身份比较，不可伪造。

### 2.2 最重要的一处刻意偏离：不复刻 `getTraceable`

Cordis 用透明代理让「服务方法内部调 `this.ctx.on()` 时，副作用记到**调用方**的 fiber 上」
（`utils.ts:117-233`）。Python 里复刻它需要一整套 `__getattribute__` 代理，性能与可调试性代价都很高。
**不做透明代理**这一条维持不变。

剩下的问题只是：调用方身份怎么传到注册 API 手里。

**已推翻的原方案（纯显式参数）。** 原先写的是「所有注册类 API 显式收调用方 ctx」
（`ctx.tools.register(ctx, bash_tool)`），理由是「谁注册的在签名上显式，不靠隐式魔法」，
并用 `Service` 基类的 `_own_effect` / `_caller_effect` 辅助方法「让误用在阅读时刺眼」。
**原型实测证伪了这个卖点**（`research/kernel-python-prototype.md` Q4）：

| 误用 | 实测结果 | 辅助方法/静态检查能否拦住 |
|---|---|---|
| ① 服务作者顺手用 `self.ctx` 登记 | **运行时零报错、静默泄漏**（注册挂到 provider 的 fiber，consumer 卸载不撤销） | 挡不住 —— 作者本来就选错了方法 |
| ② 调用方把服务自己的 ctx 递回去（`registry.register(registry.ctx, ...)`，因为 `self.ctx` 就在手边，是最顺手的错误） | **同样静默泄漏** | 挡不住 —— 辅助方法不在调用方那一侧；「静态检查首参必须是 `caller: Context`」也无效，因为递错的**也是** `Context` 类型，类型检查全绿 |

也就是说，原方案唯一的运行时兜底其实是 A1⑤ 的计数归零断言，而不是签名。

**改用：contextvars 缺省 + 显式参数覆盖。**

```python
class ToolRegistry(Service):
    def register(self, tool: Tool, *, ctx: Context | None = None) -> Disposer:
        caller = ctx or current_ctx.get()      # 缺省取当前插件上下文；断链场景显式传
        def install() -> Disposer:
            self._tools[tool.name] = tool
            return lambda: self._tools.pop(tool.name, None)
        return caller.effect(install)
```

内核承接两条义务：**插件 `apply` 前 `current_ctx.set(plugin_ctx)`、`finally` reset**；
**总线分发前按监听器注册时捕获的 owner 重绑**（约 6 行，插件无感）。

五条传播路径的实测结果：

| 路径 | contextvars | 显式参数 |
|---|---|---|
| (a) `apply` 内直接注册 | 通过，且少一个参数 | 通过（可能误用②） |
| (b) `apply` 里 spawn 的后台任务，reset 之后才注册 | 通过（`create_task` 创建时拷贝上下文） | 通过 |
| (c) 事件监听器内注册 | 通过（靠上述 owner 重绑；naive 分发会挂错到发射方） | 通过 |
| (d) 常驻 worker + 队列边界 | **失败**，须入队捕获 + 出队恢复 | **同样失败**，须把 ctx 塞进消息 |
| (e) 生命周期外裸调 | 抛 `LookupError`，**失败很响** | **静默挂错**，更差 |

结论：contextvars 在 (a)(b)(c)(e) 四条路径上**结构性消除**了误用面，(d) 与显式方案同责。
代价诚实地说是「归属不在签名里」；但原方案的签名**也没能保证归属正确**，
所以这不是「显式换隐式」，而是「假的显式换真的默认正确」。
保留 `ctx=` 覆盖参数，让队列接力这类断链场景仍有显式出口。

于是第十三节风险 4 从「整体靠纪律」降级为「**仅队列边界靠纪律**」，
A1⑤ 的计数归零断言照常作为最终兜底 —— 它是这类误用的**唯一**运行时兜底，
无论采用哪个方案。

（描述符方案已判定为死路，未做原型：描述符只知宿主实例、拿不到调用方身份，
除非 `sys._getframe` 栈检查 —— 脆弱且慢。）

### 2.3 类型化服务查找（替代 TS 的 `declare module`）

TS 靠声明合并把 `ctx.shell` 变成有类型的属性。Python 没有等价物，但有更好的做法：

```python
# 定义包 shell/definition.py
class ShellService(Service, ABC):
    SERVICE_NAME = "shell"
    @abstractmethod
    async def start(self, request: ShellRequest) -> ShellHandle: ...

# 消费方
inject = [ShellService]                        # 内核读 .SERVICE_NAME 取名字
shell = ctx.require(ShellService)              # 静态类型直接来自定义包，无需 .pyi
```

`inject` 同时接受字符串与 Definition 类。用 Definition 类的好处：消费方对 Definition 包产生
**显式的、可被 import-linter 检查的**依赖，而对 provider 包零依赖 —— 正是 R-S-2 要的形状。

实测签名（mypy 2.3.1 / pyright 1.1.411 均按预期推导，含 `get` 的 `T | None`，无需 `.pyi`）：

```python
class Context:
    @overload
    def require[T: Service](self, definition: type[T]) -> T: ...
    @overload
    def require(self, definition: str) -> Any: ...
    def require(self, definition): ...          # 运行时读 SERVICE_NAME / 字符串查槽
    def get[T: Service](self, definition: type[T]) -> T | None: ...
```

**必须在 P1 定下类型检查器**（项目当前 `pyproject.toml` 未配任何检查器）：
mypy 对 `require(type[T])` 传 ABC 会报 `type-abstract`（推导本身正确，是 DI 场景的通行摩擦点，
python/mypy#4717 一系），pyright 1.1.411 零告警。三条路：
① 选 pyright，上面写法原样可用（最省）；
② 选 mypy，配 `disable_error_code = ["type-abstract"]`（粒度是全项目）；
③ Definition 类弃用 `@abstractmethod`、方法体 `raise NotImplementedError` ——
两个检查器都干净，代价是丢掉「provider 少实现方法时实例化即报错」的 ABC 拦截。
泛型令牌 `Definition[T]` 虽零告警，但引入平行对象且与 `inject = [ShellService]` 的既定形状冲突，
不采用。

### 2.4 Fiber 与 epoch

状态机 `PENDING → LOADING → ACTIVE → FAILED / UNLOADING → DISPOSED`。
每次 `ctx.plugin()` 无条件新建一个 Fiber（多实例是默认语义）；单例性由服务名占用保证 ——
同隔离域重复 provide 同名服务直接抛错，与 Cordis 一致。

epoch 计算逐字照搬 `fiber.ts:611-623` 的语义：

```python
def _compute_epoch(self) -> str | None:
    epoch = ""
    for name in self.inject:
        impl = self._store.get(name)
        if impl is None:
            return None                      # INACTIVE
        epoch += f":{impl.fiber.uid}"        # 依赖“提供方实例代”，不只是名字
    return epoch
```

epoch 变化单向驱动状态机。**provider 换代（同名新实例）也必须触发 consumer 重载** —— 这是 A1③ 的验收点，
也是最容易在实现时漏掉的一条。

加载期做依赖快照 `self.store = dict(self._store)`，保证插件活动期内看到的依赖实例恒定。

**抖动抑制需要四件套，不止 `call_soon`**（原型实测，见
`research/kernel-python-prototype.md` Q2）。`loop.call_soon` **本身不做任何合并**，
它只提供延迟；真正合并靠：

1. `call_soon` 延迟到本轮循环末尾；
2. `_scheduled` 标志防止同轮重复排程；
3. flush 时做 **epoch 对账**（比较当前算出的 epoch 与活动时的快照，相同则什么都不做）；
4. **单飞脏位 reconcile** —— 重载窗口内又发生换代时置脏，由 `while` 下一圈吸收。

第 4 件原方案没写，是**防丢更新的必要件**：重载是 `await` 过程，期间到达的换代通知
若没有脏位承接就会被吞掉，consumer 停在旧代上。

实测边界：合并范围 = 同一事件循环轮次。同轮 3 连换 → **1 次重载**；
同轮「拔掉又原样插回」净零变化 → **0 次重载**（抖动被吸收）；
跨 `await` 的 3 连换 → 2 次重载（属预期，不是缺陷）。这三条是 A1③ 的量化形态。

### 2.5 effect 与卸载顺序

```python
Disposer = Callable[[], None] | Callable[[], Awaitable[None]]
```

卸载语义必须精确复刻 Cordis：**顶层 effect 之间并发拆除，单个 effect 内部逆序串行**
（原型实测 0.096s vs 纯串行 0.15s）。
每个 fiber 持有一个 effect 列表，卸载时 `asyncio.gather(*[e.dispose() for e in effects])`，
而单个 effect 内部登记的子 disposer 按逆序 `await` 串行执行。
有严格顺序要求的拆除必须放进同一个 effect —— 这条要写进内核文档。

两条禁用（均为实测结论）：

- **`contextlib.AsyncExitStack` 不能用**，理由精确为**无并发**这一条
  （它的错误延续性本身是合格的，原方案把这条也算作理由是不准确的）。
- **`asyncio.TaskGroup` 不能用于拆除**：实测一个 disposer 失败会取消兄弟任务，
  **导致其余 disposer 根本不执行**，与「卸载必须撤销全部注册」直接冲突。

**错误策略（原方案缺失，必须补）**。读 Cordis 源码确认其真实语义是两层：

- **同一 effect 内 fail-fast**：某个 disposer 抛错则跳过该链剩余 disposer；
- **fiber 级逐 effect 隔离**：收集异常、`logger.error` 记录、**不上抛**。

不上抛是有原因的：卸载常由 epoch 变化驱动，此时**没有自然的调用方能接这个异常**。
本设计照搬：默认记日志；仅显式 `await fiber.dispose()` 提供聚合成 `ExceptionGroup` 的变体。

因此 `research/cordis-kernel-spec.md` 公理 5 里「逆序执行**全部** disposer」的表述需同步修正 ——
同 effect 内有失败时它不成立。

**未决事项**：teardown 自身被取消（`CancelledError`）如何处置（`shield` 还是约定），
本次未验证，留到 P1 实现期决策。

### 2.6 事件总线

五种分发（R-K-5）。Python 签名：

| 方法 | 语义 | 备注 |
|---|---|---|
| `emit(*args)` | 同步广播，忽略返回值 | 监听器异常记日志不冒泡 |
| `await parallel(*args)` | `asyncio.gather(return_exceptions=True)` | 失败聚合为 `ExceptionGroup` |
| `await serial(*args)` | 依次 await，首个非 `None`/`False` 胜出并停止 | |
| `bail(*args)` | serial 的同步版 | 只能挂同步监听器 |
| `await waterfall(*args, next)` | 环绕中间件；**不调 `next()` 即短路否决** | 策略层靠它 |

三个实现决定（原方案未写，原型实测后必须定死）：

1. **`emit` 对异步监听器的策略**：fire-and-forget 必须**持强引用防 GC**
   （`asyncio` 只持弱引用，任务可能被回收）；热路径事件（如 `assistant/chunk`）
   建议**直接禁止挂异步监听器**，避免不可见的并发。
2. **`bail` 运行时拒绝异步监听器**：抛 `TypeError`，并 `coroutine.close()` 防
   "coroutine was never awaited" 告警。
3. **`serial` / `bail` 的胜出条件精确为「非 `None` 且非 `False`」** ——
   即 `0` 和 `""` 算胜出。这条要写进词汇文档，否则实现者会顺手写成 truthy 判断。

另：`waterfall` 加 `next` 重复调用防呆（约 4 行）。

「失败聚合为 `ExceptionGroup`」**只适用于 `parallel` 分发路径**；
**拆除路径不聚合上抛**（见 2.5），两处不要混用同一句话。
`parallel` 的异常归属保真已实测：原始 `raise` 帧 + `add_note` 双通道，能定位到具体监听器。

边界不靠总线分区，靠**分发时首个对象参数携带的过滤谓词**。`Service` 默认谓词按隔离标签比较 ——
**隔离域即事件边界**，这是 R-T-1 租户隔离能成立的内核基础。（过滤谓词本次未做原型验证。）

Python 有 `ExceptionGroup`（3.11+），比 JS 的 AggregateError 更顺手，直接用。

## 三、插件形态与目录结构

### 3.1 两种形态

沿用 dsh 的分工（`research/capability-seams-and-patterns.md` 第四节）：
**提供服务的包用类插件，只消费/挂监听的包用函数插件。**

```python
# 函数插件：tool_bash/plugin.py —— 只消费
name = "tool-bash"
inject = [ToolRegistry, ShellService, SystemPrompt]
Config = BashConfig                                  # Pydantic BaseModel

async def apply(ctx: Context, config: BashConfig) -> None:
    ctx.tools.register(ctx, BashTool(config))
    ctx.system_prompt.section(ctx, "bash-usage", _render)

# 类插件：shell_local/plugin.py —— 提供服务
class LocalShell(ShellService):
    Config = LocalShellConfig
    def __init__(self, ctx: Context, config: LocalShellConfig) -> None:
        super().__init__(ctx)                        # 基类完成 provide("shell", self)
```

TS 那条「有 Config 的函数插件必须用 named export，default 导出会丢 schema」的坑在 Python 不存在
（模块级属性天然具名），但 Loader 仍按固定属性名 `name` / `inject` / `Config` / `apply` 读取，
缺失即报错，不做猜测。

### 3.2 目录结构：seam 一包，provider 一包

每个 seam 三角色分属独立顶层包，这样 import-linter 才有 R-S-2 需要的检查粒度：

```
shell/          Definition：ShellService(ABC) + 词汇类型 + 事件名常量
shell_local/    Provider
shell_sandbox/  Provider
tool_bash/      Consumer
```

包内仍保持项目既有的 feature 域五件套（`models.py` / `schemas.py` / `repository.py` /
`service.py` / `api.py`），新增一个 `plugin.py` 作为插件入口。**不新建按层划分的目录**，
符合 CLAUDE.md 的既有约定。

**代价与决策**：顶层包会从 22 个涨到约 60 个。备选是收进一个 `plugins/` 根命名空间，
但那更接近「按层划分」，与 CLAUDE.md 冲突。**推荐扁平顶层包**（与 dsh 的 `packages/` 同形，
且 import-linter 规则最直白）。这是对项目现有约定的一处扩充，需在最终评审时确认。

### 3.3 依赖规则的机器强制

用 import-linter 表达，**契约类型已按其实际表达力校准**（实测见
`research/toolchain-feasibility.md`；`*_local` 这类部分名通配符官方不支持，
实测报错「A wildcard can only replace a whole module」，因此下面不用通配写法）：

1. **禁止依赖 provider** —— **每个 provider 顶层包一条 `protected` 契约**
   （白名单语义，2.5 版本新增）：
   ```toml
   [[tool.importlinter.contracts]]
   name = "shell_local 只允许组合层与自身测试导入"
   type = "protected"
   protected_modules = ["shell_local"]
   allowed_importers = ["composition", "tests.shell_local"]
   ```
   代价是**新增 provider 必须同步登记一条契约**，要么进评审 checklist，要么写个目录 diff 脚本兜底。
2. **禁止依赖循环实现**：`protected_modules = ["agent_loop"]`（A2 的机器化验收）。
   若组合根也纯动态加载、不 import，`allowed_importers = []` 是合法写法。
3. **内核不得依赖业务**：拆两半表达。内部依赖用 `forbidden`（`source_modules = ["kernel"]`，
   `forbidden_modules` 穷举其余顶层包，已实测）；**外部依赖白名单内置契约表达不了**，
   二选一：(a) 黑名单枚举重点外部包（fastapi / sqlalchemy / anthropic 等，务实但不完备）；
   (b) 写自定义契约做精确白名单（约 40–80 行，报告里有骨架，**未实测运行**）。
   **推荐先走 (a)**，等 `kernel/` 稳定再考虑 (b)。

配套决策（实现期必须定，别拖到出问题）：`tests/` 是否纳入导入图
（要补 `__init__.py` 并列入 `root_packages`，否则上面的 `tests.shell_local` 例外无效）；
`uv add --dev "import-linter>=2.5.1"`（`protected` 契约 + Python 3.14 支持的最低版本）；
`.import_linter_cache` 加进 `.gitignore`。

横向依赖不一刀切禁止 —— provider 复用兄弟 provider（如 `shell_sandbox → shell_local`）是允许的，
与 dsh 一致，在被复用方的 `allowed_importers` 里显式列出即可；但方向必须向 Definition 收敛。

## 四、seam 目录：现有代码到三角色的映射

R-S-6 首批 **18 个** seam（原 18 个 **+ `api`**，再 **− 已并入 `jobs` 的 `schedule`**；
`eventTransport` 并入 `sessionLog` 的读侧职责，`schedule` 降级见表下说明）。「机制」列区分两种多实现方式（判据见
`research/capability-seams-and-patterns.md` 第三节）：**槽**＝独占服务槽，换实现改组合文件一行；
**表**＝注册表按 key 路由，多实现共存。

| seam | 机制 | Definition 来源 | 首批 Provider | 关键改造动作 |
|---|---|---|---|---|
| `llm` | 表 | `llm/base.py` 的 `BaseLLMClient` 升格为 `LlmRuntime`(注册表) + `LlmAdapter`(ABC) | anthropic、openai | 修复 7 处断裂 import；`max_tokens` 从硬编码改配置；`complete_structured` 进 Adapter 契约 |
| `tools` | 表 | `tools/registry.py` 的 `ToolRegistry` | 26 个内建工具按**耦合组**拆成约 10 个 `tool_*` 包 | ① `to_param()` 不再产 `anthropic.ToolParam`（仅 3 文件 6 引用，成本低）；② 工具返回值从裸 `str` 改结构化结果；③ **DB 工具目录真源重定义**（见表下「工具目录静默误删」）；④ 6 处硬编码工具名集合迁移归属；⑤ 交互 4 工具与 `approval` **成组交付** |
| `sessionLog` | 槽 | **全新**：`append()` / `derive_messages()` + **读侧扇出**（订阅/续传） | PostgreSQL(+in-process 扇出)、PostgreSQL+Redis Streams 扇出 | 事件溯源真源，R-S-5 的载体；扇出层不另设 seam，作为本 seam 的 provider 维度（见 5.8） |
| `sessionPersistence` | 槽 | **全新**：`prepare(id)` / `save()` | PostgreSQL | 从 `session/service.py:432-466` 的快照+水位逻辑迁入 |
| `systemPrompt` | 槽 | `prompt/compose.py` 的固定管线升格 | 本体 | 提供 `section`/`context`/`variable`/`tools` 四种注册 + `assemble` waterfall |
| `approval` | 槽 | **全新**：`approval/request` waterfall | `interaction` 域现有人工交互 | 权限裁决从循环里搬出（`agent.py:903-930`）；**无应答方时 ask 落为 deny**；连带拆迁 `interaction` 域（1157 行）与交互 4 工具 |
| `compaction` | 槽 | `runtime/compact.py` 升格 | basic | 从循环体内搬出，改挂**三处**：`agent/pre-step`(压力) + `agent/request-error`(请求期 400 溢出) + 成功路径 `stop_reason=model_context_window_exceeded`(生成期溢出) |
| `skills` | 表 | `skill/` 域升格为 `SkillRegistry` + `SkillProvider` | object-storage | 保留唯一的运行时上传通道语义 |
| `subagents` | 表 | `tools/subagents/` 升格 | spawn-in-process | **打破 `AgentType` 封闭 enum**；SPECS 不再 import 期冻结 |
| `storage` | 表 | 现有 `ObjectStorage` ABC 直接升格 | minio、local | 消灭 `_executor_instance` 式单例工厂 |
| `subprocess` | 槽 | **全新** | local | 与 `fs` 共享执行世界，为将来整体切远程沙箱留形状 |
| `shell` | 槽 | `tools/shell_policy.py` 的策略部分升格 | bash-local、bash-sandbox | 静态白名单降级为 `sandboxPolicy` 的一种策略，不再是安全边界本身 |
| `fs` | 槽 | **全新** | local、sandbox | 写入前过 `fs/write-intent` waterfall，让观测/否决插件可挂 |
| `sandbox` | 槽 | **全新**：单方法 `confine(argv, policy)` | macOS Seatbelt / Linux bwrap | 见第十节 |
| `jobs` | 槽 | Definition 全新，**Provider 从 `memory/jobs/repository.py:227-315` 泛化** | postgres-queue | 现有队列已具 SKIP LOCKED + 租约 + 退避 + 幂等，是成熟承载者，不是从零造；同批要定 worker 进程如何装配（现由 memory 域自持） |
| ~~`schedule`~~ | — | **已并入 `jobs`**（决策 D12） | — | `after`/`at`/`every`，**不是 cron**；到期只 `followup()`，绝不打断当前轮次。**全仓零承载者零消费者**，故不占首批名额，见表下说明 |
| `memory` | 槽 | `memory/` 域（6752 行，最大）升格 | 现有实现 | **不止「改依赖获取方式」**：3 处跨域直连 session（`memory/jobs/extraction.py:20,218`、`memory/recall/repository.py:283`、`memory/repository.py:728,811`）在 P4 切真源时全部要改；业务规则仍不动 |
| `hooks` | 表 | `hooks/` 的 `HookRegistry` 升格 | 本体 | 去全局单例；4 个事件点并入统一事件总线，补优先级与卸载 |
| `api` | 槽 | **全新**：`ctx.http.mount(caller, router, prefix)` | fastapi | 消灭 `api/router.py:3-38` 静态聚合；Consumer = 15 个域的 `api.py`；卸载语义与 OpenAPI 缓存失效见 6.4 |

**三处重活，不是两处**（依据 `research/remaining-domains-assessment.md`）：

1. **`tools`**：3134 行 + 26 个工具按耦合组拆约 10 包（第五节之外最大工作量）。
2. **循环**：第五节全部内容。
3. **`session` 域**：两个新 seam（`sessionLog` + `sessionPersistence`）、
   `_SESSION_CHILD_MODELS` 级联机制改造、`interaction` 域吸收 —— 全压在它一个域上。

原先「`memory` 是最大的域但改造动作最轻」的判断**不成立**，已按上表改写：
memory 有 3 处直接读写 session 表的跨域连接，P4 切真源时必须一起改。

**`schedule` 的降级决定（已定案 D12）**：全仓无任何调度器实现、无任何调用方，
是纯新增能力而非改造。它与「一切皆插件」的论证无关（不存在「现有能力被 seam 化」这回事），
且唯一排队的消费者是尚未实现的「B2 后台自动唤醒」（`runtime/agent.py:1669` 自述）。
**并入 `jobs`**（延时投递即 `after`/`at` 的最小形态），`every` 语义待真实需求出现再展开。
首批 seam 计数因此为 **18 个**（表内 19 行中 `schedule` 一行已划掉）。

**工具目录静默误删（新增风险，此前未记录）**：`tools/service.py:34-45` 的
`sync_builtins()` 在启动时用「当前注册表全集」调 `mark_missing_deleted()`，
把不在集合里的目录行软删。插件化后**任一插件未挂载 ⇒ 它的工具目录行被软删 ⇒
已绑定该工具的 Agent 报 409**。这不是重构副作用，是真源关系变了：
目录的真源从「进程内建表」变成「当前组合树」。P3-3 必须显式重定义这层关系
（候选：只对本次实际挂载的插件做差集、或目录行改为「组合无关的声明式清单」）。
`tools/subagents/trigger.py:15` 已存在同类幽灵名（引用不在注册表里的工具名），
说明该风险在插件化之前就已具备现实形态。

## 五、循环重构：从 1712 行上帝类到「事件词汇 + 唯一实现」

### 5.1 事件词汇（由 `agent` Definition 包拥有，R-S-4）

对齐 dsh 的 `architecture.zh.md:71-86`，按 AgentOS 实际需要增删。**分发模式是词汇的一部分**，
定了就不能改 —— 改分发模式等于改契约。

| 事件 | 分发 | 语义 | 谁会挂 |
|---|---|---|---|
| `turn/start` | parallel | 一轮用户输入开始 | 遥测、turn 落库 |
| `agent/pre-step` | **waterfall** | 每步前的可否决/可改写关口 | **压缩**（上下文压力）、**plan-mode** |
| `step/start` | parallel | 单步开始 | 遥测 |
| `system-prompt/assemble` | **waterfall** | 组装系统提示词 | 各能力包贡献段落 |
| `agent/request` | **waterfall** | 即将发出的模型请求 | 请求改写、录制回放 |
| `llm/stream` | **waterfall** | 流式调用本身 | 测试用 Fake 整层替换 |
| `assistant/chunk` | emit | 流式增量（**仅供 UI 渲染，不作落库真源**） | SSE 推送 |
| `llm/response` | parallel | **每次模型调用完成后必发**（新增，见 5.1.1） | usage 入日志、计费 |
| `tool/call` | parallel | 模型请求调用工具 | 日志落盘 |
| `approval/request` | **waterfall** | 权限裁决 | permission 域；**无应答方 → deny** |
| `tools/pre-execute` | **waterfall** | 执行前否决/改写入参 | plan-mode 白名单、PreToolUse hook |
| `tools/execute` | serial | 实际执行 | tools seam |
| `tools/post-execute` | parallel | 执行后观测与副作用 | PostToolUse hook、各工具自己的状态迁移 |
| `tool/result` | parallel | 结果产生 | 日志落盘、SSE |
| `step/end` | parallel | 单步结束 | 遥测 |
| `agent/turn-stopping` | **serial** | 是否续跑（首个非空返回续跑指令） | stop hook、多轮目标续跑 |
| `turn/end` | parallel | 一轮结束 | **队友唤醒**、记忆抽取、turn 收尾 |
| `agent/request-error` | **waterfall** | 供应商返回错误 | **压缩**（确认溢出后重试） |

### 5.1.1 三条词汇细则（依据 `research/anthropic-sdk-adaptation.md`）

**① 为什么必须新增 `llm/response`。** 原方案把 usage 挂在 `agent/request` 上，
但 waterfall 是「请求发出前」的改写关口，而 usage 在流耗尽后才产生 —— 时机对不上。
新增每次调用必发的响应事件，载荷 =
`{model, stop_reason, usage{input, output, cache_creation, cache_read}, request_id}`，
由它入日志；`agent/request` 保持纯请求侧。`turn/end` 的 usage 语义随之改为
「本轮各 `llm/response` 之和」。SDK 侧采集点是 `message_final`
（= `message_start` + 末次 `message_delta` 的累积，四个 usage 字段齐全）。
现状丢失 per-call usage 的原因不是 SDK 拿不到，而是 translator 的 `_on_message_final`
每次覆盖同一份缓存。

**② `llm/stream` 采用小契约。** waterfall 包装 `AsyncIterator` 在 Python 下可行，
但要求每层都做对三件事：必须 `contextlib.aclosing`、迭代器单次消费禁止 tee、
中断后没有 `message_final` 等价帧因而消费方须容忍缺失。
本设计选**小契约**：中间件只许改 options 或**不调 `next` 整层替换**，
流中观测一律走 `assistant/chunk`(emit)。测试 Fake 用「整层替换」这一路，能力不减。

**③ `agent/request-error` 的三条语义。**
(a) 错误可能发生在**已产出若干 chunk 之后**（流中 SSE error 从 `async for` 内抛出，
且 SDK 不重试流中错误）—— 故规定「chunk 仅供渲染，权威内容以最终事件为准」，
重试即前端整块重绘；
(b) **溢出识别判据下沉 provider**：anthropic 靠 `BadRequestError` + **消息串匹配**
（无稳定错误码，这是已知脆弱点），openai 靠 `code == context_length_exceeded`；
事件载荷只携带中立错误类别（`context_overflow` / `rate_limited` / `overloaded` /
`transient` / `fatal`），**循环与压缩插件不做任何字符串匹配**；
(c) 流中错误抛出的是 `status_code=200` 的基类 `APIStatusError`，只能靠 `.type` 分类，
该翻译同属 provider 职责。

### 5.2 十处硬编码依赖的落点

`research/agentos-baseline.md` 第二节列的每一处，都必须有明确去向 —— 这是 R-S-3 的核对表。

| 现状 | 位置 | 新落点 |
|---|---|---|
| `maybe_compact` 焊在循环体 | `agent.py:558` | compaction 插件挂 `agent/pre-step` + `agent/request-error` |
| `PLAN_MODE_ALLOWED_TOOLS` | `agent.py:63-75` | plan_mode 插件挂 `tools/pre-execute`，不匹配即不调 `next()` |
| `INTERACTION_TOOL_KINDS` | `agent.py:58-62` | 交互工具自行走 `approval/request`；**循环不再认识「交互工具」这个概念** |
| 工具名字符串特判 | `agent.py:800-901` | `Agent`/`EnterPlanMode`/`ExitPlanMode` 三个工具各自挂 `tools/post-execute` |
| 权限裁决 + `DANGEROUS_TOOLS` | `agent.py:41, 903, 1110-1114` | approval seam；permission 域作 provider，危险清单变配置 |
| `get_hook_registry()` 全局单例 | `agent.py:1624, 1650` | hooks 插件把 4 个事件点挂到总线；循环不再显式调用 |
| 队友唤醒（直 import 三个模块） | `agent.py:1673-1693` | team 插件挂 `turn/end` |
| `LLMClient()` 直接 new | `agent.py:113` | `inject = [LlmRuntime]` |
| `AnthropicStreamTranslator` | `agent.py:565` | 移进 `llm_anthropic` provider，循环只见中立事件 |
| `build_tool_registry()` ×5 处 | `agent.py:114,1451` 等 | `inject = [ToolRegistry]`，装配点唯一 |
| 10 个具体 Service 直接构造 | `agent.py:107-121` | 全部改 inject |

拆完后循环体内**只剩**：取历史 → 发请求 → 消费流 → 分发工具调用 → 判断是否续跑。
A2 的验收（第二个循环实现只改一行 `name:`）能否成立，取决于这张表有没有漏项。

### 5.3 两份循环合一

`runtime/subagent.py:330-391` 与主循环同构但已漂移：不落库、无审批（`ask` 直接坍缩为拒绝）。
合并后**只有一个循环实现**，子代理的差异全部下沉为组合差异：

- 不落库 → 子代理子树里挂一个内存 `sessionLog` provider（换 provider，不是加 if）
- 无审批 → 子代理子树里**不挂** approval provider，waterfall 无应答方自然落为 deny，
  与 dsh 的 `ctx.approval` 语义完全一致（`capability-seams.zh.md`）
- 轮次上限不同 → 组合条目的 `config` 字段

这条同时验证了内核设计：**「行为差异靠子树组合表达」如果在这里成立，L3 的租户差异化就同样成立。**

### 5.4 日志即真源：`deriveMessages` 的 AgentOS 形态

现状真源是 `session_messages` 关系表 + `session_snapshots` 快照水位；`StreamEvent` 不落库。
新形状：

```
session_events (append-only)  ← 唯一真源
      │  derive_messages(session_id, upto?)   纯函数投影
      ▼
模型请求的 messages 数组
```

`session_messages` / `session_snapshots` 降级为**投影缓存**：可重建、可删、可重算。
快照带 `projector_version` 列，判定规则是「**快照永不失效、只会落后**」——
投影器版本变更时旧快照仍可用作前缀，只是要从更早的水位重算。
事件表**不继承 `Base` 的软删除**（否则全局 `is_deleted=false` 过滤器徒增谓词且语义矛盾，
见 `database/engine.py:35-57`）。
压缩不再是「改写历史表」，而是「往日志追加一条压缩事件，投影时据其折叠前缀」——
于是压缩、fork、恢复、回放共用同一条代码路径（dsh 的 `architecture.zh.md:96-100`）。

不变量「模型可见即已记录」用运行时断言实现，但**必须分级**（实测依据见
`research/event-sourcing-feasibility.md` §1.3、§6）：

| 档 | 形式 | 何时开 | 实测成本 |
|---|---|---|---|
| L1 | 规范化**哈希**对内存投影 | **生产热路径常开** | 1~5 ms |
| L2 | 回读全量日志逐字节比对 | 仅 CI / 影子路径 | 0.5~23 s（取决于与 DB 的网络距离） |

不分级的后果是实现者只能在「常开但拖垮热路径」与「只在 CI 开、生产裸奔」间二选一。
断言失败的处置：**发送前中断 + 必发 `turn/end`**。
附带一条既有事实：hook 的 `additional_context`（`agent.py:180-184`）现在不落任何库，
切真源后必须事件化，否则该断言每次都会正确地拦下它。

折叠投影本身不是瓶颈：实测折叠 1 万条事件仅 **2.25 ms**，
成本全在把 1 万行 JSONB 从 DB 拉过来（约 23 s）—— 所以**快照 + 水位必须保留**，
它省的是网络与反序列化，不是 CPU。

中止处理（A4 后半）：轮次被中断时，为每个已 append `tool/call` 但未产生 `tool/result` 的调用
补写合成错误结果，保证日志里 call/result 永远配对 —— 否则投影出的历史对模型是非法的。
写者自己崩溃时它补不了，**由接管者按 5.5 的乐观追加补写**。

### 5.5 最根本的差异：Agent 生命周期 > HTTP 请求生命周期

dsh 是单进程本地工具，agent 活在进程里。AgentOS 是多实例 Web 服务，**一次会话可能跨越
多个 HTTP 请求、多个进程、甚至多次部署**。现有代码用 `RuntimeSuspensionRecord`
序列化剩余 tool_uses 来续跑（`agent.py` 挂起点 + `resume()` 加锁重放），这是绕过该差异的补丁。

有了 5.4 的事件日志，正确解法是**日志驱动的接管**，而不是序列化续跑：

- 任一实例都能 `prepare(session_id)` 从日志投影出完整状态并接手，无需知道上次是谁在跑
- **「挂起」不再是一种状态，而是「收件箱在等一个 approval 事件」**：
  用户批准 = 往日志 append 一条事件；跑着的实例看见即继续，没有实例在跑则任一实例可接管

#### 单写者机制：乐观追加，**不是** advisory lock

**这一条是本次调研推翻的原方案。** 早先写的「单写者靠 PostgreSQL advisory lock 保证」
在本项目的实际部署形态下**不成立，且失效是静默的**（论证见
`research/event-sourcing-feasibility.md` §4.2）：

1. **会话级锁被事务池击穿**。`.env` 的 DB 端口是 6543 —— Supabase transaction-mode pooler
   的约定端口；`database/engine.py:18-19` 的注释自证跑在事务池后
   （「Supabase pooler(事务模式)不支持服务端 prepared statements」）。
   PgBouncer 官方 SQL feature map 对事务池化下的 Session-level advisory locks 标注为 **Never**。
2. **SQLAlchemy 归还语义让锁穿透**。官方 Pooling 文档 Reset On Return：连接归还池只做 rollback，
   不跑 `DISCARD ALL`，因而不含 `pg_advisory_unlock_all()` —— 会话级锁会滞留在被复用的连接上。
3. **事务级锁跨不过 Turn 的分步 commit**（`agent.py:177/204/581/737` 四处），
   而分步 commit 正是 CLAUDE.md 第 5 条允许并且本设计依赖的。

替代方案（message-db 同型）：

- **主机制**：事件表 `UNIQUE(session_id, seq)` + 写者**显式赋 `seq = last_seq + 1`**，
  冲突者让位（fencing）。双写者各自算出同一个 seq，DB 仲裁，败者退出接管。
- **效率层**：租约行（部分唯一索引，先例 `interaction/models.py:34-41`）做降噪，
  让第二个写者通常压根不去抢，而不是靠它保证正确性。
- **可选**：append 批次事务内用 `pg_advisory_xact_lock`（**仅事务级变体**）。
- **禁用会话级 advisory lock**，理由如上。

写者互斥的**唯一源是 PG**；Redis 侧不做互斥（`XADD` 显式 ID 的单调拒写只是双写防错窗）——
否则会出现两个互斥源，谁也说不清以哪个为准。

这样 `RuntimeSuspensionRecord` 整个概念被 5.4 吸收，与 dsh 的收件箱同样是事件溯源的 —— 形状一致，
但 AgentOS 多出「跨实例」这一维，由 5.8 承载。

### 5.6 现有事件契约的六处缺口（新词汇必须补上）

完整清单见 `research/agentos-event-contract.md`（10 种 EventType、18 处 `bus.emit` 发射点、
translator 映射表、SSE 帧契约、三张会话表写入时机）。**该文件是新词汇的映射源**，
迁移时逐个发射点对照，不靠记忆。

其中**六处**缺口若不在词汇设计阶段解决，事件溯源就是假的：

| # | 缺口 | 证据 | 新词汇的处置 |
|---|---|---|---|
| 1 | **token 用量丢失**：一个 Turn 内前 N-1 次模型调用的 usage 连事件都不发；库中无任何 usage 列。成因不是 SDK 拿不到，而是 translator 的 `_on_message_final` 覆盖式缓存 | `llm/types.py:210-220` | 新增 `llm/response` 事件（5.1.1 ①）入日志，载荷含四个 usage 字段；`turn/end` 的 usage = 各 `llm/response` 之和 |
| 2 | **事件无时间戳**；`sequence` 由请求内 `StreamBus` 自增，断线即丢 | `core/event_bus.py` | 日志事件自带 `(session_id, seq, occurred_at)`。**seq 由写者按 `last_seq+1` 显式赋值、由 `UNIQUE(session_id, seq)` 仲裁**；DB 自增列仅作物理主键，不承担游标与并发控制职责 —— BIGSERIAL 官方明示非 gapless 且跨会话可乱序，提交乱序会在续传游标上开出可见性空洞，且 DB 分配的号**检测不出双写者** |
| 3 | **SSE 帧只有 `data:` 行**，无 `event:` / `id:` / 心跳 / 结束帧，无法重连重放 | SSE 端 | 帧带 `id:`＝**会话内 seq**（缺口 2 的产物），客户端用 `Last-Event-ID` 断点续传；免疫空洞的前提是单写者成立（会话内提交顺序 = seq 顺序）。跨会话订阅者若将来引入，需按 Marten 高水位模式另行设计，当前不做 |
| 4 | **subagent 中间过程完全不落库**（只存 prompt + report）；`tool_calls` 表无 actor / run_id 列 | — | 事件统一带 actor 与 run_id，子代理与主代理写同一条日志、按 run_id 区分 |
| 5 | **异常路径可能无终结事件**：未预期异常不发 error 帧，直接 `bus.close()`，前端只能靠流断开推断 | `runtime/agent.py:227-237` | `turn/end` 必发，携带终止原因；无终结事件视为不变量违规 |
| 6 | **增量事件流不足以重建 assistant content**：`redacted_thinking` 被误标为 TEXT，`signature_delta` / `citations_delta` 被直接丢弃 | translator | 词汇文档明示「增量事件仅供 UI，**assistant content 的落库真源是每次调用的 final content**」；translator 迁入 provider 时补 `redacted_thinking` 映射 |

另注：`core/events.py:1` 引用的 `docs/streaming-protocol.md` 已不存在，**当前协议的唯一真源是代码**。
改造后词汇由 `agent` Definition 包拥有，并从其类型定义生成文档（对应 dsh 的 `gen-config-catalog.ts` 做法）。

### 5.7 服务不得持有 `AsyncSession`

**这是插件化最容易踩的陷阱**：fiber 是长生命周期（进程级/租户级），而 `get_db` 的
`AsyncSession` 是每请求一个。现状 30 个文件 `XxxService(db)` 的写法一旦搬进服务槽，
会把一个短命 session 钉死在长命 fiber 上，产生跨请求泄漏与并发误用。

规则：**服务持有 `async_sessionmaker`，每次操作自开工作单元**。

```python
class SessionLogPg(SessionLogService):
    def __init__(self, ctx: Context, config: Config) -> None:
        super().__init__(ctx)
        self._sessionmaker = ctx.require(DatabaseService).sessionmaker

    async def append(self, event: SessionEvent) -> None:
        async with self._sessionmaker() as db, db.begin():
            ...
```

CLAUDE.md 第 5 条已允许「流式 Agent 或后台运行时为了增量持久化分步 commit」——
本设计正落在该豁免内，需在实现时按要求注释生命周期原因。
API 层的普通 HTTP 请求仍走 `get_db` 统一事务，两条路径并存不冲突。

### 5.8 跨实例事件平面（R-T-5 的展开）

原先 R-T-5 在 design 里只有 5.5 末尾一句话，没有任何形状。以下决策依据
`research/cross-instance-events.md`。

**拓扑**（Redis 是**扇出管道 + 热区缓存，不是真源**）：

```
写者（乐观追加，5.5）
   └─ session_events append（写者显式赋 seq）        ← 真源
        └─ XADD agentos:events:{session_id} {seq}-0  ← 扇出
             └─ N 个订阅者（任一实例）XREAD
```

**五条决定**：

1. **每会话一个 stream，不用 consumer group**。consumer group 是**竞争消费**
   （一条消息只投递给组内一个成员），与「同一会话的每个订阅者都要收到全量」正好相反。
2. **ID 对齐**：stream entry ID 显式用 `{seq}-0`，SSE 帧 `id:` 也用 seq ——
   DB 游标与 Redis 游标**合并成同一套整数**，续传时直接整数比较即可判断热区是否有空洞，
   有空洞就回落 DB 补读。
3. **写序：先 DB 后 XADD**。反过来会让 Redis 里出现尚未落库的事件，
   订阅者看得见、重启后消失。
4. **互斥唯一源是 PG**（5.5）。Redis 侧不加锁；`XADD` 显式 ID 的单调拒写只当双写防错窗。
   否则会出现两个互斥源，谁也说不清以哪个为准。
5. **降级路径**：memory transport（单实例开发态）；Redis 不可用时退纯 DB 轮询。

**SSE 传输规范**（现状帧只有 `data:` 行）：

- 15s 心跳注释帧；`Cache-Control: no-store` + **`X-Accel-Buffering: no`**
  （nginx `proxy_buffering` 默认 on，不加这个头 SSE 会被缓冲住）；显式终结帧。
- **断线 ≠ 中止运行**。现状 `runtime/agent.py:139-146` 把 SSE 断开与运行中断绑在一起，
  与 5.5 的「日志驱动接管」直接冲突 —— 必须解耦。

**端点形状（这是本次新发现的一处硬缺口）**：现有两个 SSE 端点**都是 POST**，
而浏览器原生 `EventSource` 只能发 GET，也只有它会在重连时自动带 `Last-Event-ID` 头。
因此必须**新增 `GET /sessions/{id}/events` 订阅端点**，把「订阅」与「发消息」分离；
两个 POST 端点的既有 SSE 行为按过渡期保留。

**配置**：新增 6 个 `AGENTOS_EVENTS_*` 键（transport 选择、stream 上限、心跳间隔、
续传窗口等），并**吸收现有的 `AGENTOS_DISABLE_REDIS_EVENTS`** ——
该键在 `.env` / `.env.example:37` / `core/config.py:54` 三处都在，但**全仓零代码读取**，
属 CLAUDE.md「不得保留无代码读取的废弃环境变量」要清理的对象。

**`sse-starlette` 的取舍**：`uv.lock` 里已有 3.4.5（经 `mcp[cli]` 传递依赖进来），
但全仓零 import。若 P6-5 采用它，必须在 `pyproject.toml` 里**提升为直接依赖**，
不能依赖传递路径 —— 传递依赖随时可能因上游调整而消失。

## 六、L2 组合层

### 6.1 条目与 patch 语法

沿用 dsh 的形状（`research/plugin-loading-and-composition.md` 第四节），YAML：

```yaml
- id: llm-anthropic            # patch 的定位键，必须稳定
  name: llm_anthropic          # 插件模块名
  config:
    max_tokens: 32000
- id: shell
  name: shell_local            # ← 换成 shell_sandbox 即整体切沙箱执行世界
```

patch 语义严格照搬：按 `id` 定位、`config` **整体替换不深合并**、`disabled` 关行、`insert` 加行。
**行序不带加载语义**，激活由服务可用性驱动 —— 这条必须写进文件头注释，否则使用者一定会
按顺序理解依赖，然后在依赖变化时被 epoch 的自动重载搞懵。

不引入 dsh 的 `!!js` 表达式（Python 侧等价物是执行任意代码，安全代价不划算）。
需要动态值时走「前一行提供服务、后一行 inject」的方式，与 dsh 的 `webStartup` 同构。

### 6.2 patch 栈：AgentOS 有一层 dsh 没有的「DB 层」

| 层 | 来源 | 谁改 | 热更新 |
|---|---|---|---|
| 1 `agentos-base` | 代码内置 | 开发 | 否（改代码） |
| 2 部署层 `cordis.yml` | 仓库/镜像内文件 | 运维 | 否（重启） |
| 3 环境层 `$AGENTOS_HOME/cordis.patch.yml` | 机器本地文件 | 运维 | 是 |
| 4 **租户层** | **数据库** | 租户管理员 | 是（R-T-3） |
| 5 **Agent preset 层** | **数据库的 Agent 实体** | 用户 | 是（R-T-2） |

第 4、5 层的组合来源是 DB 而非文件，是 AgentOS 与 dsh 的结构性区别。
但**加载器不需要知道来源** —— 每层只需产出一个 patch 数组，来源差异由各自的 provider 吸收。
这保证 L2 的 Loader 设计不被 L3 需求污染。

### 6.3 `dump-config` 与启动断言

- `uv run python -m agentos dump-config [--tenant X] [--agent Y]`：
  打印带来源层注释的完整条目树而不启动进程。**打印出的每一行都必须可被 patch 替换**（A5）。
  敏感字段（含 `password`/`token`/`key`/`secret`/`dsn`/`url` 的键）统一脱敏为 `***`，满足 R-X-4 / A8。
- 启动完成时遍历全部条目，非 ACTIVE 即列出条目 `id`、状态与失败原因并退出非零。
  **这条比看起来重要**：epoch 驱动的自动激活意味着「少配一个 provider」的表现是「某些插件静默不工作」
  而不是报错。没有这条断言，插件化会把「启动即失败」变成「运行时才发现」。

### 6.4 R-C-5 的真正难点：FastAPI 路由的动态挂载与卸载

工具、提示词片段随插件挂卸都简单（本来就是注册表）。**路由不是** —— Starlette 的
`app.router.routes` 是普通 list，追加可行。以下结论均已在项目实装版本
（FastAPI 0.136.3 / Starlette 1.3.0）上实测，依据见 `research/toolchain-feasibility.md`：

1. **启动后 `include_router` 生效**：Starlette 源码无启动守卫（对照 `add_middleware`
   有 `RuntimeError` 守卫），13 项断言实测通过。**官方无书面承诺，属源码推断 + 实测。**
2. **OpenAPI 缓存**：`app.openapi_schema` 挂载/卸载后必须置 `None` 强制重算。
   `/docs`、`/openapi.json` 无其他服务端缓存层（HTML 壳 + 每请求重读该属性）。
3. **卸载靠挂载时快照 diff，不能事后反查**：`include_router` 会为源 Route **复制**新对象，
   拿插件自己的 router 反查不到实际挂上去的那些。做法是挂载前后对 `app.router.routes`
   取快照 diff 记下新增对象，卸载时按 `id()` 过滤重建整表并一次性重绑
   （copy-on-write，**不能用 `list.remove`** —— `Route.__eq__` 是值相等，会误删同签名路由）。
4. **`url_path_for` 没有缓存**：每次线性遍历路由表（全包 grep 无 `lru_cache`），无需清理。

四条硬约束（缺一即在运行期暴露）：

- 插件 router **禁止携带 `lifespan` / `on_startup` / `on_shutdown`**：启动后挂载不会执行，
  且合并不可逆，反复挂卸会让 lifespan 包装链持续增长。
- 插件**禁止动态注册中间件与全局异常处理器**：前者启动后抛 `RuntimeError`，后者静默无效。
- 挂卸必须 `asyncio.Lock` 串行化，且只在事件循环内执行。
- **多实例/多 worker 下动态挂载是单进程状态**。这条由现有证据定死，不用另做选择：
  今天 `main.py:59` 是 `uvicorn.run(app, ...)` 无 `workers`，即单进程；而 R-T-5/A11 要求多实例。
  两者合起来的结论是 —— 本节上面已定「**路由不做租户级差异化**」，
  于是路由表只随**进程级根树**变化，而进程级根树对所有实例是同一份组合文件，
  因此：**生产环境的进程级组合变更走滚动重启，不依赖运行时跨实例广播**。
  动态挂卸能力仍然保留且必须实现 —— 它让 `effect` 语义在 `api` seam 上不出现例外，
  且开发期热装、测试期起停都靠它。**但不得把「运行时给线上加路由」写进产品能力。**

因此 `api` 能力做成 seam：Definition 提供 `ctx.http.mount(caller, router, prefix)`，
内部用 `caller.effect()` 登记，disposer 负责快照 diff 移除 + 置空 OpenAPI 缓存。
消灭 `api/router.py:3-38` 的静态聚合。

**路由不做租户级差异化**（L3 只差异化能力组合，不差异化 HTTP 表面）——
否则 OpenAPI 文档与反向代理配置会随租户漂移，收益远小于代价。

### 6.5 非 Web 进程也要跑组合树

现状被忽略的一个入口：`memory/jobs/worker.py:98-104` 是独立进程
（`python -m memory.jobs.worker`），靠 import 期的全局 `settings` / `AsyncSessionLocal` /
`LLMClient()` 自给自足。这些全局量正是 R-S-7 要消灭的对象 —— 一旦消灭，
worker 里就拿不到 `DatabaseService` / `LlmRuntime`，而
`memory/jobs/__init__.py:12-17` 明确只有 Worker 会做 dream 认领、retention 清理、
inline 失败后的重试接管，**这三件事会静默失效**。

因此进程平面有**两个入口共用同一套装配代码**：

```
compose_root(profile)  →  Context 根树        # 唯一装配入口
   ├── web 入口   : main.py  → 挂 api seam provider（FastAPI 宿主）
   └── worker 入口: worker.py → 不挂 api，只挂 jobs / memory / llm / database
```

差异**只体现为组合文件的 profile 差异**（`profile: worker` 少几行），不体现为代码分叉。
这条与 6.4 的路由结论一致：worker profile 里没有 `api` 条目，自然没有路由挂卸问题。
`main.py:60-61`「import 即 `create_app()`」也在这一步被消灭 —— 入口函数化，
由 Loader 驱动，而不是 import 副作用。

## 七、L3 多租户与开放

### 7.1 三级作用域

| 级别 | Context 节点 | 生命周期 | 能覆写什么 |
|---|---|---|---|
| 进程级 | 根 Context | 进程 | 全部（部署期装配） |
| 租户/workspace 级 | `root.isolate(...)` 派生的 realm | 首次访问到租户停用 | 能力 provider 选型、配额、第三方插件 |
| 会话级 | realm 的子 fiber | 一次会话 | 工具白名单、提示词片段、模型参数 |

**不能覆写的**：内核本身、数据库连接、审计与计费相关的服务槽 —— 这些必须固定在进程级，
否则租户可以把自己的用量记账关掉。实现上靠「租户 patch 的 `id` 白名单」限制可 patch 的行。

这里说的「计费服务槽」**不是一个单独的 `usage` seam**（首批清单里没有这一项）：
计费数据的载体是 5.1.1 ① 新增的 `llm/response` 事件 —— 它每次模型调用必发、直接入日志，
监听它的记账插件挂在**进程级**且不在租户可 patch 的 `id` 白名单里。
这样「租户关不掉自己的记账」是由「租户改不到进程级那一行」保证的，不需要额外机制。

隔离由 `ctx.isolate(name, label)` 提供，label 是租户 ID 派生的哨兵对象。
**隔离域同时是事件边界**（2.6）—— 这一条让 A9「一方的服务对另一方不可见」不需要额外机制。

### 7.2 realm 的挂载时机与失败处理

照搬 dsh 的 preset 挂载纪律（`research/plugin-loading-and-composition.md` 第三节）：

- realm 内的服务必须放进带 `isolate` 的分组，**防止发布进根 realm 造成跨租户冲突**
- 每个租户/preset 进程内只挂一次（standing scope），会话按父子关系「加入」而非重挂
- 挂载失败则**整个会话创建回滚**，不允许出现「会话建好了但能力半残」的状态

会话结束时销毁其派生子树，A9 要求「全部注册均已撤销」—— 这直接落在 R-K-4 的 effect 语义上，
不需要为多租户单独设计清理逻辑。这是内核设计正确带来的红利。

## 八、数据层归属：每插件一条迁移链

完整论证见 `research/alembic-per-plugin-migrations.md`。

### 8.1 为什么这一节必须存在

L1 把能力切成可插拔的 provider 后，**建表权也跟着散了**：`sessionLog` 的 PostgreSQL provider
要建 `session_events`，`memory` 的向量 provider 要建自己的表，租户装一个插件就可能带一张表。
而现状是**单链单 metadata**：`alembic/env.py:25` 的 `target_metadata = Base.metadata`
配 `database/registry.py` 的全量汇总导入。这套机制的失效方式项目自己已经写在
`database/registry.py:1-5` 的 docstring 里 —— **遗漏任一模块，autogenerate 就把缺失实体误判为删表**。

插件场景把这个「遗漏」从人为疏忽变成常态：**卸载一个插件 = 它的 metadata 不再进列表 =
下一次 autogenerate 立即为它的表生成 DROP**。这是插件化数据层的主要误删通道，
必须在设计阶段解决，不能留给实现时随手处理。

### 8.2 选定路线：程序化多环境（每插件独立版本表）

Alembic 官方有两条路线，选择依据是它们各自的设计意图原文：

| | 多 base（`version_locations` + `branch_labels`） | **多环境（cookbook「Run Multiple Alembic Environments」）** |
|---|---|---|
| 版本表 | 共享一张 `alembic_version`（多行，每 head 一行） | **每链独立**（`version_table` 参数改名） |
| 官方定位 | 为链间 `depends_on` **交叉依赖**设计 | "completely independent revision lineages" |
| `upgrade head` | **永久失效**，必须 `heads` 或 `<label>@head` | 每链单 head，链内 `head` 永远合法 |
| 单链卸载 | `downgrade <label>@base` | `downgrade base`（物理隔离，碰不到别人） |
| `target_metadata` | 共享 | 每链自己的 |

**选多环境。** 理由是需求与官方分界原文正好对齐：插件要的是「装卸不影响别人」，
而多 base 的唯一独占优势 `depends_on`（跨链依赖）**恰恰是插件场景要禁止的东西** ——
却要照付它的全部成本（`upgrade head` 失效、共享版本表、共享 metadata）。

结构：

1. **核心链原样不动**：`alembic/versions/` 现有 21 个 revision、`alembic_version` 表、
   现有 env.py 与 CLI 流程全部保持。这一节不产生对核心链的任何破坏性改动。
2. **插件包内自带**：`migrations/` 版本目录 + 独立 `MetaData` + 表名强制 `plugin_<name>_` 前缀。
3. **宿主程序化驱动**：`Config()` + `set_main_option("script_location", <插件 migrations 路径>)`，
   共享一个通用 env.py 模板，`context.configure(version_table=f"alembic_version_plugin_{name}",
   target_metadata=<该插件 metadata>, include_name=<按前缀白名单>)`。
4. **装 = `command.upgrade(cfg, "head")`；卸 = `command.downgrade(cfg, "base")` + 删自己的版本表。**

### 8.3 三道强制防护（缺一不可）

1. **`include_name` 前缀白名单**：每个插件环境只看得见自己前缀的表。机制在本仓库已有先例 ——
   `alembic/env.py:32,45` 就是用 `include_name`（`database/migration_history.py:20-27`）
   把 `alembic_version_history` 排除在 autogenerate 之外的，**不是新引入的技巧**。
2. **加载期校验表名前缀**：插件 metadata 中出现不合前缀的表 → **拒绝加载**，
   而不是等 autogenerate 出事。多 MetaData 要求 table key 全局唯一，重名会直接报错。
3. **禁止插件之间的外键**。插件→核心表的外键允许，但宿主必须固定「先核心后插件」的执行顺序，
   并让插件声明 `requires_core_revision >= X` 在 upgrade 前校验 ——
   **Alembic 不校验跨环境依赖，这是本路线主动让渡的能力**，必须由宿主补。

### 8.4 已知失败模式与处置

| 失败模式 | 处置 |
|---|---|
| 卸载半程失败（多 revision 降级非整体原子） | 标记「卸载失败待处理」，**不得直接移除插件代码** —— 迁移脚本一丢，残表永远无法再用 Alembic 清理 |
| 孤儿表（代码先删、库没 downgrade） | 防护的代价是「不删 = 永久残留」。需运维工具列出「版本表存在但插件不存在」的孤儿，人工处置 |
| 审计缺口 | 现有 `on_version_apply` 审计只挂在核心 env.py（`alembic/env.py:46`）。**通用模板必须挂同一钩子**，history 表加 `plugin_name` 列区分 |
| `create_tables` 开发捷径漂移 | `main.py:24-26` 只建核心表，插件表没人建 → 开发与生产行为不一致。lifespan 里对启用插件统一跑 upgrade，两边同路径 |

**第二个数据层缺口（与迁移链彼此独立，此前未记录）：会话级联软删除的静态清单。**

`session/repository.py:24-38` 用一个硬编码的 `_SESSION_CHILD_MODELS` 列表，
在删除会话时级联软删 10 张**别域**的表。这套机制的前提是「所有带 `session_id` 的表
在编译期就能枚举完」—— 而插件自带表**恰恰打破这个前提**：
一个插件带一张 `session_id` 子表，它的行在会话删除时不会被清理，
而插件卸载后连清单里该写什么都无从得知。

处置方向（P3-4 定案）：把静态清单改为**子表所有者向 `sessionPersistence` 注册清理钩子**，
钩子随插件 effect 挂卸 —— 这与 R-K-4 的可逆副作用是同一套机制，不需要新概念。
顺带解决了现状的一个既有问题：这个清单跨 10 个域硬编码，本身就违反了
CLAUDE.md「feature 包之间只允许 Service 调 Service」。

### 8.5 退路（若多环境在实现期证伪）

按优先级：**独立 PostgreSQL schema**（`DROP SCHEMA CASCADE` 一键清场，也可作本方案的加固变体）
> **JSONB 附属表**（插件不准建表，轻插件够用、重插件表达力不足）
> 插件自带建表 SQL（**等于自造迁移引擎，不推荐**，且违背 CLAUDE.md「提供显式 upgrade/downgrade 迁移」）。

## 九、第三方插件：「一切皆插件，但插件分公民等级」

完整论证见 `research/third-party-plugin-isolation.md`。

### 9.1 dsh 在这里抄不到作业

**dsh 的插件是全信任代码**：`dsh plugin add` 是 pnpm 转发器，装完即同进程 Node 代码，
甚至指导用户放行 git 依赖的安装期构建脚本（`apps/cli/src/plugin.ts:129-133, 148-155`）。
dsh 的全部沙箱设施（sandbox / e2b / code-runtime）针对的都是**模型产物** ——
`confine` 限的是工具将要 spawn 的 argv，e2b 搬走的是执行世界，code-runtime 说的 "hostile peers"
指模型写的程序。检索 dsh 全仓 untrusted / trust boundary 关键词，命中全部指向模型产物侧。

**结论：不可信插件问题不在 dsh 的威胁模型里。R-T-4 必须自己设计。**

### 9.2 硬约束：进程内隔离不可信 Python 代码没有现实路径

RestrictedPython 自述 not a sandbox 且有逃逸 CVE；audit hooks 是检测不是防护；
进程内 seccomp 会连宿主一起杀；subinterpreters 不是安全边界。
更直接的事实：同进程一行代码就能拿到 `settings.jwt_secret_key`（`core/config.py:115, 142-143`）。

因此 **A10 与「租户插件同进程运行」直接互斥**。「一切皆插件」在 AgentOS 必须精确化为：

> **组合机制对所有等级统一**（条目 / patch / epoch / 依赖驱动完全一样），
> **能力词汇按等级裁剪**。

### 9.3 四个公民等级

| 等级 | 谁能装 | 跑在哪 | 能做什么 | 不能做什么 |
|---|---|---|---|---|
| **T0 平台内建** | 随代码发布 | 同进程 | 全能力：ctx 服务、五种分发、inject、自带 ORM 表与迁移 | —（受工程纪律约束） |
| **T1 可信第三方** | **仅运维/平台管理员**部署期经组合文件装配 | 同进程 | 同 T0 | 绝不进租户自助安装流程；必须审核 + 签名 + 版本 pin |
| **T2 租户协议插件**（R-T-4 的正主） | 租户 workspace owner/admin | 进程外 MCP server，底座是第十节的沙箱 | 提供工具（命名空间化）、提示词片段、资源；带租户短时 token 回调平台 API；经 stub 参与依赖驱动；订阅**白名单通知型**事件 | 不得注册任意 ctx 服务；**不得挂 waterfall/serial/bail**；无 DB/Redis/MinIO 直连；无宿主 fs；默认无出网；受限额 |
| **T3 租户一次性脚本**（收编现有 skill 通道） | 租户 workspace owner/admin | 进程外一次性子进程，**必须补 OS 沙箱** | 短生命周期计算：argv/stdin 进、stdout 出 | 无常驻、不参与组合、无网络、无平台 API 回调（需要就升 T2） |

**T1 与 T2 的分界不是技术，是安装权** —— 谁按下安装键，谁就进入了信任边界。

### 9.4 stub 代理：进程外插件如何参与组合

ctx 服务槽、waterfall 监听、inject 本质都是内核地址空间内的 Python 调用，
进程外代码不可能持有 ctx 对象。唯一通路是**平台自带一个受信的通用代理插件（stub）在内核侧代表远端**：
stub 声明 `inject`、注册服务、挂监听，把每次调用翻译成一次 RPC；远端存活由 stub 监督
（心跳/重启），映射为服务的 provide/撤销，**epoch 依赖驱动照常工作**。

| 组合能力 | 可达度 |
|---|---|
| 提供工具 | **完整** —— MCP 即为此设计（`pyproject.toml:23` 已声明 `mcp[cli]` 但全仓零 import） |
| 提供 ctx 服务 | **部分** —— 仅粗粒度、请求-响应、可序列化、容忍毫秒时延者；**持活对象服务不可能跨进程**（`AsyncSession`、`StreamBus`、返回进程句柄的 subprocess seam） |
| 挂事件监听 | **通知型（emit/parallel）可桥**；waterfall/serial/bail 技术上可桥但**不应对租户开放** |
| 被 inject / 依赖驱动 | **对 stub 完整** —— stub 是正常一等插件 |
| 自带 ORM 表 | **不可达（政策性）** —— 不给租户代码 DB 直连 |

为什么 waterfall 绝不给租户：挂在 `agent/request` 上就能看见并篡改全部提示词与上下文 ——
**那等于交出会话主权与数据**。而且每个监听点是热路径上一次同步 RPC 往返。

### 9.5 与 dsh 必须反着做的一条

**禁止安装期执行代码**。dsh 因为信任插件而教用户放行 pnpm 构建脚本；
AgentOS 对租户上传物**只解包、只静态校验**，任何构建/安装钩子一律不执行 ——
依赖装配在沙箱内完成，或要求上传自包含产物。

来源校验：整包 sha256 已有（`skill/service.py:163-166`），扩展为安装记录 hash + 版本 pin
（学 dsh 的 `github:owner/repo#sha` pin 语义）；市场渠道加签名与静态扫描。

## 十、`sandbox` seam 设计

### 10.1 接口：一个方法

照抄 dsh 的最小接口（`research/capability-seams-and-patterns.md` 第二节）：

```python
class SandboxProvider(Service, ABC):
    SERVICE_NAME = "sandbox"

    @abstractmethod
    def confine(self, argv: Sequence[str], policy: SandboxPolicy) -> ConfinedArgv: ...
```

拿到即将 spawn 的 argv，按策略**包装 argv**。**策略本身归 `sandboxPolicy` 单独持有** ——
这样 shell 与 fs 才不会限到不同的根目录。这是 dsh 的关键细节，容易漏。

### 10.2 实现梯子与 fail-closed

| 平台 | 首选 | 回退 | 定位 |
|---|---|---|---|
| Linux | bubblewrap（用户命名空间 + 挂载白名单） | Landlock（无特权 LSM，5.13+；ABI4/6.7+ 可限 TCP） + seccomp 缩系统调用面 | **生产** |
| macOS | Seatbelt `sandbox-exec -p` | — | **仅开发机**（Apple 标 deprecated） |
| Windows | 受限令牌 + ACL | — | dsh 自标 partial，**不作多租户依据** |

「功能探测 → 回退链 → **不可用即 fail-closed 拒跑**」直接照搬 dsh
（`sandbox-local/src/index.ts:2-6, 67-90`）。部署期需确认发行版是否默认限制非特权 userns。

生产多租户目标档是**容器 / microVM**（gVisor 用户态拦 syscall；Firecracker/Kata 每租户独立客体内核），
但 seam 接口不变 —— 换 provider 而已，这正是 seam 化的收益。

### 10.3 现状最大缺口：skill 脚本零 OS 边界

`skill/service.py:307-374` 的 `run_script` 是**现存唯一「租户上传代码并执行」通道**。
已有：超时、`killpg` 整组回收、env 白名单、一次性 `mkdtemp` 目录、DB/Redis/MinIO 凭证不下发。
缺的是 OS 边界本身 —— **脚本可读宿主 `.env`，网络全敞开**。

`core/executor.py:110-118` 的 `get_executor()` 已预留 `SandboxExecutor` 切换点，
所以 T3 补沙箱是「换一个 provider」而不是重写。

### 10.4 限额与出网

| 维度 | 现状 | 目标 |
|---|---|---|
| 时间 | 已有（超时 + `killpg`，`core/executor.py:72-90`） | 保持 |
| 输出 | 已有截断（`core/executor.py:92-95`） | 保持 |
| CPU/内存 | **无** | 生产用 cgroup v2（容器原生；裸机 `systemd-run --scope -p MemoryMax= -p CPUQuota=`）；轻量档用 rlimit |
| 磁盘 | 一次性目录（`skill/service.py:333`） | 沙箱 tmpfs 或容器存储配额 |
| 出网 | **全敞开（最大缺口）** | **默认全禁**（bwrap unshare-net / Landlock ABI4 / 容器网络策略）；按插件声明经**平台正向代理**放行白名单域名，代理侧记账审计 |
| 常驻实例 | — | 每租户实例数/并发上限 + 空闲回收，防囤进程 |

数据访问：插件读写业务数据只能携带**租户作用域短时 token** 回调平台 API，
由平台 Service 层执行既有权限规则 —— 这就是 CLAUDE.md「跨域只许 Service 调 Service」
在进程边界上的推广。

## 十一、测试范式与回归网

### 11.1 「回归网先行」在这里的正确含义

R-X-2 要求破坏性改造前先有回归网，但本次是**结构全换**：针对旧结构写的单元测试改造后一条都不适用
（D4 已决不重建那 25 个旧测试）。所以「先行」不能理解成「先补旧结构的单测」，而是：

**先建立行为等价判据，判据必须是端到端的、不依赖内部结构的。**

具体就是一条真实会话主路径：用户输入 → 模型返回工具调用 → 工具执行 → 结果回灌 → 结束。
改造前跑通它（先修复 import 断裂），改造后同一条断言必须仍然通过。这是唯一能横跨新旧两套结构的判据。

顺序上因此有一个硬前置：**必须先修复 `llm.client` 断裂让工作区能 import**，否则连基线都跑不出来。

### 11.2 测试拓扑与生产拓扑同构

照搬 dsh 的范式（`research/capability-seams-and-patterns.md` 第六节）：
**不 mock 内核、不 monkey-patch，起真 Context 挂真服务栈，Fake 走与生产同一个扩展点。**

```python
async def harness(**config) -> Context:
    ctx = Context()
    await mount_agent_loop_test_dependencies(ctx)   # 真 LlmRuntime / SessionLog / SystemPrompt / ToolRegistry
    await ctx.plugin(AgentLoop, {"agents": []})
    return ctx

# Fake 通过正规 registry 注入，与生产替换同一机制
ctx.llm.register_adapter(ctx, ["mock"], MockAdapter([...脚本...]))

# 或直接 waterfall 短路整层替换
ctx.on("llm/stream", lambda options, next_: replay(options))   # 不调 next_ 即替换
```

这条纪律的价值不只是「测试更真」：它使**测试本身成为插件化是否成功的验证** ——
如果 Fake 必须靠 monkey-patch 才能注入，说明那个扩展点根本没做出来。

### 11.3 分层与最低覆盖

| 层 | 内容 | 对应验收 |
|---|---|---|
| 内核单测 | 七项子验收①–⑦，**不加载任何业务插件** | A1 |
| 组合层测试 | patch 叠加结果、`dump-config` 输出、缺依赖启动失败 | A5 |
| seam 契约测试 | 每个 seam 的两个 provider 跑同一组断言 | A3 |
| 会话主路径 | 成功路径 + 失败路径 | A7 |
| 不变量断言 | 「模型可见即已记录」在 CI 中以运行时断言执行 | A4 |
| 静态检查 | import-linter 三条契约 + `grep` 类断言 | A2、A6 |

PostgreSQL 相关（会话日志、**乐观追加的并发仲裁**、租户隔离）按 CLAUDE.md 要求走
**真实 PostgreSQL 集成测试**，不用 SQLite 替身 —— `UNIQUE(session_id, seq)` 的冲突让位行为
只有在真实并发事务下才测得出来。`pyproject.toml:26-28` 的 `testpaths=["tests"]` 保持不变，
重建 `tests/` 目录。

## 十二、迁移路径与阶段划分

改造范围是 L0–L3 全量，但**不能一次性切换** —— 需要每一阶段结束时系统都可运行、可验证。

| 阶段 | 内容 | 结束时的可验证状态 |
|---|---|---|
| **P0 解封** | 修复 `llm.client` 断裂的 7 处 import；重建 `tests/`；跑通一条会话主路径基线 | 工作区可 import，基线测试绿 |
| **P1 内核** | `kernel/` 独立开发，不接触任何业务代码 | A1 七项子验收全绿（业务代码零改动） |
| **P2 首条竖切** | 只做 `llm` 一个 seam 走完三角色 + 最小组合文件 | 换 provider 只改一行；主路径仍绿 |
| **P3 seam 铺开** | 其余 17 个 seam 按依赖顺序（storage/subprocess/fs/shell → tools → session → 其余） | A3；每个 seam 落地后主路径仍绿 |
| **P4 循环** | 事件词汇冻结 → 日志真源 → 拆循环 → 两份合一 | A2、A4 |
| **P5 组合层** | Loader + patch 栈 + `dump-config` + 启动断言 | A5、A6 |
| **P6 多租户** | realm 隔离 + Agent preset + Redis 事件 | A9、A11 |
| **P7 开放** | 第三方插件 + 沙箱 | A10 |

**P2 是关键的风险闸门**：一个 seam 走完全流程会暴露内核设计的所有问题（显式 ctx 传递够不够用、
epoch 重载会不会误伤、服务持有 sessionmaker 的写法顺不顺手）。P2 不顺就回头改内核，代价可控；
跳过 P2 直接铺开其余 17 个 seam，问题会在 P3 中段集中爆发且难以回退。

**P4 必须在 P3 之后**：循环是所有 seam 的最大消费方，seam 没稳定就拆循环等于同时动两端。

## 十三、风险与回滚

### 13.1 风险

| # | 风险 | 影响 | 缓解 |
|---|---|---|---|
| 1 | **规模**：32000 行 + 零测试 + 全量重构 | 最大风险 | P0–P7 阶段化，每阶段结束系统可运行；P0 先建行为等价判据 |
| 2 | **内核 bug 爆炸半径**：内核错则全系统错 | 高 | P1 内核独立开发、零业务依赖；A1 七项子验收；内核可脱离 AgentOS 单独跑 |
| 3 | **epoch 自动重载的意外抖动**：provider 换代触发大面积级联重载 | 中高 | 四件套合并 notify（2.4，`call_soon` 单独不够）；重载必打日志；启动断言兜底 |
| 4 | **effect 挂错 fiber**（2.2）：仅剩队列边界靠纪律 | 中低 | contextvars 缺省已结构性消除 4/5 条路径；队列接力处显式传 `ctx=`；**唯一运行时兜底是 A1⑤ 计数归零断言** |
| 5 | **事件词汇冻结过早**：被 18 个 seam 消费后难改 | 中 | P2/P3 只用最小子集，P4 才正式冻结；词汇由 Definition 包拥有，改动有单一归属 |
| 6 | **性能**：服务查找走父链、waterfall 多层调用 | 中低 | 查找层数＝插件树深度（个位数）；**热路径用 `emit` 不用 `waterfall`**（`assistant/chunk` 每 token 一次，绝不能是 waterfall） |
| 7 | **多租户内存增长**：每租户一棵常驻子树 | 中低 | realm 懒挂载 + 空闲超时卸载；卸载正确性由 R-K-4 保证 |
| 8 | **顶层包 22 → 约 60** | 低但需确认 | 属 CLAUDE.md 约定的扩充，需在最终评审签字（3.2） |
| 9 | **插件自带表与会话级联软删除冲突**（新增） | 中 | `session/repository.py:24-38` 现在硬编码级联软删 10 张别域表；插件自带 `session_id` 子表后该清单无法再静态枚举 —— 必须改为「子表所有者注册清理钩子」，见第八节 |
| 10 | **规模估算偏乐观**（新增） | 低 | 内核本体从 1500–2500 上修为 **2000–3000** 行：原型只写了语义面「容易的一半」且零鲁棒性就已 1002 行，而 Cordis 最难的 `fiber.ts` 有 754 行重入语义一行没动 |

### 13.2 回滚

- **每个阶段一个 git 分支点**，阶段内失败直接 revert 到该点。D1 已决不保留兼容层，
  所以阶段内是替换式改造，revert 是唯一回滚手段 —— 因此阶段划分不能太粗。
- **唯一不可逆的是数据库**：P4 引入 `session_events` 表。原则是**只加表不删表**，
  `session_messages` / `session_snapshots` 降级为投影缓存后**保留结构**，
  确认稳定前不写删表迁移。真要删也必须走新的 Alembic upgrade/downgrade（CLAUDE.md 明令）。
- **P4 的不可逆点**是「停止把 `session_messages` 当真源」的那一刻。切换前必须先证明
  A4（日志投影与实际请求逐字节一致）在真实会话上通过，而不只是在测试用例上通过。
