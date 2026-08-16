# 插件化架构改造（一切皆插件）

> ## ⛔ 已作废（2026-08-15）
>
> 用户于 2026-08-15 决定**作废本任务**。任务已归档，但**从未进入实施阶段**
> （作废时 `status` 仍为 `planning`，无任何代码改动落地）。
>
> **注意**：Trellis 的 `task.py archive` 会把 `status` 强制写成 `completed`，
> 那是脚本行为，**不代表本任务完成**。真实状态以 `task.json` 的
> `meta.cancelled = true` 与本横幅为准。
>
> **本文及同目录下所有文档（含 `design.md`、`implement.md`、`research/` 15 份调研）
> 不作为任何实施依据。** 其中 `prd.md` 决策 D3 关于
> `08-14-统一大模型调用入口` 的处置结论（「代码吸收、方案作废、任务归档」，
> 判定 `llm/factory.py` / `create_llm_client(scenario)` / `settings.llm_provider`
> 作废不实现）**一并失效**——该结论的前提是本插件化方案会落地，前提既已不成立，
> 统一大模型调用入口任务按其自身 `prd.md` / `design.md` 继续推进。
>
> 保留归档仅为留存 15 份调研资料与内核原型，供日后重启该方向时参考。

## 目标

把 AgentOS 从「按 feature 域分层的单体 FastAPI 应用」改造为「微内核 + 能力 seam + 声明式组合树」的
Agent Harness，使模型、工具、技能、会话、沙箱、存储、循环、调度等能力均可由配置组合、自由替换与重组，
对标 DeepSeek Harness（dsh）的 Cordis 架构。

用户已明确：**不需要为现有功能与代码做任何兼容和妥协，可以随意改造。**

## 一、dsh 是怎么做到「一切皆插件」的（分析结论）

源码：`/Users/xianglei/Desktop/deepseek-harness`（TS monorepo，约 50 包，Cordis v4.0.0-rc.7
vendored 于 `vendor/cordis/src`，**全部只有 2693 行 / 9 文件**）。详见 `research/` 下四份 dsh 报告。

结论：靠三件事，缺一不可 —— **内核给「能替换」，纪律给「换了不炸」，组合层给「自由重组」**。

### 1. 一个对 Agent 一无所知的微内核（2693 行）

只提供 5 个原语：Context 派生 / Fiber 生命周期 / Service 槽位 / 事件总线 / 可逆 effect。
`docs/architecture.zh.md:13`：「不存在需要打补丁的特权内核：扩展 dsh 的方式是把插件挂载到其他插件旁边，
而各项注册都是副作用，会在其插件卸载时撤销。」agent loop 自身也只是一个插件。

五公理（`docs/cordis-primer.zh.md:9-13`，实测细节见 `research/cordis-kernel-spec.md`）：
插件是可调用体 + 元数据 → 上下文按 `ctx.<key>` 查服务而非 import 实现 → `inject` 声明依赖、
**加载顺序由依赖表达而非手动编排** → 类型化事件（primer 写四种分发，`events.ts:165-243` 实为
**五种**：emit/parallel/serial/bail/waterfall）→ **注册是可逆副作用**，reload/teardown 自动撤销。

其中真正的机关是 **epoch 字符串**（`fiber.ts:611-623`）：依赖满足性编码为各提供方实例 uid 的拼接，
provider 出现 → consumer 自动启动；provider 消失**或换代** → consumer 自动卸载并自动重载。
这让「运行时替换」不需要任何插件配合。

### 2. 一条被 CI 与文档生成器强制的纪律

**能力 seam = Service Definition + Provider + Consumer 三角色**（`docs/glossary.zh.md:9`、
`architecture.zh.md:104`）：「seam 是完整能力，绝不是其中一个角色。」
范例：`shell`（定义）/ `bash-local`、`bash-sandbox`、`pwsh-local`（提供方）/ `tool-bash`（消费方）。

铁律：**扩展插件依赖 Definition，绝不依赖具体 provider**。`tool-bash` 编译期只 `import type {}`
（零运行时代码），运行期靠 `inject = ['tools','shell',...]` 从 ctx 取实例。
`docs/capability-seams.zh.md` 列出全部 `ctx.*` 并标注角色：`core`（主干）/ `seam`（26 个可替换能力）/
`bundle`（组合点，**只有 `ctx.agentLoop` 一个**）。

seam 接口都极小 —— 整个沙箱能力只有一个方法 `confine(argv, policy)`。因为小，所以
「替换一个提供方即改变整个产品」（`architecture.zh.md:106`）：fs 与 subprocess 共享同一执行世界，
两者一起指向 e2b，Bash/PTY/LSP 一并搬进远程沙箱，**无需为每个工具做 provider 专用 fork**。

### 3. 一个声明式组合层：Profile + Bundle + patch

运行中的 dsh 是一棵插件树，由启动时按序叠加的各层 YAML patch 组成（`architecture.zh.md:15-37`）。
**没有插件注册中心，也没有运行时扫描** —— 发现 = pnpm 依赖闭包 + `package.json` 的 `dsh.bundle` 字段。
patch 按 `id` 定位条目、整体替换 `config`、`disabled` 关行、`insert` 加行。
`dsh --profile web --dump-config` 打印实际配置树，**打印出的任何条目都可被用户 patch 替换**。
关键细节：**行序不带加载语义**（base patch 原文：activation is service-availability driven）。

「自由重组」发生在这一层，不在代码层。三个平面各自可整包替换：进程平面（bundle）/
agent 平面（preset）/ 浏览器平面（`dsh.client` + 浏览器里的第二个 Cordis Loader）。

### 4. 两条使能不变量

- **模型可见即已记录**（`architecture.zh.md:96-100`）：抵达模型请求的一切都必须能从会话日志重建，
  由运行时不变量断言。历史用 `deriveMessages()` 从事件日志投影，循环近乎无状态 ——
  **压缩、fork、恢复、回放因此全部免费**。
- **两层扁平作用域**（`glossary.zh.md:13-19`）：注册要么全局、要么属于恰好一个 scope key；
  活跃 agent 即其自身 scope 的 key；shadowing = 最具体者胜出。

### 5. 最反直觉的一点：循环不是靠「策略接口」可插拔的

dsh **没有 LoopStrategy 接口**。`ReactLoopAgent` 是朴素三层嵌套 while（`kick→turn→step`），
契约是 `dsh-agent` 拥有的**事件词汇**，`dsh-agent-loop` 只是唯一具体实现且**任何包都不许依赖它**
（模块图印证：依赖它的只有示例包）。
生命周期词汇（`architecture.zh.md:71-86`、`agent-lifecycle.zh.md`）：
`turn/start` → `agent/pre-step`(waterfall) → `step/start` → `system-prompt/assemble`(waterfall)
→ `agent/request`(waterfall) → `llm/stream`(waterfall) → `assistant/chunk*` → `tool/call*`
→ `tools/pre-execute` → `tools/execute` → `tools/post-execute` → `tool/result*` → `step/end`
→ `agent/turn-stopping`(serial) → `turn/end`。
waterfall 监听器不调 `next()` 即短路否决 —— **这就是策略层的可重排中间件**。

于是：plan-execute = 挂 `agent/pre-step` 的监听器；多轮目标续跑 = 监听 `agent/status` 后调
`followup()`；subagent = 同一工厂递归造 agent；压缩 = 挂 `agent/pre-step`(压力) 与
`agent/request-error`(供应商确认溢出) 两个监听器。
`docs/cookbook/extension-cookbook.zh.md:99` 断言：钩子、压缩、plan mode、subagent、MCP、skill、
记忆、cron、UI、权限、热重载全部如此，「**没有任何一行修改循环本身**」。

## 二、AgentOS 现状基线

完整调查见 `research/agentos-baseline.md`。规模 ~32000 行 Python（不含 alembic 3820 行）。

**三个必须先处理的阻塞现实**（已实测）：

1. **工作区当前 import 即失败**：`llm/client.py` 已删除但 7 个调用方仍引用
   （`runtime/agent.py:32`、`runtime/subagent.py:21`、`runtime/compact.py:6`、
   `memory/recall/service.py:13`、`memory/jobs/{service.py:14,dream.py:17,extraction.py:15}`），
   `uv run python -c "import runtime.agent"` → `ModuleNotFoundError: No module named 'llm.client'`。
   这是在途任务 `08-14-统一大模型调用入口` 的断点。三处补充事实（已实测，决定 P0-1 的做法）：
   ① **`llm/factory.py` 计划中但未创建**，Phase 3 未执行；
   ② **构造签名有鸿沟**：新 `AnthropicClient.__init__` 的 `model` 是**必填位置参数**
      （`llm/anthropic_client.py:20-27`），而 7 处调用方全是**零参** `LLMClient()`
      （`runtime/agent.py:113` 等），仅改 import 名会从 `ModuleNotFoundError` 变成 `TypeError`；
   ③ `import llm` 本身**已经成功**（`llm/__init__.py:7` 导出
      `BaseLLMClient / AnthropicClient / OpenAIClient`），断裂面精确地只有那 7 行。
2. **该分支的全部工作未提交**：`git rev-parse HEAD main` 同为 `e872cdc`，`git log main..HEAD` 为空 ——
   `feature/unify-llm-client` **相对 main 零 commit**。`llm/{base,anthropic_client,openai_client}.py`
   共 **332 行是 untracked 新文件**，`llm/client.py` 的删除也只存在于工作区。
   **一次 `git clean -fd` 或误切分支即全部丢失**，而这 332 行正是 P2 的起点素材（见 D3）。
3. **测试已全部删除**：`tests/` 不存在，全仓 0 个 `test_*.py`（提交 e872cdc 删了 25 文件 / 15250 行），
   而 `pyproject.toml:26-28` 仍配 `testpaths=["tests"]`。**改造期间没有回归网**。

各 feature 包规模：

| 包 | 行数 | 包 | 行数 | 包 | 行数 |
|---|---|---|---|---|---|
| memory | 6752 | tools | 3134 | runtime | 2477 |
| session | 1467 | group | 1343 | interaction | 1157 |
| workspace | 1050 | core | 1037 | department | 988 |
| agent | 910 | skill | 875 | team | 809 |
| llm | 559 | user | 498 | auth | 458 |
| permission | 441 | task | 387 | plan | 367 |
| database | 224 | hooks | 243 | prompt | 206 |
| api | 121 | | | | |

**与 dsh 的十处结构性差距**（每一处都是本次改造要跨的沟）：

| # | 差距 | 证据 |
|---|---|---|
| 1 | 无插件机制、无 DI 容器；`Depends` 只在 API 层 | 30 个文件直接 `XxxService(db)` |
| 2 | 循环是 1712 行上帝类，压缩/权限/plan-mode/队友唤醒全焊死在里面 | `runtime/agent.py:552-702` 及其 10 处硬编码依赖 |
| 3 | 存在**两份**同构循环，逻辑已漂移 | `runtime/subagent.py:330-391`（不落库、无审批） |
| 4 | Anthropic 消息格式贯穿全栈 | `tools/base.py` 的 `to_param()→ToolParam`；`session_messages.content` JSONB 直存 Anthropic 块 |
| 5 | **无事件溯源**：真源是关系表，`StreamEvent` 不落库，无法重放 | `core/event_bus.py:11-50` 进程内 Queue，不持久化 |
| 6 | 全局单例：`settings`（22 文件 import，且 import 期即被消费）、hook `_registry`、`engine` | `core/config.py:143`、`tools/subagents/registry.py:97` |
| 7 | 静态装配不可逆：路由、26 个工具、SubAgent SPECS 全在 import 期冻结 | `api/router.py:3-38`、`tools/registry.py:89-125` |
| 8 | 单一 ORM registry + 单条 Alembic 链（21 版本线性），**插件无法自带表** | `database/registry.py:7-37` |
| 9 | 封闭 enum 阻断扩展 | `tools/subagents/definition.py:34-40` `AgentType` |
| 10 | 无 OS 沙箱（只有静态命令白名单，文件头自述「不是 OS 级沙箱」）、无调度器、无 MCP（`pyproject.toml:23` 声明零 import） | `tools/shell_policy.py` |

**可复用的已有雏形**（不必从零）：`ToolRegistry`、`HookRegistry`（4 事件点、fail-open、60s 超时）、
`SubAgentSpec` 注册表、Agent 实体（DB 级提示词/模型/工具白名单）、Skill 系统（唯一运行时上传通道）、
`BaseLLMClient` ABC、`Executor`/`ObjectStorage` ABC、四层优先级权限规则。

## 三、需求

分四层描述。**范围已定为 L0–L3 全量**（决策记录见第六节）：L0→L2 是「一切皆插件」的最小完整闭环，
L3 是 AgentOS 独有的多租户与开放增量 —— dsh 没有对应物，因为它是单进程单用户本地工具。

### L0 内核（新包 `kernel/`，Cordis 的 Python 子集）

- **R-K-1 Context 与派生**：`ctx.<key>` 解析服务；支持 `extend` / `isolate(name, label)` /
  `intercept(name, config)` 三种派生，子节点遮蔽、父链不可变。
- **R-K-2 Service 槽位**：以 `(名字, 隔离标签)` 为键的全局槽；同隔离域重复 provide 同名服务必须抛错；
  服务可见性绑定提供方 fiber 的 ACTIVE 状态。
- **R-K-3 Fiber 生命周期与 epoch 依赖驱动**：状态机
  `PENDING→LOADING→ACTIVE→FAILED/UNLOADING→DISPOSED`；依赖满足性编码为提供方**实例代**的拼接；
  provider 出现/消失/换代自动驱动 consumer 启停重载，**插件自身无需感知任何一维**。
- **R-K-4 可逆 effect**：一切副作用（事件监听、服务注册、子插件挂载）统一经 `ctx.effect()` 登记，
  卸载即撤销；顶层 effect 并发拆除、单个 effect 内部逆序串行。
  **错误语义**：同一 effect 内某个 disposer 失败即跳过该链剩余（fail-fast）；
  fiber 级逐 effect 隔离、收集后记日志**不上抛**（卸载常由 epoch 驱动，无自然调用方接异常）。
- **R-K-5 事件总线五种分发**：`emit` / `parallel`(allSettled 聚合) / `serial`(首个胜出) /
  `bail`(同步 serial) / `waterfall`(环绕中间件，不调 `next` 即短路)；
  **`serial`/`bail` 的胜出条件精确为「非 `None` 且非 `False`」**（`0` 与 `""` 算胜出）；
  边界不靠总线分区，靠分发时携带的过滤谓词，默认按隔离标签比较（**隔离域即事件边界**）。
- **R-K-6 配置校验**：每次激活前用 Pydantic v2 校验插件 config，失败即 FAILED，
  **绝不带残缺配置启动**；更新走「校验 → 可否决的 update waterfall → restart」。
- **R-K-7 明确不复刻**：Cordis 的 `getTraceable` 透明代理与 HMR。
  **调用方归属经 contextvars 传播，注册 API 保留显式 `ctx=` 覆盖参数**用于队列接力等断链场景
  （纯显式参数方案已被原型证伪，见 `research/kernel-python-prototype.md` Q4）。
  JS→Python 逐项映射与取舍理由见 `research/cordis-kernel-spec.md`。

规模预期：内核本体 **2000–3000** 行 Python（Cordis 为 2693 行 TS，Pydantic v2 直接顶掉 schemastery）。
原估 1500–2500 已按原型实证上修：只覆盖「容易的一半」且零鲁棒性的原型就已 1002 行，
而 Cordis 最难的 `fiber.ts`（754 行重入语义）一行未动。

### L1 能力 seam 化（工作量主体）

- **R-S-1 三角色纪律**：每个 seam 必须同时产出 Definition（拥有 `ctx.<key>` 与词汇类型）、
  至少一个 Provider、至少一个 Consumer。**只交付其中一个角色不算交付这项能力。**
- **R-S-2 依赖方向可静态检查**：消费方只 `inject` 服务名、只依赖 Definition 的类型，
  **禁止 import 任何具体 provider**；用 import-linter 或等价工具在 CI 强制。
- **R-S-3 循环去上帝化**：`runtime/agent.py`(1712 行) 与 `runtime/subagent.py`(同构第二份) 合并为
  **一个** agent-loop 插件，只依赖事件词汇，且**任何其它包不得依赖它**。
  压缩、权限裁决、plan-mode、交互挂起、队友唤醒全部改为挂在词汇上的监听器；
  **循环体内不得出现任何工具名字符串特判**（消灭 `agent.py:58-75, 800-901`）。
- **R-S-4 事件词汇**：定义并冻结一套 AgentOS 生命周期事件（对齐 dsh 的 turn/step/tool 词汇），
  逐个标注分发模式；词汇由 `agent` Definition 包拥有，不由循环实现拥有。
  现有契约全量清单见 `research/agentos-event-contract.md`（10 种 EventType、18 处发射点、SSE 帧），
  新词汇必须一并补上其中**六处**缺口：
  ① **每次模型调用的 token 用量**（现仅存最后一次且不落库）——
     需新增 `llm/response` 事件，因为 `agent/request` 在调用前发射、用量只在流耗尽后才有；
  ② **事件时间戳与会话内单调序号**（由写者显式取 `last_seq + 1`，
     `UNIQUE(session_id, seq)` 仲裁并发；DB 自增列只作物理主键，不承担顺序语义）；
  ③ **SSE 帧的 `id:` 与断点续传**（`id:` 即会话内 `seq`）；
  ④ **子代理中间过程与 actor/run_id 归属**；
  ⑤ **异常路径必发终结事件**；
  ⑥ **助手消息内容的重建真源**：增量流不足以重建
     （`redacted_thinking` 被错标为文本、`signature_delta`/`citations_delta` 被丢弃），
     真源必须是每次调用的最终内容块，`assistant/chunk` 仅供 UI 渲染、不作落库依据。
- **R-S-5 会话日志成为唯一真源**：引入 append-only 会话事件日志，模型历史由日志投影得出；
  建立并断言不变量「**模型可见即已记录**」；中止时为未启动的工具调用写合成错误结果，
  保证日志里 call/result 永远配对、可回放。
  **追加必须幂等**（事件构造期身份 `event_id` + 双唯一约束）；
  **并发写者由 `UNIQUE(session_id, seq)` 仲裁，冲突者让位** ——
  不写这半句，「BIGSERIAL + 无幂等」的错误实现同样能通过字面上的 R-S-5。
- **R-S-6 首批 seam 清单**（每项都要三角色齐活，共 18 项）：
  `llm`（顺带修复断裂的 import）/ `tools` / `sessionLog`（含跨实例读侧扇出）/
  `sessionPersistence` / `systemPrompt` / `approval` / `compaction` / `skills` /
  `subagents`（打破封闭 `AgentType` enum）/ `storage` / `subprocess` / `shell` / `fs` /
  `sandbox` / `jobs` / `memory` / `hooks` / `api`。
  `schedule` 全仓零承载者零消费者，属纯新增能力而非能力 seam 化，**已并入 `jobs`**（决策 D12）；
  跨实例事件不另设 seam，作为 `sessionLog` 的 provider 维度（design 5.8）。
- **R-S-7 去全局单例**：`settings`（22 文件 import）/ hook `_registry` / `_executor_instance` /
  `engine` / `core/storage.py:152` 的 `_storage_instance` 五个模块级单例改由服务槽提供；
  **消灭 import 期读配置**（`tools/subagents/registry.py:97`、
  `memory/jobs/extraction.py:103-106` 的类定义期配置冻结）；
  **`main.py:60-61` 的「import 即 `create_app()`」**同属本条验收对象。
- **R-S-8 消息格式脱耦**：会话与工具层不再直存/直产 Anthropic 内容块，
  改用 provider 中立的内部表示，Anthropic 形态由 `llm` provider 负责翻译。

### L2 声明式组合层

- **R-C-1 组合描述**：插件树由声明式条目（`id`/`name`/`config`/`disabled`/`isolate`）组成；
  Loader 本身也是插件，把条目 diff 成 mount / unmount / update 三种操作作用于注册表。
- **R-C-2 分层 patch**：多层 patch 按序叠加（后者胜），按 `id` 定位、整体替换 config、可禁用可插入；
  **行序不带加载语义**，激活由服务可用性驱动。
- **R-C-3 组合可见**：提供 `dump-config` 等价命令，打印带来源注释的实际插件树而不启动；
  **打印出的任何条目都可被 patch 替换**。
- **R-C-4 启动断言**：启动完成时全部条目 fiber 必须 ACTIVE，否则列出未激活条目并失败退出。
- **R-C-5 可插拔资产随插件走**：FastAPI 路由、工具注册、提示词片段随插件挂载/卸载生效，
  消灭 `api/router.py:3-38` 与 `tools/registry.py:89-125` 的静态聚合。

### L3 多租户与开放（AgentOS 独有，dsh 无对应物）

- **R-T-1 三级作用域**：进程级根树（部署期装配）/ 租户或 workspace 级 realm / 会话级 isolate 派生；
  明确每级能覆写什么、不能覆写什么。
- **R-T-2 Agent 实体即 preset**：DB 中的 Agent 配置成为 agent 平面的组合来源
  （对应 dsh 的磁盘 preset 目录），会话创建时挂载，失败则回滚会话创建。
  这把现有 Agent 实体从「只能收窄内建集合」升级为「能引入新实现」。
- **R-T-3 运行时可变**：新增或修改 Agent 组合无需重启进程即生效。
- **R-T-4 第三方代码插件（分公民等级）**：「一切皆插件」精确化为
  **组合机制对所有等级统一、能力词汇按等级裁剪**。四级：
  T0 平台内建（同进程、全能力）/ T1 可信第三方（同进程、**仅管理员部署期装配**）/
  **T2 租户协议插件（R-T-4 的正主）**：进程外 MCP server，经受信 stub 参与组合，
  只提供工具/提示词/资源、只订阅白名单通知型事件，**不得注册任意 ctx 服务、不得挂
  waterfall/serial/bail、无 DB 直连** / T3 租户一次性脚本（收编现有 skill 通道）。
  T2/T3 必须配套 OS 级沙箱、资源限额与来源校验，且**禁止安装期执行代码**。
  论证见 `research/third-party-plugin-isolation.md`。
- **R-T-5 跨进程事件**：**PostgreSQL 事件日志是唯一真源，Redis Streams 只做扇出与热区续传**；
  任一实例均可订阅同一会话的事件流，Redis 不可用时退化为 DB 轮询而非丢事件。
  写序为「先落库、后 XADD」，Redis 中的 ID 与日志 `seq` 对齐，使订阅方只需维护一个整数游标。

### 横切要求

- **R-X-1 数据层归属**：解决「插件自带表」与「单条线性 Alembic 链」的冲突
  （现状：`database/registry.py:7-37` 单一 metadata + 21 个线性 version）。
  采用**程序化多环境**：每插件独立迁移目录 + 独立 `MetaData` + 独立版本表
  `alembic_version_plugin_<name>` + 强制表名前缀，核心链原样不动；
  装 = 对该插件环境 `upgrade head`，卸 = `downgrade base`。
  必配 `include_name` 前缀白名单防 autogenerate 误删，禁止插件之间的外键。
  论证见 `research/alembic-per-plugin-migrations.md`。
  **数据层有第二处缺口**：`session/repository.py:24-38` 的 `_SESSION_CHILD_MODELS` 把
  「会话级联软删除」硬编码为 10 张别域表的清单，前提是「子表在编译期可枚举」——
  插件自带的带 `session_id` 子表会**静默漏删**。须由 `sessionPersistence` 提供清理登记扩展点，
  随插件 effect 挂载/撤销。（该清单本身也违反 CLAUDE.md 第 8 条，正好一并收编。）
- **R-X-2 回归网先行**：当前全仓 0 个测试。任何破坏性改造启动前必须先建立最小可运行基线，
  否则无法判断「改造后行为等价」。
- **R-X-3 测试范式**：不 mock 内核、不 monkey-patch —— 起真 Context + 真服务栈，
  Fake 通过与生产同一扩展点注册，使测试拓扑与生产拓扑同构。
- **R-X-4 敏感信息**：插件配置、`dump-config` 输出、事件日志与错误响应中不得出现
  密码、Token、密钥或完整连接串。

## 四、验收标准

以「可观察结果」表述，逐条对应需求。

**A1（对应 R-K-1..7）内核可独立验证**
在不加载任何 AgentOS 业务插件的前提下，用一组内核单测证明：
① provider 插件挂载后 consumer 自动从 PENDING 转 ACTIVE；
② provider 卸载后 consumer 自动 UNLOADING 并在 provider 重新挂载后自动重载；
③ provider **换代**（同名新实例）触发 consumer 重载 1 次；
   **同一事件循环轮次内连换 3 次仍只重载 1 次**；
   **同轮内「拔掉又原样插回」净零变化则重载 0 次**（抖动吸收，原型实测行为）；
④ 同隔离域二次 provide 同名服务抛错，不同 isolate 域则各自成功且互不可见；
⑤ 卸载后该插件登记的全部事件监听、服务、子插件均已撤销（计数归零）。
   **这条是「effect 挂错 fiber」类误用的唯一运行时兜底** ——
   无论显式传 ctx 还是 contextvars，误挂都不会在挂载期报错；
⑥ 五种分发模式行为符合定义，waterfall 不调 `next()` 即短路，
   且**短路时链尾的真操作未执行**；**parallel 的失败聚合能定位到具体监听器**；
⑦ 配置校验失败的插件进入 FAILED 且未产生任何副作用。

**A2（R-S-3、R-S-4）循环可被替换而不改任何消费方**
提供第二个循环实现（哪怕是「单步不带工具」的最小实现），仅改组合条目的一行 `name:` 即可切换，
业务插件与工具零改动；`grep` 证明除组合文件外无任何模块 import 具体循环实现。

**A3（R-S-1、R-S-2、R-S-6）seam 三角色完备**
每个已交付 seam 都能列出 Definition / Provider / Consumer 三者；
CI 的依赖检查在「消费方 import 具体 provider」时失败。
至少两个 seam 具备**两个可切换的 provider**，且切换只改组合文件。

**A4（R-S-5）日志即真源**
分两档执行（成本差两个数量级，不可混为一谈）：
- **L1 生产常开**：每次请求发送前，对「日志投影结果」与「实际请求体」做**归一化哈希**比对
  （统一 JSON 键序与空白），单次 1~5ms，不一致则**在发送前中断本次调用**并必发 `turn/end`，
  绝不带着不一致继续跑；
- **L2 CI/影子环境**：逐字节比对（含键序与浮点格式），单次 0.5~23s，只在 CI 与影子流量上跑。

另断言：中止一次含并发工具调用的步骤后，日志中每个 `tool/call` 都有配对的 `tool/result`
（未启动者由**接管者**写入合成错误结果，因为原写者可能已崩溃）。

**A5（R-C-1..4）组合可见且可替换**
`dump-config` 能打印完整插件树及每行来源层；对其中任意一行做 `disabled`、`config` 替换或
`insert` 新行，重启后行为按预期改变；缺失依赖时启动失败并明确列出未激活条目名。

**A6（R-S-7、R-C-5）静态装配已消灭**
`grep` 证明：不存在模块级 `settings = Settings()` 式全局单例被业务代码直接 import；
路由与工具集合来自插件挂载而非硬编码列表；import 期不再读取配置。

**A7（R-X-2、R-X-3）回归网存在且范式正确**
`uv run pytest` 可执行且通过；至少覆盖一条完整会话主路径（用户输入 → 工具调用 → 结果 → 结束）
与一条失败路径；测试用真 Context + 从正规扩展点注入的 Fake，不含对内核的 monkey-patch。

**A8（R-X-4）敏感信息不外泄**
`dump-config` 输出、事件日志与 API 错误响应中不含密码、Token、密钥或完整连接串。

**A9（R-T-1、R-T-2、R-T-3）租户级组合可差异化**
两个不同租户在同一进程内加载**不同的**插件组合且互不干扰（一方的服务对另一方不可见）；
修改某 Agent 的组合后新会话立即生效且无需重启进程；会话结束后其派生子树的全部注册均已撤销。

**A10（R-T-4）第三方插件受控**
租户自助安装的插件（T2/T3）**全部运行在宿主进程之外**：`grep` 证明不存在「租户上传物被同进程
import」的代码路径。恶意插件无法读取其它租户数据、无法拿到 DB/Redis/MinIO 凭证、
无法注册 ctx 服务或挂 waterfall 否决点；超出 CPU/内存/时间限额被终止；默认无出网；
来源校验（hash + 版本 pin）失败则拒绝安装；上传物的任何安装期钩子都不被执行。

**A11（R-T-5）事件跨实例**
同一会话的事件流可从任一应用实例订阅到；**续传数据源是 PostgreSQL 事件表**（Redis 只是热区加速），
故实例重启、Redis 清空或订阅方断线均不丢已落库事件。
断线重连（携带 `Last-Event-ID`）后收到的事件序列**既不缺号也不重号**，
且断线本身不中止正在运行的会话。

**A12（R-X-1）插件数据层可装卸**
带表的插件安装后其表存在、卸载后其表与版本表被清除，**全程不触碰核心链**
（`alembic_version` 与 `alembic/versions/` 无变化）；在插件 A 已安装的前提下，
对插件 B 跑 autogenerate **不产生任何针对 A 的 DROP**；插件迁移与核心迁移一样有
`alembic_version_history` 审计记录。
另断言：一个带 `session_id` 子表的插件挂载后，软删除该会话，**插件表中的对应行同样被软删除**；
卸载该插件后，级联清单中不再残留它的登记。

## 五、不在范围内

- **UI / 前端插件平面**：dsh 的「UI 即插件」依赖浏览器里的第二个 Cordis Loader，
  而 AgentOS 当前仓库**没有前端工程**（纯后端 API + SSE）。本次只保证后端事件与能力可被前端消费，
  不建设前端插件运行时。
- **HMR / 开发期热重载**：Python 下收益成本比远低于 TS（R-K-7）。
- **npm 式插件分发与安装 CLI**：不做包管理器转发器、不做插件市场。
- **完整复刻 dsh 的 26 个 seam**：只做 R-S-6 首批清单；e2b 远程执行世界、LSP、workflow 引擎、
  终端 PTY、web 检索等不在首批。
- **Cordis `getTraceable` 透明代理语义**：不复刻；调用方归属改由 contextvars 传播（R-K-7、D9）。
- **路由变更的跨实例广播**：动态挂卸能力必做，但多实例一致性走滚动重启（D8）。
- **`schedule` 独立 seam**：并入 `jobs`（D12）；`after`/`at`/`every` 词汇待 B2 后台唤醒需求出现再展开。
- **业务功能变更**：本任务不新增/修改任何面向最终用户的业务能力，只改结构。
  多租户、权限、记忆等域的**业务规则**保持既有语义。

## 六、已决决策

| # | 决策 | 结论 | 依据 |
|---|---|---|---|
| D1 | 是否为现有代码保留兼容层 | **否**，可任意重写 | 用户原话「不需要为当前项目已经实现的功能和代码做任何的兼容和妥协」 |
| D2 | 改造范围切在哪一层 | **L0–L3 全量**（内核 + seam 化 + 组合层 + 多租户与开放） | 用户于范围提问中明确选择 |
| D3 | 在途任务 `08-14-统一大模型调用入口` 如何处置 | **代码吸收、方案作废、任务归档**（三分处理，不是整体丢弃）：① 已写的 332 行 `BaseLLMClient` ABC + 双 provider 实现**吸收为 P2 的起点**；② `llm/factory.py` / `create_llm_client(scenario)` / `settings.llm_provider` 全局开关**作废不实现**；③ scenario 档位语义**保留**，改由 provider 的插件 config 承接 | 已落地的 332 行方向与 P2 同构（`llm/base.py:10` 的 `BaseLLMClient(ABC)` 恰是 P2-1 的 `LlmAdapter`，`anthropic_client.py:19`/`openai_client.py:17` 恰是两个 provider，且 `openai_client.py:73-81` 的 `stream` 未实现与 P2-3 的判断吻合），丢掉纯属浪费；但工厂方案是**配置驱动的静态 if-else 选型**，正是 seam 要替换的东西 —— 它直读全局 `settings` 撞 R-S-7，`llm or create_llm_client(...)` 的手工 DI 撞 R-S-1（consumer 应 `inject` 而非自行兜底），provider 选择在 seam 形态下由组合条目的一行 `name:` 决定。另：该任务的 `architecture-after-refactor.md`(505 行) 是**早期过时快照**（场景级 provider、主 Agent 硬编码不走工厂、保留 `LLMClient` 别名，三条均与其自身 prd/design 相反），**不作为任何实施依据**；其 `implement.md:957-965` 的「完成标志」9 项全 `[x]` 与同文件 21 项 Review 全 `[ ]`、19 条验收全 `[ ]` 矛盾，勾选状态不可信 |
| D4 | 是否重建被删除的 25 个旧测试 | **否**。旧测试针对即将被重写的结构，重建即浪费；改为**为新架构写新测试**，内核单测优先（A1），再补会话主路径（A7） | R-X-2 的目的是「有回归判据」，不是「恢复历史文件」 |
| D5 | UI 插件平面是否纳入 | **否** | 仓库无前端工程；后端只保证事件与能力可被前端消费 |
| D6 | 租户第三方插件能否同进程运行 | **否**。「一切皆插件」精确化为「插件分公民等级」：组合机制统一，能力词汇按等级裁剪；租户自助安装的只能是进程外形态（T2/T3） | 进程内隔离不可信 Python 代码无现实路径（RestrictedPython 自述非沙箱且有逃逸 CVE、audit hooks 是检测非防护、进程内 seccomp 会杀宿主），同进程一行代码即可读 `settings.jwt_secret_key`（`core/config.py:115,142-143`）。**dsh 抄不到作业** —— 其插件是全信任代码，沙箱全部针对模型产物 |
| D7 | 插件迁移链走多 base 还是多环境 | **多环境**（每插件独立版本表），核心链不动 | 官方对两条路线的分界原文正好对应需求：多 base 为「cross-dependencies」设计并共享版本表，多环境为「completely independent revision lineages」设计。插件恰恰要**禁止**跨链依赖，多 base 的唯一独占优势 `depends_on` 用不上，却要照付 `upgrade head` 永久失效等全部成本 |
| D8 | 动态挂卸的路由在多实例下如何一致 | **不做跨实例广播**：路由只随进程级根树变化，生产环境的进程级组合变更走滚动重启。动态挂卸能力仍必须实现，但不作为线上产品能力承诺 | 现状 `main.py:59` 单进程（`uvicorn.run` 无 `workers`），而 R-T-5/A11 要求多实例；design 6.4 已定「路由不做租户级差异化」，两者合起来使路由表对所有实例同构，广播的收益为零、复杂度全留 |
| D9 | 注册类 API 如何拿到调用方 ctx | **contextvars 缺省 + 显式 `ctx=` 覆盖**（混合签名），不复刻 `getTraceable` 透明代理 | 纯显式方案的卖点是「让误用刺眼」，但原型实测两类误用（服务作者用 `self.ctx`；调用方把服务自己的 ctx 递回去）**运行时零报错、静默泄漏**，且「静态检查首参必须是 `Context`」对第二类无效——递错的值也是 `Context`。混合签名在四条路径上结构性消除误用，只剩队列边界靠纪律（该边界显式方案同样靠纪律），严格优于原方案。见 `research/kernel-python-prototype.md` Q4 |
| D10 | 单写者互斥用什么机制 | **乐观追加**：`UNIQUE(session_id, seq)` + 写者显式取 `last_seq+1`，冲突即栅栏；租约行作效率层；**禁用会话级 advisory lock** | ① `.env` 指向 Supabase 事务模式池（6543），PgBouncer 官方对事务池下的会话级 advisory lock 明确标 Never；② SQLAlchemy 归还连接只 rollback，不发 `DISCARD ALL`/`pg_advisory_unlock_all()`，锁会随连接泄漏给下一个租户；③ 事务级锁跨不过一个 Turn 的分步 commit（`agent.py:177/204/581/737`）。PG 是唯一互斥源，Redis 不增加任何保证 |
| D11 | 是否引入类型检查器 | **不强制引入**。design 2.3 的 `overload` 签名在 pyright 1.1.411 下零告警；若将来引入 mypy，配 `disable_error_code = ["type-abstract"]` 即可 | 项目 `pyproject.toml` 当前未配任何检查器；实测两个检查器都能从 `ctx.require(ShellService)` 推出精确类型，无需 `.pyi`。唯一摩擦是 mypy 对 ABC 传 `type[T]` 报 `type-abstract`（DI 框架通病），属可配置项而非设计缺陷 |
| D12 | `schedule` 是否进首批 seam | **并入 `jobs`**，不单列 | 全仓零承载者、零消费者：无 cron/调度框架，唯一排队的消费者是尚未实现的「B2 后台自动唤醒」（`runtime/agent.py:1669` 自述）。为空能力写两个 provider 只会产出假验收；而 `jobs` 的 provider 素材（lease / `FOR UPDATE SKIP LOCKED` / 退避 / 幂等键 / 优雅停机）在 `memory/jobs/repository.py:227-315` 已完整存在 |

D3–D12 若与你的判断不同，请直接指出，我改回来再继续。

## 七、待决问题

**阻塞性问题：无。** 范围与决策已收敛（D1–D12），`design.md` / `implement.md` 已成文。

评审时需你点头的**两项**（均不阻塞规划，但都不由我单方决定）：

1. **顶层包扩充**：seam 三角色分属独立顶层包后，顶层包从 **22 个涨到约 60 个**（design 3.2）。
   推荐**扁平顶层包**（与 dsh 的 `packages/` 同形，import-linter 契约最直白）；
   备选是收进 `plugins/` 根命名空间，但那更接近「按层划分」，与 CLAUDE.md 的既有约定冲突。
2. **`08-14-统一大模型调用入口` 的收尾**（决策 D3 已定处置方式，但**归档动作**与**保存现场**要你授权）：
   该任务状态为 `in_progress`，2880 行文档中 `architecture-after-refactor.md` 已过时作废、
   工厂方案按 D3 不实现，仅 332 行代码作为 P2 起点被吸收。建议**归档该任务**，
   并在归档前先 `git add` 三个 untracked 新文件提交一次留档（**不 push**）——
   否则那 332 行随时可能被 `git clean` 抹掉。
