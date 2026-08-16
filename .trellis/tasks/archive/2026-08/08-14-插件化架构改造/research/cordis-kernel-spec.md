# 研究：Cordis 内核机制（Python 复刻规格）

来源：`/Users/xianglei/Desktop/deepseek-harness/vendor/cordis/src/`（v4.0.0-rc.7，2693 行 / 9 文件）。

## 一、七条不可再拆的公理

1. **插件 = 可调用体 + 元数据**（`name` / `inject` / `Config` / `provide`）。身份即该可调用体本身；
   注册表是 `Map[callable, Runtime]`。函数、类、带 `apply` 的对象三种形态归一（`registry.ts:222-228`）。
   **多实例是默认语义**：每次 `ctx.plugin(p, cfg)` 无条件新建一个 Fiber（v3 的 `reusable` 标志已删除）。
   单例性不由框架保证，而由**服务名占用**保证——同隔离域重复 provide 同名服务直接抛错（`reflect.ts:289-291`）。

2. **上下文是原型链节点**。每个插件拿到的是 `parent.extend({fiber: self})`，即
   `Object.create(父ctx)` + 唯一自有属性 `fiber` 指向自己（`fiber.ts:236`）。
   所有注册 API 内部从 `this.ctx.fiber` 取归属，**副作用记账因此结构性正确，而非靠纪律**。
   `extend` / `isolate` / `intercept` 三种派生均只在子节点遮蔽映射表，父链不可变（`context.ts:99-145`）。

3. **服务 = 以（名字, 隔离标签）为键的全局槽位**（`reflect.ts:209, 277-305`）。
   写入者是某个 fiber，**可见性与该 fiber 的 ACTIVE 状态绑定**（`fiber.ts:588-594`）——
   服务在提供方 apply 完成后才对依赖方就绪。只有提供方能 `set`；注销必须唤醒依赖方并等其卸载完成。

4. **依赖满足性编码为 epoch 字符串**（各提供方实例 uid 的拼接，`fiber.ts:611-623`）：
   ```ts
   for (const name of Object.keys(this.inject)) {
     const impl = this._store[name]
     if (!impl) { epoch = INACTIVE; break }
     epoch += ':' + impl.fiber.uid      // 依赖“提供方实例代”，而非仅名字
   }
   ```
   epoch 每次变化单向驱动状态机：`PENDING → LOADING → ACTIVE → UNLOADING →（DISPOSED | 回 LOADING）`。
   提供方出现→消费者自动启动；提供方消失**或换代**→消费者自动卸载并自动重载。
   **这就是「时空可组合性」：空间=组合树位置与隔离域，时间=服务出现/消失时刻，插件自己无需感知任何一维。**
   加载期快照 `this.store = {...this._store}`（`fiber.ts:647`）保证插件活动期内看到的依赖实例恒定。

5. **一切副作用都是 effect**：立即执行的主体返回 disposer，disposer 归属当前 fiber，
   卸载 = 逆序执行全部 disposer（`fiber.ts:415-561, 675-696`）。
   事件监听、服务注册、子插件挂载全部建立在这一个原语上。
   因此「卸载干净」「热重载」「配置重启」是同一操作（unload→load）的三种触发方式。
   注意：**顶层 effect 之间并发清理，单个 effect 内部逆序串行**——有顺序要求的拆除必须放进同一个 effect。

6. **事件总线全局唯一，五种分发契约**（`events.ts:165-243`），模式是事件公开约定的一部分：

   | 方法 | 语义 |
   |---|---|
   | `emit` | 同步广播，忽略返回值 |
   | `parallel` | 并发 + `allSettled`，失败聚合为 AggregateError |
   | `serial` | 依次 await，首个非 null/false/undefined 值胜出并停止 |
   | `bail` | serial 的同步版 |
   | `waterfall` | 环绕中间件，末参 `next`；不调 `next` 即短路否决 |

   边界不靠总线分区，靠**分发时 thisArg 携带的过滤谓词**（`events.ts:165-175`）；
   `Service` 内置过滤器按隔离标签比较（`service.ts:61-63`）——**隔离域即事件边界**。
   框架自身生命周期也走同一总线（`internal/status`、`internal/service`、`internal/update` 等），具备自反性。

7. **配置在每次激活前经外部校验协议验证**（Standard Schema `~standard.validate`，`fiber.ts:50-62`）。
   校验失败 = 加载失败（FAILED），**绝不带残缺配置启动**。
   更新流程：校验 → 可否决的 `internal/update` waterfall → restart。
   Cordis 核心不绑定具体校验库；harness 用 vendored schemastery。

## 二、加载器只是又一个插件

`vendor/loader` 把声明式 YAML 条目（`id` / `name` / `config` / `disabled` / `isolate`）
diff 成 mount / unmount / update 三种操作作用到注册表（`loader/src/config/entry.ts:142-246`）：
- 仅 `config` 变 → `fiber.update()`（过 waterfall 后 restart，插件可监听 `internal/update` 免重启热更）
- `name`/`inject`/`group` 变 → dispose 旧 fiber、重 import、启新 fiber，失败回滚
- `disabled` → dispose 但保留 entry

HMR（`vendor/hmr/src/index.ts:400-549`）= 清模块缓存 + 重 import + registry 级 dispose/replay
（**保留原父 ctx 与原 config**），失败恢复模块缓存并重装旧插件。

## 三、JS/TS 特性 → Python 等价方案

| Cordis 依赖 | 用途 | Python 替代 | 难度 |
|---|---|---|---|
| `Proxy` (`reflect.ts:135`) | ctx 未知属性→服务解析、set 保护 | `__getattr__` / `__setattr__` | 低（Python 更自然） |
| 原型链 `Object.create` | O(1) 派生、父不可变、子遮蔽 | `ChainMap` 或显式 `parent` 指针逐级查找 | 低 |
| TS `declare module` 声明合并 | 类型层扩展 Context/Events | 无运行时等价；`.pyi` / `Protocol` 或放弃静态面 | — |
| 闭包 disposer + generator effect | 可回收副作用 | 返回 callable / `@contextmanager` / `AsyncExitStack` | 中（需自实现并发清理版，ExitStack 是纯串行） |
| `Symbol(name)` 隔离标签 | 不可伪造的作用域键 | `object()` 哨兵实例（身份比较） | 低 |
| 动态 `import()` + 缓存清除 | 热重载 | `importlib.reload` + `del sys.modules[...]`；发现用 `importlib.metadata` entry_points | 中高 |
| Promise 微任务合并（`fiber.ts:650`） | 批量吸收连锁 notify，避免抖动 | `loop.call_soon` / `await asyncio.sleep(0)` 同轮合并 | 中 |
| PromiseLike Fiber (`registry.ts:331`) | `await ctx.plugin()` 等加载完成 | 实现 `__await__` 或 `fiber.wait()` 协程 | 低 |
| `WeakMap` / `WeakSet` | 不阻止回收的记账 | `weakref.WeakKeyDictionary` | 低 |
| **`getTraceable` 上下文重绑定**（`utils.ts:117-233`） | 服务方法内 `this.ctx` 指向**调用方**，副作用记账到调用者 | **最难复刻**。务实做法：`contextvars.ContextVar('current_ctx')` 在调用边界压入调用方 ctx；或注册 API 显式收 `ctx` 参数 | 高 |
| Standard Schema | 校验器解耦 | **Pydantic v2 直接替代**（AgentOS 已有） | 低 |

## 四、Python 复刻的取舍建议

- **`getTraceable` 建议不复刻**：改为「注册类 API 显式传 ctx」。代价是 API 略啰嗦，
  收益是省掉整套透明代理，且 Python 里透明代理性能与调试成本都很高。
- **`declare module` 无需替代**：Python 属性本就动态；类型面用 `Protocol` + `.pyi` 补，
  或接受运行时校验（服务缺失时抛明确错误，Cordis 本身也是运行时抛错）。
- **HMR 建议延后**：开发期热重载在 Python 里收益/成本比远低于 TS，非 MVP 必需。
- **Pydantic v2 白送公理 7**：`model_validate` 直接就是 Standard Schema 的等价物。
