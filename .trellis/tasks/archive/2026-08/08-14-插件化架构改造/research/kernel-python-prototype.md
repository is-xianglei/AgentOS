# 研究:L0 内核关键假设的 asyncio 原型实证

- **Query**: 用最小可运行原型逐条验证 design.md 第二节与 prd R-K-1..7 在 Python/asyncio 侧的设计断言(Q1–Q6)
- **Scope**: mixed —— 内部(design/prd/Cordis 源码比对)+ 原型实证(`/tmp/kernel_proto/`,仅标准库;mypy/pyright 走 uvx 临时环境,未动项目依赖)
- **Date**: 2026-08-15

## 0. 原型清单与运行环境

| 文件(/tmp/kernel_proto/) | 行数 | 验证对象 | 运行结果 |
|---|---|---|---|
| `q1_effect.py` | 136 | design 2.5 拆除顺序 + 两种错误策略 | 3 场景全 PASS |
| `q1b_alternatives.py` | 79 | AsyncExitStack / TaskGroup 否决证据 | 3 场景全 PASS |
| `q2_epoch_merge.py` | 153 | design 2.4 notify 合并(4 策略 × 3 场景) | 12 组全跑通 |
| `q3_bus.py` | 104 | 五种分发的完整实现 | — |
| `q3_test_basic.py` / `q3_test_waterfall.py` | 208 | emit/parallel/serial/bail + waterfall | 全 PASS(首跑修正过一处断言字符串,见 Q3) |
| `q4_explicit.py` / `q4_contextvars.py` | 244 | design 2.2 显式 ctx vs contextvars | 全 PASS;contextvars 证据推翻部分结论 |
| `q5_typing.py` | 78 | design 2.3 类型化服务查找 | mypy 与 pyright 行为分裂 |

- 环境:Python 3.14.4(uv 安装);mypy 2.3.1、pyright 1.1.411(uvx 临时环境)。
- 另与 Cordis 源码逐行比对了拆除语义(`vendor/cordis/src/fiber.ts:427-441, 676-686`),
  发现 `research/cordis-kernel-spec.md` 公理 5 的「逆序执行全部 disposer」表述不精确,证据在 Q1。

---

## Q1 effect 拆除顺序(design 2.5)

### 实测结果

1. **「顶层并发、单 effect 内逆序串行」可以干净实现,核心约 40 行。**
   effect 内部 `for fn in reversed(disposers): await fn()`;顶层
   `asyncio.gather(*(e.dispose() for e in effects), return_exceptions=True)`。
   实测:A(3 个 disposer)+ B(2 个)各 0.03s,总耗时 0.096s(全串行应 0.15s),
   执行序 `['a3','b2','a2','b1','a1']` —— 顶层交错并发、effect 内严格逆序。

2. **Cordis 的真实错误语义与调研文档表述不同(读源码发现):**
   - fiber 级 `_unload`(fiber.ts:676-686):`Promise.all` 并发,每个顶层 effect 单独
     try/catch,错误 `logger.error` 记日志,**不聚合也不上抛** —— 卸载永不失败,状态机继续走;
   - 单个 effect 内部(fiber.ts:427-441):`.then()` 链逆序串行,某 disposer 失败会
     **跳过该链剩余 disposer(fail-fast)**,错误冒到 fiber 级被吞。
   `cordis-kernel-spec.md` 公理 5 写「卸载=逆序执行全部 disposer」,在「同一 effect 内
   有失败」时不成立 —— 剩余的不执行。
   原型两种策略都做了:failfast(保真)与 continue+聚合(对照)均可实现;实测差异:
   failfast 时 `a1` 被跳过、兄弟 effect B 完整执行;continue 时五个全执行。

3. **异常聚合与上下文:`ExceptionGroup` + PEP 678 `add_note` 完全够用。**
   每个异常 `add_note(f"disposer={fn.__name__} effect={name}")`,顶层聚合为
   「fiber 级 EG → effect 级 EG → 原始异常」两层嵌套;`traceback.format_exception`
   输出里 note 与原始 raise 帧都在,归属可直接定位(断言实测通过)。

4. **`AsyncExitStack` 否决理由确认,但只对一半:**
   实测 3 个 0.05s 回调 `aclose()` 耗时 0.156s —— 纯串行、无并发,design 的否决成立;
   但「出错继续」它其实做得到(中途抛错后其余回调照常执行完),只是异常形态为
   单异常 + `__context__` 链,不是 ExceptionGroup。否决理由应精确为「无并发」一条。

5. **`asyncio.TaskGroup` 被证死,必须写进内核文档禁用:**
   实测一个 effect 立即抛错 → TaskGroup 取消兄弟任务 → 慢 effect 在第一个 disposer
   之前收到 CancelledError,**disposer 全部丢失**(log 只剩 `['slow-CANCELLED']`)。
   拆除路径只能用 `gather(return_exceptions=True)`。

### 推荐实现骨架(已验证可运行)

```python
class Effect:
    async def dispose(self) -> None:
        pending = self._disposers[::-1]; self._disposers.clear()
        for fn in pending:                      # 同 effect 内:逆序串行,fail-fast(Cordis 保真)
            r = fn()
            if inspect.isawaitable(r): await r  # 异常直接上抛,剩余跳过

async def _unload(self) -> None:                # fiber 级:并发,错误隔离
    results = await asyncio.gather(*(e.dispose() for e in effects), return_exceptions=True)
    for eff, r in zip(effects, results):
        if isinstance(r, BaseException):
            logger.error("effect %s 拆除失败", eff.name, exc_info=r)
            # 默认吞掉(Cordis 语义);显式 await fiber.dispose() 的入口可改为聚合上抛
```

### 未验证的注意点

- 拆除协程自身被外部取消时,`except Exception` 拦不住 CancelledError,会丢剩余 disposer。
  实现期需决定 teardown 是否 `asyncio.shield` 或按「不可取消任务」约定处理。本次未做原型。

---

## Q2 epoch 变更合并(design 2.4)

### 实测数据(4 策略 × 3 场景,数值为 唤醒次数 / 重载次数)

| 场景 | immediate(基线) | call_soon+标志 | create_task 每次 | Event+worker |
|---|---|---|---|---|
| burst3:同一轮内连换 3 次 | 3 / **3** | 1 / **1** | 3 / 1 | 1 / 1 |
| spread3:3 次换代间各隔一次 await | 3 / 3 | 3 / **2** | 3 / 2 | 2 / 2 |
| flap:同轮拔掉又原样插回 | 2 / **2** | 1 / **0** | 2 / 0 | 1 / 0 |

三种延迟策略终态全部一致(无丢更新)。

### 结论

1. **design 2.4 的核心断言成立**:「一轮内 provider 换 3 次 → 只重载 1 次」在
   call_soon 方案下实测为真;顺带白送 flap 吸收(同轮拔插净零变化 → 0 次重载,
   即 Cordis 微任务合并的完整收益)。
2. **但表述必须修正:`loop.call_soon` 本身不合并任何东西**(三次 notify 就排三个回调)。
   合并是四件事共同作用:①延迟到本轮批末(call_soon 排的回调在当前批执行完后才跑);
   ②`_scheduled` 标志防重复排队(Cordis `Promise.resolve().then` 前面那个 `??=` 的对应物);
   ③flush 时按 epoch 对账,而不是按通知次数行动;
   ④重载耗时窗口内的新变更由「单飞 + 脏位」的 reconcile 循环吸收。
   第④件 design 完全没提,却是防丢更新的必要件(实测正是它保证了 spread3 终态一致)。
3. **合并范围 = 同一事件循环轮次。** 跨 `await` 的 3 次换代实测重载 2 次
   (第一次重载只见到变更 1,第二圈吸收 2+3)—— 不是缺陷,但要写进文档,
   避免把「合并」误解成任意时间窗防抖。
4. **方案对比**:create_task-每次也能收敛到 1 次重载(靠③④),但每次通知多分配一个
   Task(唤醒 3 vs 1);Event+worker 行为等价,代价是每 fiber 一个常驻任务与关闭时序。
   **call_soon + 标志 + 单飞脏位是最优解**,维持 design 选型。

### 推荐实现骨架(已验证)

```python
def notify(self):
    if self._scheduled: return
    self._scheduled = True
    loop.call_soon(self._flush)

def _flush(self):
    self._scheduled = False
    self._dirty = True
    if not self._reconciling:
        loop.create_task(self._reconcile())

async def _reconcile(self):
    self._reconciling = True
    try:
        while self._dirty:                      # 重载期间的新变更由下一圈吸收
            self._dirty = False
            if (target := self._compute_epoch()) != self._active_epoch:
                await self._unload_and_load()   # 含依赖快照
                self._active_epoch = target
    finally:
        self._reconciling = False
```

---

## Q3 五种事件分发(design 2.6)

五种全部实现于 `q3_bus.py`(104 行,不含过滤谓词/优先级/退订记账),场景全 PASS。
唯一插曲:parallel 测试首跑断言失败 —— 不是语义问题,是断言字符串没料到 add_note 里的
监听器名是完整 qualname(闭包场景为 `t_parallel.<locals>.fail_fast`),修正断言后通过。

### parallel:「哪个监听器挂的」保得住,且有两条通道

`gather(return_exceptions=True)` 后按位置 `zip(listeners, results)`,给每个异常
`add_note(f"listener={qualname} event={event}")`,聚合 `ExceptionGroup` 上抛。实测输出同时保住:

- **原始 traceback 帧**:gather 返回的异常对象自带 `__traceback__`,能看到监听器内具体 raise 行;
- **note 归属行**:`listener=t_parallel.<locals>.fail_fast event=e`。

allSettled 语义实证:首监听器立即抛错,0.03s 的慢监听器仍执行完成后才聚合上抛。

### waterfall:「不调 next 即短路」在 async 链里自然成立

实现:索引递归 + 每层现造 `next_` 闭包,发射方以 `final=` 传入链尾真操作。实测:

- **短路**:中间件不调 `next_` → `final` 与后续层都不执行;外层中间件照常收尾,
  且其「后处理」作用在否决值上(koa 风格环绕语义);
- **中途异常**:沿 await 链向外冒;「前面已执行的部分」不需要框架回滚 ——
  与同步调用栈语义一致,每个外层拿到一次 try/finally(收尾)或 try/except(降级补偿)
  的机会,两种模式都实测成立;
- **防呆**:同一中间件重复调 `next_()` 抛 RuntimeError(4 行代价,建议进内核);
- `next_(*new_args)` 支持改写下游参数,不传则透传当前参数(实测)。

### emit / serial / bail 的三个实现决定(design 未写,必须定)

1. **emit 遇到异步监听器怎么办**:原型做法是 `ensure_future` fire-and-forget +
   done_callback 记日志 + 总线持强引用防任务被 GC(裸 create_task 的著名坑)。可行,
   但热路径(`assistant/chunk` 每 token 一次)每次分配 Task 不可接受 ——
   建议热路径事件在词汇层禁挂异步监听器,或 emit 与 bail 同待遇直接拒绝。
2. **bail 拒绝异步监听器**可实现:检测到 awaitable → `coroutine.close()`(防
   RuntimeWarning)+ TypeError,实测有效。
3. **serial/bail 胜出条件**:实现为「非 None 且非 False」;实测 `0` 胜出且后续监听器
   未执行,与 Cordis「非 null/false/undefined」对齐(`0`/`""` 都算胜出)。
   这条要写进事件词汇文档,否则「返回 0 表示不拦截」类误解必然出现。

过滤谓词(隔离域即事件边界)本次未做原型 —— 属直白的按标签比较,风险低,如实标注未验证。

---

## Q4 显式 ctx vs contextvars(design 2.2)

### 显式版实测(q4_explicit.py)

- 调用形态 `ctx.tools.register(ctx, tool)`:能用,双 ctx 视觉冗余但可读。
- **两类误用运行时零报错、静默泄漏**(实测):
  - 误用① 服务作者顺手用 `self.ctx` → 注册挂到 provider 的 fiber,consumer 卸载不撤销;
  - 误用② 调用方把服务自己的 ctx 递回去(`registry.register(registry.ctx, ...)`,
    `self.ctx` 就在手边,是最顺手的错误)→ 同样泄漏。
- **`_own_effect` / `_caller_effect` 两类误用都挡不住** —— 它们只是命名卫生:
  误用①的作者本来就选错了方法,误用②发生在调用方,辅助方法根本不在场。
  design 13.1 风险 4 的缓解「静态检查注册类方法首参必须是 caller: Context」对误用②无效
  (递错的也是 Context 类型,类型检查全绿)。唯一运行时兜底是 A1⑤ 的计数归零断言。

### contextvars 版实测(q4_contextvars.py,五条路径)

内核在调插件 apply 前 `current_ctx.set(plugin_ctx)`、finally reset;注册 API 内部
`caller = current_ctx.get()`,不再收 caller 参数。

| 路径 | 实测结果 |
|---|---|
| (a) apply 内直接调 `registry.register(tool)` | 通过:自动归属正确,调用点比显式少一个参数 |
| (b) apply 里 spawn 的后台任务,apply 返回、token 已 reset 之后才注册 | 通过:`create_task` 创建时拷贝上下文,父帧 reset 不影响 |
| (c) 事件监听器内注册(分发发生在发射方上下文) | 有条件通过:naive 分发挂错到发射方;总线注册时捕获 owner、分发时 set/reset 重绑后正确,约 6 行内核内部代码,插件无感 |
| (d) 常驻 worker + 队列边界 | 失败(预期):天然断链,静默挂到 worker 宿主;须入队捕获 + 出队恢复。显式 ctx 在这里同样要把 ctx 塞进消息 —— 两方案负担相同 |
| (e) 生命周期外裸调注册 API | 通过:抛 `LookupError`,失败很响(显式版对应场景是静默挂错,更差) |

### 结论(如实说,这会部分推翻 design 2.2)

- **「不复刻 getTraceable 透明代理」维持** —— contextvars 方案同样不需要任何代理。
- **但「注册类 API 显式收 ctx」不再是必然选择。** contextvars 在 (a)(b)(c)(e) 四条路径上
  **结构性消除**了显式版仅存的两类误用面(design 自认「唯一靠纪律的点」),只剩 (d)
  队列边界与显式版完全同责。代价:归属不在签名里(隐式);总线分发每监听器一次
  set/reset(ContextVar 是 C 实现,开销可忽略);内核多约 20 行。
- 推荐落点:**混合签名** `def register(self, tool, *, ctx: Context | None = None)`,
  缺省取 `current_ctx.get()`,显式传参留给队列接力等断链场景。这样 design 13.1 的风险 4
  从「靠纪律」降级为「仅队列边界靠纪律」,A1⑤ 照常兜底。
- 描述符方案判断为死路(未做原型):描述符只知宿主实例,拿不到调用方身份,除非
  `sys._getframe` 栈检查 —— 脆弱且慢,不建议投入。

---

## Q5 类型化服务查找(design 2.3)

### 实测(mypy 2.3.1 / pyright 1.1.411,Python 3.14;PEP 695 语法运行时实测可执行)

| 变体 | mypy 推导 | mypy 告警 | pyright 推导 | pyright 告警 |
|---|---|---|---|---|
| v1 `require(type[T])`,T=ABC 服务类(design 原写法) | `ShellService` 正确 | **error: type-abstract** | 正确 | 无 |
| v2 同签名,非 ABC 类 | `PromptService` 正确 | 无 | 正确 | 无 |
| v3 `require(str)` 重载 | `Any` 正确 | 无 | 正确 | 无 |
| `get(type[T]) -> T \| None` | `ShellService \| None` 正确 | error: type-abstract | 正确 | 无 |
| v4 泛型令牌 `Definition[T]` | `ShellService` 正确 | 无 | 正确 | 无 |

### 结论

- **design 2.3 的核心成立:两个检查器都能从 `ctx.require(ShellService)` 推出精确返回
  类型**(含 `get` 的 `T | None`),无需 `.pyi`。
- 唯一障碍:mypy 认为 `type[T]` 意味着可实例化,对传 ABC 报 `type-abstract`
  (python/mypy#4717 一系,DI 框架的常见摩擦点);pyright 1.1.411 零告警。
- 项目现状:pyproject.toml **未配置任何类型检查器**,决定权开放。三选一:
  1. 选 pyright → design 写法原样可用(最省);
  2. 选 mypy → 配置 `disable_error_code = ["type-abstract"]`(DI 场景通行做法,粒度全项目);
  3. Definition 类不用 `@abstractmethod`(方法体 `raise NotImplementedError`)→
     两个检查器都干净,代价是丢失「provider 少实现方法在实例化时报错」的 ABC 拦截。
- 泛型令牌 v4 虽零告警,但引入平行对象、与 `inject = [ShellService]` 的既定形状冲突,不推荐。

可用的泛型签名(实测两个检查器都按预期工作):

```python
class Context:
    @overload
    def require[T: Service](self, definition: type[T]) -> T: ...
    @overload
    def require(self, definition: str) -> Any: ...
    def require(self, definition): ...          # 运行时读 SERVICE_NAME / 字符串查槽
    def get[T: Service](self, definition: type[T]) -> T | None: ...
```

---

## Q6 规模估算复核(prd:内核本体 1500–2500 行)

写完 1002 行原型后的体感:**下限 1500 明显乐观,上限 2500 接近但偏紧;建议改为 2000–3000。**

依据:

1. 本次原型只覆盖语义面里「容易的一半」且**零鲁棒性** —— 没有 Context 派生链
   (extend/isolate/intercept)、没有隔离标签槽位与可见性绑定、没有六状态机全集、
   没有配置校验与 loader diff、没有过滤谓词、没有任何重入保护 —— 就已 1002 行。
2. Cordis 行数分布(wc 实测):**fiber.ts 754**(最难)/ reflect.ts 418 / events.ts 352 /
   registry.ts 337 / utils.ts 287 / logger.ts 270 / context.ts 146 / service.ts 115 / index.ts 14。
   Python 能省的:logger(标准库,-270)、getTraceable(约 -120..230)、schemastery
   接口层(Pydantic 白送)。合计省约 500,余约 2200 行 TS 当量。
3. Python 侧要**加回来**的:Q2 的单飞+脏位 reconcile、Q4 的总线重绑与 contextvars 装配、
   Pydantic 集成、中文 docstring(项目规范;Cordis 的 JSDoc 同样占行,大致抵消)。
   最被低估的是 fiber 生命周期的重入语义:Cordis 仅 `effect()` 单函数就约 150 行
   (setup barrier、卸载中登记抛 INACTIVE_EFFECT、disposer 幂等、并发 join 在途清理),
   这些在 asyncio 里一个都省不掉,而原型一行都没写。
4. 折算:fiber(状态机 + epoch + effect 生命周期)约 700–900 行,其余模块合计约
   1100–1600,落点 2000–2800;加安全余量报 **2000–3000**。

---

## 对 design.md / prd.md 的修正建议

### design 2.1(Context)

- 本次未验证派生链与服务解析(原型只覆盖注册/effect 面),无修正意见。
- 仅建议把 Q5 的 overload 签名写进 2.1 接口草案:当前
  `def require[T](self, definition: type[T]) -> T` 缺字符串重载与 `T: Service` bound。

### design 2.2(不复刻 getTraceable / 显式 ctx)—— 核心修正

- 「不做透明代理」维持;「所有注册类 API 显式收调用方 ctx」建议改为
  **contextvars 缺省 + 显式参数覆盖**(`register(tool, *, ctx=None)`)。
  实证:显式版两类误用(服务作者用 self.ctx / 调用方递错 ctx)运行时零报错静默泄漏,
  `_own_effect/_caller_effect` 与「静态检查首参」都挡不住(误用②传的就是 Context 类型);
  contextvars 把误用面缩到仅剩队列边界,而该边界显式方案同样靠纪律。
- 若坚持纯显式方案:2.2 中「让误用在阅读时刺眼」应降级为「命名卫生」,并明确
  唯一运行时兜底是 A1⑤ 计数断言。
- 采用 contextvars 时内核须承接两条义务:插件 apply 前 set / finally reset;
  总线分发前按监听器注册时捕获的 owner 重绑(约 6 行)。

### design 2.3(类型化服务查找)

- 可行性成立;但必须补一条检查器决策:pyright 零成本;mypy 需
  `disable_error_code = ["type-abstract"]` 或 Definition 类弃用 @abstractmethod。
  项目当前未配任何检查器,P1 引入时定下来并写进内核文档。

### design 2.4(Fiber 与 epoch)

- 「用 loop.call_soon 合并」机制成立(burst3 实测 1 次重载),但表述改为四件套:
  **call_soon 延迟 + `_scheduled` 防重 + flush 时 epoch 对账 + 单飞脏位 reconcile**;
  第四件 design 未提,是防丢更新的必要件(重载窗口内的换代由 while 下一圈吸收)。
- 补两句边界:合并范围=同一事件循环轮次(跨 await 的 3 连换实测重载 2 次,属预期);
  同轮「拔掉又原样插回」净零变化 → 0 次重载(抖动吸收,实测)。

### design 2.5(effect 与卸载顺序)—— 核心修正

- 「顶层并发、effect 内逆序串行」实证成立(0.096s vs 串行 0.15s);AsyncExitStack
  否决维持,但理由精确为「无并发」一条(其错误延续性合格);
  **补一条:禁用 asyncio.TaskGroup 做拆除**(实测兄弟任务被取消、disposer 丢失)。
- **必须补错误策略**:Cordis 真实语义是 effect 内 fail-fast(失败跳过同链剩余)+
  fiber 级逐 effect 隔离、logger.error 吞掉不上抛(卸载常由 epoch 驱动,没有自然
  调用方接异常)。建议照搬:默认记日志;仅显式 `await fiber.dispose()` 提供聚合
  ExceptionGroup 的变体。同步修正 `research/cordis-kernel-spec.md` 公理 5 的
  「逆序执行全部 disposer」表述(同 effect 内失败时不成立)。
- 补一条未决事项:teardown 自身被取消(CancelledError)的处置(shield 或约定),本次未验证。

### design 2.6(事件总线)

- 五种分发全部可实现(104 行原型);parallel 的 ExceptionGroup + add_note 归属保真
  实证成立(原始 raise 帧 + note 双通道)。
- 注意:「失败聚合为 ExceptionGroup」只适用于 parallel 分发路径;**拆除路径不聚合上抛**
  (见 2.5 修正),两处不要混用同一句话。
- 需补三个实现决定:① emit 对异步监听器的策略(fire-and-forget 须持强引用防 GC;
  热路径事件建议直接禁挂异步监听器);② bail 运行时拒绝异步监听器(TypeError +
  `coroutine.close()`);③ serial/bail 胜出条件精确为「非 None 且非 False」
  (`0`/`""` 胜出),写进词汇文档。
- waterfall 建议加 next 重复调用防呆(4 行);过滤谓词本次未验证,如实标注。

### prd R-K-1..7

- R-K-1 / R-K-2 / R-K-6:本次未原型验证(派生链、槽位可见性、Pydantic 校验),无修正。
- R-K-3:核心机制已验证;「合并同轮 notify」的表述与 design 2.4 的四件套说法对齐。
- R-K-4:「顶层并发拆除、单个 effect 内部逆序串行」保持,**补错误语义半句**:
  「同 effect 内失败跳过剩余(fail-fast),fiber 级隔离收集、记日志不上抛」。
- R-K-5:成立;serial 补「非 None/False 胜出」的精确定义;parallel 的
  「allSettled 聚合」保持。
- R-K-7:「改为注册类 API 显式收 ctx 参数」建议改写为「不复刻透明代理;调用方归属经
  contextvars 传播,注册 API 保留显式 ctx 覆盖参数」。
- 规模预期:**1500–2500 → 2000–3000**(Q6 论证)。

### 验收标准 A1(七项)

- A1③(换代触发重载):建议扩为三个断言 —— 换代重载 1 次;**同轮 3 连换仅 1 次重载**;
  **同轮拔插净零变化 0 次重载**(Q2 实测行为,正是 design 说「最容易漏掉的一条」的量化形态)。
- A1⑤(计数归零):维持,并升格说明 —— 无论显式还是 contextvars 方案,这条都是
  「effect 挂错 fiber」类误用的**唯一运行时兜底**(Q4 实测两类误用静默无报错)。
- A1⑥(五种分发 + waterfall 短路):可测性已由 q3 原型证明;建议断言加两条:
  「waterfall 短路时链尾真操作未执行」「parallel 失败聚合可定位到具体监听器」。
- A1①②④⑦:无修正。

## Caveats / Not Found

- 未验证:Context 派生链与服务槽位可见性(R-K-1/2)、配置校验(R-K-6)、事件过滤谓词、
  teardown 被取消的语义。前三者属直白实现、风险较低;第四者需在 P1 实现期决策。
- 检查器结论基于 mypy 2.3.1 与 pyright 1.1.411;版本升级可能改变 type-abstract 行为。
- 耗时断言(0.096s / 0.156s)依赖 sleep 精度,为本机单次实测,量级结论稳定、具体数值会漂。
- 原型文件已从 `/tmp/kernel_proto/` 归档到 **`research/kernel-prototype/`**（9 个文件，共 1002 行），
  可直接作为 P1 实现的起点；报告中的骨架代码段即摘自这批跑通的原型。
