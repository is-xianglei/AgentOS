# 执行计划

> ⛔ **已作废（2026-08-15）**：本任务经用户决定作废，下列 P0–P7 阶段**一个都没有执行**。
> 归档时 `status` 被 `task.py archive` 强制写为 `completed`，那是脚本行为，
> 不代表完成。**本文不作为任何实施依据**，详见同目录 `prd.md` 顶部横幅。

范围 L0–L3 全量，按 `design.md` 第十二节的 P0–P7 推进。**每个阶段结束时系统必须可运行、
可验证、可回滚**；阶段之间打 git 标记点。

约定：
- 每个阶段开头先 `git switch -c` 或打 tag，作为该阶段的回滚点。
- 每条勾选项都要有对应的验证命令，不能只靠「看起来对」。
- 不自动 `git push`（用户全局约定）。

---

## P0 解封：让工作区能跑，建立行为等价判据

**为什么必须最先做**：当前 `uv run python -c "import runtime.agent"` 直接 `ModuleNotFoundError`，
连基线都跑不出来，后续任何「改造后行为等价」的判断都无从谈起。

- [ ] **P0-0 先保存现场**（其余全部步骤的前置）：`feature/unify-llm-client` 相对 main **零 commit**，
      `llm/{base,anthropic_client,openai_client}.py` 共 332 行是 **untracked** 新文件，
      `llm/client.py` 的删除也只在工作区。这 332 行是 P2 的起点素材（D3），
      **一次 `git clean -fd` 就没了**。开工前先 `git add` 这三个文件并提交一次留档。
      **不 `push`**（用户全局约定）。
- [ ] **P0-1** 修复 `llm.client` 断裂的 7 处 import
      （`runtime/agent.py:32`、`runtime/subagent.py:21`、`runtime/compact.py:6`、
      `memory/recall/service.py:13`、`memory/jobs/{service.py:14,dream.py:17,extraction.py:15}`）。
      按决策 D3，这里只做**最小接线**让 import 通过，不做 `llm/factory.py` 的原方案设计 ——
      真正的 `llm` seam 在 P2 重做。
      **注意不是改 7 行 import 就完事**：新 `AnthropicClient.__init__` 的 `model` 是必填位置参数
      （`llm/anthropic_client.py:20-27`），而 7 处调用方全是零参 `LLMClient()`
      （`runtime/agent.py:113`、`runtime/subagent.py:66`、`runtime/compact.py:29`、
      `memory/recall/service.py:58`、`memory/jobs/{service.py:188,dream.py:224,extraction.py:215}`），
      只改名会把 `ModuleNotFoundError` 换成 `TypeError`。
      **做法**：`model` 改为 `str | None = None` 并在构造内回落 `settings.anthropic_model`，
      7 处 import 改为 `from llm import AnthropicClient`、调用点零参不动。
      合计 7 行 import + 1 处签名 + 若干类型标注，**不新增任何 P2 要删的文件**。
      （`complete_structured` 签名与旧版完全一致、`stream` 只是类型标注从 SDK 类型改成
      `dict[str, Any]`，调用点均无需改动。）
      `AGENTOS_ANTHROPIC_MODEL` 为空时的失败信息要说人话，别抛裸 SDK 异常。
- [ ] **P0-2** 重建 `tests/` 目录（`pyproject.toml:26-28` 已配 `testpaths=["tests"]`，无需改配置）。
- [ ] **P0-3** 写一条端到端会话主路径测试作为**行为等价判据**：
      用户输入 → 模型返回工具调用 → 工具执行 → 结果回灌 → 结束。
      模型侧用 Fake，但必须走真实的 `AgentRuntime` 入口，不 monkey-patch 内部方法。
- [ ] **P0-4** 写一条失败路径测试（工具抛错 / 模型返回错误）。
- [ ] **P0-5** 确认 `.env` 中的 PostgreSQL 可连通；不可连通则**报告配置项名称与错误**，
      不自行安装或改地址（CLAUDE.md 配置与本地环境第 5、6 条）。

**验证**：
```bash
uv run python -c "import runtime.agent, runtime.subagent, runtime.compact"
uv run pytest -q
```

**回滚点**：P0 只增不改结构，风险最低。失败直接 revert。

---

## P1 内核：`kernel/` 独立开发

**纪律**：本阶段**不允许触碰任何业务代码**。`kernel/` 里不得出现 session、tool、llm、prompt
等业务词（design 第一节边界 1）。

**起点**：`research/kernel-prototype/` 已归档 9 个跑通的原型（1002 行），
Q1–Q5 的骨架代码可直接摘用；`research/kernel-python-prototype.md` 记录了对应的实测数据与反例。

- [ ] **P1-0** 定类型检查器（design 2.3 / 决策 D11）：默认 **pyright**（design 2.3 的
      `overload` 签名零告警）；若选 mypy 则必须配 `disable_error_code = ["type-abstract"]`。
      不选也可以，但结论要写进 `kernel/README.md`，别留给后来人试。
- [ ] **P1-1** `kernel/context.py`：`Context` 与三种派生（`extend` / `isolate` / `intercept`），
      `__getattr__` 服务解析走父链，缺失抛 `ServiceNotProvided`。隔离标签用 `object()` 哨兵。
      `require` / `get` 用 design 2.3 的 `@overload` 签名（`type[T]` 与 `str` 两路）。
- [ ] **P1-2** `kernel/service.py`：`Service` 基类 + `provide()`；同隔离域重复 provide 同名服务抛错。
      **注册类 API 一律 `def register(self, x, *, ctx: Context | None = None)`**：
      缺省取 `current_ctx.get()`，显式参数留给队列接力等断链场景（design 2.2、决策 D9）。
      内核承接两条义务：插件 `apply` 前 `current_ctx.set()` / finally reset；
      事件分发前按监听器注册时捕获的 owner 重绑（约 6 行）。
- [ ] **P1-3** `kernel/fiber.py`：状态机与 **epoch 计算**（design 2.4）。
      **本阶段最容易出错的一条**：provider 换代（同名新实例）也必须触发 consumer 重载。
      加载期做依赖快照；抖动抑制用**四件套**（缺第四件会丢更新）：
      `loop.call_soon` 延迟 + `_scheduled` 防重排队 + flush 时按 epoch 对账 + **单飞脏位 reconcile**。
      合并范围仅限**同一事件循环轮次**，跨 `await` 的连续换代不合并（预期行为，写进文档）。
- [ ] **P1-4** `kernel/effect.py`：**顶层 effect 并发拆除、单个 effect 内部逆序串行**。
      不要用 `contextlib.AsyncExitStack`（**纯串行**，实测 3×0.05s 回调耗时 0.156s）；
      **禁用 `asyncio.TaskGroup`**（实测一个 effect 抛错会取消兄弟任务，disposer 整组丢失）。
      只能 `asyncio.gather(..., return_exceptions=True)`。
      **错误策略两层**：effect 内某 disposer 失败 → 跳过该链剩余（fail-fast，与 Cordis 一致）；
      fiber 级逐 effect 隔离、`logger.exception` 记录后**不上抛**
      （卸载常由 epoch 驱动，没有自然调用方接异常）。仅显式 `await fiber.dispose()` 提供聚合变体。
      **未决**：teardown 自身被取消（`CancelledError`）的处置（shield 还是约定不可取消）——
      实现期定，别默认 `except Exception` 了事。
- [ ] **P1-5** `kernel/events.py`：五种分发 `emit`/`parallel`/`serial`/`bail`/`waterfall`；
      过滤谓词机制，`Service` 默认按隔离标签比较。**`parallel` 失败聚合为 `ExceptionGroup`
      并给每个异常 `add_note("listener=... event=...")`**（实测原始 traceback 帧与 note 双通道都在）。
      三个必须定的细节：① `emit` 的异步监听器 fire-and-forget 需**持强引用**防 Task 被 GC，
      热路径事件（`assistant/chunk` 每 token 一次）在词汇层**禁挂异步监听器**；
      ② `bail` 运行时拒绝异步监听器（`TypeError` + `coroutine.close()` 防 RuntimeWarning）；
      ③ **`serial`/`bail` 的胜出条件是「非 `None` 且非 `False`」**，`0` 与 `""` 算胜出 ——
      写进词汇文档，否则「返回 0 表示不拦截」的误解必然出现。
      `waterfall` 加 `next()` 重复调用防呆（4 行）。
      注意：ExceptionGroup 只用于 `parallel`，**拆除路径不聚合上抛**（P1-4），两处别混。
- [ ] **P1-6** `kernel/loader.py` 的插件协议部分：函数插件读 `name`/`inject`/`Config`/`apply`，
      类插件读 `Config`；`inject` 同时接受字符串与 Definition 类（读 `.SERVICE_NAME`）。
      配置用 Pydantic v2 校验，失败即 FAILED 且**不产生任何副作用**。
- [ ] **P1-7** 内核单测覆盖 A1 的七项子验收①–⑦，**不加载任何业务插件**。
      ③ 要写满三条断言（换代 1 次 / 同轮 3 连换 1 次 / 同轮拔插 0 次）；
      ⑥ 要断言「waterfall 短路时链尾真操作未执行」与「parallel 失败可定位到具体监听器」。
- [ ] **P1-8** 写 `kernel/README.md`：contextvars 归属约定与显式 `ctx=` 的适用场景、
      effect 顺序与错误约定、事件词汇归属约定、`serial`/`bail` 胜出条件、类型检查器结论。
      **队列边界是唯一靠纪律的点**（入队捕获 + 出队恢复），必须有文档可指；
      运行时兜底只有 A1⑤ 的计数归零断言。

**验证**：
```bash
uv run pytest tests/kernel -q            # A1 七项
uv run pytest -q                          # P0 基线仍绿（业务代码零改动，必须绿）
```

**回滚点**：`kernel/` 是新增目录，删除即回滚，对现有代码零影响。

---

## P2 首条竖切：只做 `llm` 一个 seam

**这是整个改造的风险闸门**。一个 seam 走完三角色会暴露内核设计的全部问题
（显式 ctx 够不够用、epoch 重载会不会误伤、服务持 sessionmaker 顺不顺手）。
**P2 不顺就回头改内核** —— 此时代价可控；跳过 P2 直接铺开其余 17 个 seam，问题会在 P3 中段集中爆发。

**起点不是空白**（决策 D3）：`08-14-统一大模型调用入口` 已落地 332 行且方向同构，直接吸收 ——
`llm/base.py:10` 的 `BaseLLMClient(ABC)`（`stream` + `complete_structured` 两方法）就是 P2-1 要的
`LlmAdapter`；`llm/anthropic_client.py:19`（204 行）与 `llm/openai_client.py:17`（83 行）就是两个
provider 主体。**但工厂方案不吸收**：`llm/factory.py`、`create_llm_client(scenario)`、
`settings.llm_provider` 全局开关一律不实现，provider 选择改由组合条目的 `name:` 决定。
scenario 档位（agent / subagent / compact / memory_*）语义保留，落到 provider 的插件 config。
该任务的 `architecture-after-refactor.md` 是过时快照，**不要读它**。

- [ ] **P2-1** `llm/` 升格为 Definition 包：`LlmRuntime`（注册表）+ `LlmAdapter`(ABC)。
      `max_tokens` 从硬编码（`AnthropicClient` 的 131072）改为配置项，
      同步更新 `.env` / `.env.example` / `core/config.py`（CLAUDE.md 配置同步规则）。
- [ ] **P2-2** `llm_anthropic/` provider：迁入现有 `AnthropicClient`，
      并把 `AnthropicStreamTranslator`（`llm/types.py:118-220`）一并迁入 —— 翻译是 provider 的职责。
      同批必须处理四件（依据 `research/anthropic-sdk-adaptation.md`）：
      ① **`AsyncAnthropic` 实例由 effect 持有并在卸载时 `await client.close()`**
      （现状 `llm/anthropic_client.py:47` 每次构造新客户端、`:121` 用完即弃，连接池反复重建）；
      ② 补 **`redacted_thinking` 块的映射**（现被错标为普通文本，导致重放时内容失真）；
      ③ `llm/anthropic_client.py:149` 的 `temperature=0` 硬编码改为**按模型族门控**的配置项
      （部分模型不接受该参数与 thinking 并存）；
      ④ `max_retries` / `timeout` 提取为插件 config，别继续吃 SDK 默认值。
      新增配置项同步改 `.env` / `.env.example` / `core/config.py`。
- [ ] **P2-3** `llm_openai/` provider：现状 `stream()` 抛 `NotImplementedError`
      （`llm/openai_client.py:73-81`，`:83` 有个不可达 `yield {}` 用来保持 async generator 形态）。
      **定案：加载时即声明「不支持流式」**（Definition 暴露能力位，Loader 在装配期校验，
      消费方拿不到流式能力就不会调），**不允许保留「加载成功但一调就炸」的状态**。
      真正补齐流式实现**推迟到 P3-6（消息格式脱耦）之后** —— 在中立表示定稿前写翻译层是白写。
      同时收紧 `pyproject.toml:9` 的 `openai>=1.59.5` 下限（实际已装 3.0.0，声明与现实差两个大版本）。
- [ ] **P2-4** 消费方改 `inject`：`runtime/agent.py:113` 等处不再 `LLMClient()` 直接实例化。
- [ ] **P2-5** 最小组合文件 `cordis.yml`：只有 llm 相关的几行，能启动即可。
- [ ] **P2-6** 验证「换 provider 只改一行 `name:`」。

**验证**：
```bash
uv run pytest -q                                   # P0 主路径必须仍绿
uv run pytest tests/seams/test_llm.py -q           # 两个 provider 跑同一组契约断言
grep -rn "LLMClient()" --include="*.py" .          # 应为空
```

**回滚点**：P2 改动跨 `llm/` 与消费方，是第一次真正动业务代码。**打 tag**。

---

## P3 seam 铺开：其余 17 个

按依赖顺序推进，**每落地一个 seam 就跑一次 P0 主路径**，不攒着一起验。
每个 seam 必须三角色齐活（R-S-1）：Definition + ≥1 Provider + ≥1 Consumer。

- [ ] **P3-1 配置服务化**（先做，它挡着所有人）：`settings` 全局单例（`core/config.py:143`，22 文件 import）
      改为服务槽；**消灭 import 期读配置** —— 两处：`tools/subagents/registry.py:97`、
      `memory/jobs/extraction.py:103-106`（Pydantic `json_schema_extra` 在**类定义期**
      冻结 `memory_extractor_max_items`，租户级差异化对它完全无效）。
      同批处理 hook `_registry`、`_executor_instance`、`engine`、
      `core/storage.py:152` 的 `_storage_instance` 四个模块级单例（R-S-7）。
      `database/engine.py:20-32` 的「副作用导入 registry」是有意设计（CLAUDE.md 架构第 7 条），
      DatabaseService 化时必须保留「拿到 session 即全部映射在册」的等价保证。
- [ ] **P3-2 执行世界组**：`storage` → `subprocess` → `fs` → `shell` → `sandbox`。
      这五个要一起想：fs 与 subprocess 共享同一执行世界，才可能「一起切远程沙箱」。
      `tools/shell_policy.py` 的静态白名单降级为 `sandboxPolicy` 的一种策略，不再是安全边界本身。

### P3-3 `tools`（工作量最大，拆成五步做，别当一条任务）

3134 行 + **26** 个工具。依据 `research/tools-decomposition.md`，按耦合度分约 10 个包，
**不是一工具一包**（互相 import 的工具强行拆包只会制造循环依赖）。

- [ ] **P3-3a** `ToolRegistry` 升格为 Definition：注册/注销走 `caller.effect()`，
      `to_param()` 不再产 `anthropic.types.ToolParam`（形态改造留到 P3-6）。
- [ ] **P3-3b** **先解工具目录的静默误删**（做在拆包之前，否则一拆就炸）：
      `tools/service.py:34-45` 的 `sync_builtins()` + `mark_missing_deleted()` 会把
      「当前进程没挂载的工具」在 DB 目录里软删除，随后 Agent 绑定该工具直接 409。
      插件化后「没挂载」变成常态（worker profile、租户裁剪），**必须先重定义目录真源**：
      目录记录的是「曾经存在过的工具」，可用性由运行时组合决定，不由软删除标记决定。
      现成证据：`tools/subagents/trigger.py:15` 已经引用了一个目录里不存在的工具名，
      说明这个风险在插件化之前就已存在。
- [ ] **P3-3c** 工具返回值从裸 `str` 改结构化结果。
- [ ] **P3-3d** 按耦合度拆包（约 10 个 `tool_*` 包，各带 `plugin.py`）。
      **交互类 4 个工具（`ask_user_question` 等）与 `approval` seam 成组交付**，
      不在本步拆 —— 它们的挂起/恢复语义依赖 P4 的日志真源，提前拆会拆出半个状态机。
- [ ] **P3-3e** 决定 `echo` / `Weather` 两个示例工具的去留：留就进正式包并补测试，
      删就一并清目录记录与文档（CLAUDE.md 功能删除规范）。**不允许悬着。**

- [ ] **P3-4 会话组**：`sessionLog`（全新，append-only）+ `sessionPersistence`
      （迁入 `session/service.py:432-466` 的快照+水位逻辑）。**此时只建表建服务，还不切真源**（真源切换在 P4）。
      `sessionPersistence` 同批要提供**级联清理登记扩展点**：
      `session/repository.py:24-38` 的 `_SESSION_CHILD_MODELS` 硬编码了 10 张别域表，
      插件自带的带 `session_id` 子表不在清单里会被**静默漏删**（design 8.4）。
- [ ] **P3-5 其余**：`systemPrompt`（`prompt/compose.py` 升格，提供四种注册 + assemble waterfall）、
      `approval`（**连带 interaction 域 1157 行的拆迁**：`RuntimeSuspensionRecord` 在 P4 被日志吸收后，
      该域一半的状态机代码会瓦解 —— P3 先做 provider 外壳，重设计压在 P4）、
      `compaction`、`skills`、`subagents`（**打破 `AgentType` 封闭 enum**；
      顺带处理 `runtime/subagent.py:31,67` 直接 new team 域 `SubAgentRunRepository` 的越域）、
      `jobs`（**Provider 从 `memory/jobs/repository.py:227-315` 泛化**，
      lease / `FOR UPDATE SKIP LOCKED` / 退避 / 幂等键 / 优雅停机已完备，不是从零造；
      `schedule` 按决策 D12 并入本项，不单列）、
      `memory`（见 P3-5b）、`hooks`（去单例）、`api`（见 P5-5）。
- [ ] **P3-5b `memory` 不是「最轻的一个」**（`research/remaining-domains-assessment.md` 已推翻该判断）。
      业务规则确实不动，但要做四件结构性改动：
      ① 8 个文件直读 `settings` + 3 处 `session_factory=AsyncSessionLocal` 默认参数 +
         2 处 `llm or LLMClient()` 兜底 + 1 处 `jwt_secret_key` 隐藏依赖，全部改经 ctx；
      ② **三处跨域直连 session 域**必须改走 seam，且与 P4 切真源强连动：
         `memory/jobs/extraction.py:20,218`（直接 new `SessionRepository`）、
         `memory/recall/repository.py:283`（**直接写别域表列** `turn.memory_context_id`）、
         `memory/repository.py:728,811`（join `session_turns`）；
      ③ `rendered_memories` 的注入路径现横跨 `session/service.py:483-487` 与
         `runtime/agent.py:578-593` 两端，需重接到 systemPrompt / sessionLog 两个 seam；
      ④ **worker 进程宿主化**（design 6.5）：`memory/jobs/worker.py` 也要跑组合树，
         否则 dream 认领 / retention 清理 / inline 失败重试三件事静默失效。
- [ ] **P3-6 消息格式脱耦**（R-S-8）：会话与工具层不再直存/直产 Anthropic 内容块。
      `session_messages.content` JSONB 的形态变更需要 Alembic 迁移与数据转换。
      **P2-3 的 openai 流式实现排在本步之后。**
- [ ] **P3-7 每插件迁移链**（design 第八节）：通用 env.py 模板 + 程序化 `Config()` 驱动，
      `version_table=alembic_version_plugin_<name>`、`include_name` 前缀白名单、
      加载期校验表名前缀不合规即拒绝加载。**核心链原样不动。**
      通用模板必须挂上 `on_version_apply` 审计钩子（对齐 `alembic/env.py:46`），
      否则插件迁移无审计记录；`main.py:24-26` 的 `create_tables` 捷径不覆盖插件表，
      在 lifespan 里对启用插件统一 upgrade，使开发与生产同路径。
- [ ] **P3-8** 配 `.importlinter` 契约并接入 CI（design 3.3）：
      `uv add --dev "import-linter>=2.5.1"`（`protected` 契约 + Python 3.14 的最低版本）；
      **每个 provider 顶层包一条 `protected` 契约**（不能用 `*_local` 通配，官方不支持），
      `agent_loop` 一条 `protected`，`kernel` 一条 `forbidden` 穷举其余顶层包；
      同时定 `tests/` 是否纳入导入图（要补 `__init__.py` + 列入 `root_packages`），
      `.import_linter_cache` 进 `.gitignore`。

**验证**：
```bash
uv run pytest -q
uv run lint-imports                                # 三条契约
uv run pytest tests/seams -q                       # 每个 seam 的契约测试
```

**回滚点**：每个 seam 一个 commit，**每完成一组打 tag**（P3-1 / P3-2 / P3-3 / P3-4 / P3-5 / P3-7）。
`tools` 那一步单独 tag —— 它是本阶段最容易做崩的，且 P3-3b 若漏做会以「Agent 绑定 409」
的形式在集成期才暴露。
P3-7 若在实现期证伪，按 design 8.5 的退路顺序换（独立 PG schema → JSONB 附属表），
**不要自造迁移引擎**。

---

## P4 循环：词汇冻结 → 日志真源 → 拆循环

**必须在 P3 之后**：循环是所有 seam 的最大消费方，seam 没稳定就拆循环等于同时动两端。

- [ ] **P4-1** 冻结事件词汇（design 5.1 的事件表 + 分发模式），放进 `agent` Definition 包。
      逐个对照 `research/agentos-event-contract.md` 的 18 处 `bus.emit` 发射点做映射，**不靠记忆**。
      别漏 `task/service.py:25-35` 的 `task_snapshot` 支线，以及 `agent.py:180-184`
      的 hook `additional_context`（**当前完全不落库，却会进模型请求** —— 不事件化就直接违反 A4）。
      同批要定 team 收件箱（`team_messages`）的归属：**保留独立表还是并入 `session_events`**，
      冻结词汇前必须回答，否则会出现两套「待办投递」真源。
- [ ] **P4-2** 补上**六处**缺口（design 5.6）：
      ① 每次模型调用的 usage —— **新增 `llm/response` 事件**（`agent/request` 在调用前发射、
         用量只在流耗尽后才有，挂在旧事件上拿不到）；
      ② 事件时间戳 + 会话内 seq（**写者显式取 `last_seq+1`**，见 P4-7）；
      ③ SSE 帧 `id:`（＝会话内 seq）与 `Last-Event-ID` 续传；
      ④ actor / run_id 归属（子代理中间过程入同一条日志）；
      ⑤ 异常路径必发 `turn/end`；
      ⑥ **assistant content 的落库真源改为每次调用的 final content** ——
         增量流不足以重建（`redacted_thinking` 被错标、`signature_delta`/`citations_delta` 被丢），
         `assistant/chunk` 降级为「仅供 UI 渲染」。
- [ ] **P4-3** 实现 `derive_messages()` 投影 + 「模型可见即已记录」运行时断言。
      **分两档**（design 5.4）：生产常开的是 L1 归一化哈希（1~5ms），
      CI/影子环境才跑 L2 逐字节（0.5~23s）；不一致时**在发送前中断**并必发 `turn/end`。
      快照带 `projector_version` 列 —— 投影逻辑升级后快照「永不失效、只会落后」。
- [ ] **P4-4** **切真源**（不可逆点）：`session_messages`/`session_snapshots` 降级为投影缓存。
      切换前置条件有两条，缺一不可：
      ① A4 在**真实会话**上通过，不只是测试用例；
      ② **双写者接管演练通过**：人为制造两个写者同时追加，
         证明 `UNIQUE(session_id, seq)` 冲突方让位、日志无重号无缺号、接管者能补齐合成结果。
      **只加表不删表。**
- [ ] **P4-5** 拆循环：design 5.2 的硬编码依赖逐条搬到监听器，逐条勾掉。
      **循环体内不得残留任何工具名字符串特判**。
      交互 4 工具（P3-3d 缓交的那批）在这一步与 `approval` seam 一起落地。
- [ ] **P4-6** 两份循环合一（design 5.3）：子代理差异全部下沉为组合差异
      （内存 sessionLog provider / 不挂 approval provider / config 里的轮次上限）。
- [ ] **P4-7** `RuntimeSuspensionRecord` 改为日志驱动接管（design 5.5）：
      **单写者机制用乐观追加，不是 advisory lock**（决策 D10）——
      `UNIQUE(session_id, seq)` + 写者显式赋 `last_seq+1`，冲突即栅栏；
      租约行（可参照 `interaction/models.py:34-41` 的既有形态）只作效率层，不作正确性依据。
      **禁止使用会话级 advisory lock**：`.env` 指向事务模式池（6543），
      PgBouncer 官方对此明确标 Never，且 SQLAlchemy 归还连接只 rollback、
      不发 `pg_advisory_unlock_all()`，锁会泄漏给下一个租户。
      「挂起」变成「收件箱等 approval 事件」。
- [ ] **P4-8** 写第二个最小循环实现（哪怕只是「单步不带工具」）验证 A2。

**验证**：
```bash
uv run pytest -q
uv run pytest tests/invariants/test_model_visible_logged.py -q     # A4
grep -rn "import agent_loop\|from agent_loop" --include="*.py" .   # 除组合文件外应为空（A2）
grep -rn '"EnterPlanMode"\|"ExitPlanMode"' agent_loop/             # 应为空
```

**回滚点**：**P4-4 是全流程唯一的不可逆点**。切真源前单独打 tag，并确认数据库迁移
有可用的 `downgrade`。P4-5 之后循环形态彻底改变，回滚成本陡增。

---

## P5 组合层

- [ ] **P5-1** Loader 插件：条目 diff 成 mount / unmount / update 三种操作。
- [ ] **P5-2** patch 栈叠加（design 6.1、6.2 的 1–3 层；4、5 层留到 P6）。
      文件头注释必须写明「**行序不带加载语义，激活由服务可用性驱动**」。
- [ ] **P5-3** `dump-config`：打印带来源层注释的条目树而不启动；敏感键统一脱敏为 `***`（A8）。
- [ ] **P5-4** 启动断言：全部条目非 ACTIVE 即列出 `id`、状态、失败原因并非零退出。
- [ ] **P5-5** `api` seam：`ctx.http.mount(caller, router, prefix)`，
      disposer 按**挂载时快照 diff** 移除 Route（`include_router` 会复制对象，事后反查不到；
      按 `id()` 过滤重建整表，**不能用 `list.remove`**）**并把 `app.openapi_schema` 置 `None`**。
      插件 router 禁带 lifespan、禁动态中间件与全局异常处理器；挂卸用 `asyncio.Lock` 串行化。
      **多实例下这是单进程状态**：路由只随进程级根树变化，生产环境的进程级组合变更
      走滚动重启，不做跨实例广播（design 6.4）。动态挂卸仍必须实现（effect 语义不留例外 +
      开发期热装），但不作为线上产品能力对外承诺。
      消灭 `api/router.py:3-38` 与 `tools/registry.py:89-125` 的静态聚合。（design 6.4）
- [ ] **P5-6** 入口函数化 + **worker profile**（design 6.5）：
      消灭 `main.py:60-61` 的「import 即 `create_app()`」；
      web 与 worker 两个入口共用同一个 `compose_root(profile)`，
      差异只体现为组合文件（worker profile 不含 `api` 条目）。
      不做这一步，`memory/jobs/worker.py` 在 P3-1 去单例后就拿不到 DatabaseService/LlmRuntime，
      dream / retention / 重试接管三件事会**静默失效**。

**验证**：
```bash
uv run python -m agentos dump-config | head -50
uv run python -m agentos dump-config | grep -iE "password|token|secret|dsn"   # 应只见 ***
uv run pytest tests/composition -q
grep -rn "^settings = Settings()" --include="*.py" .    # A6：应为空
```

---

## P6 多租户

- [ ] **P6-1** 三级作用域（design 7.1）+ 租户 patch 的 `id` 白名单
      （审计/计费相关服务槽不可被租户覆写）。
- [ ] **P6-2** DB 来源的 patch 层（第 4、5 层）：Loader 不感知来源差异。
- [ ] **P6-3** Agent 实体即 preset（R-T-2）：会话创建时挂载，**失败则整个会话创建回滚**。
- [ ] **P6-4** realm 懒挂载 + 空闲卸载。
- [ ] **P6-5** 跨实例事件平面（R-T-5，design 5.8）。这一条有**四个面**，分开做分开验：
      ① **写侧**：日志追加成功后 `XADD agentos:events:{session_id} {seq}-0`（**先落库、后 XADD**）；
      ② **读侧**：订阅方 `XREAD` 单流、**不用 consumer group**（那是竞争消费，会让多个订阅者分食事件）；
         Redis 不可用时退化为 DB 轮询，冷区一律回落 DB；
      ③ **SSE 帧升级 + 新增 GET 端点**：现有两个 SSE 端点都是 **POST**，
         而原生 `EventSource` 只支持 GET，也只有它会自动带 `Last-Event-ID` ——
         **必须新增 `GET /sessions/{id}/events`**，否则续传只是纸面能力。
         帧补 `id:`（＝seq）、15s 心跳、`Cache-Control: no-store`、
         **`X-Accel-Buffering: no`**（nginx `proxy_buffering` 默认开，不加这行流会被缓冲）、显式终结帧。
         若采用 `sse-starlette`（目前仅经 `mcp[cli]` 间接存在、全仓零 import），
         须提升为直接依赖。
      ④ **断线语义解耦**：`runtime/agent.py:139-146` 现在是「客户端断开即中止运行」，
         改为「**断线 ≠ 中止**」，否则续传毫无意义。
      新增 6 个 `AGENTOS_EVENTS_*` 配置项，并**吸收掉死配置 `AGENTOS_DISABLE_REDIS_EVENTS`**
      （存在于 `.env`、`.env.example:37`、`core/config.py:54`，但全仓零代码读取 ——
      CLAUDE.md 明令不得保留无代码读取的废弃环境变量）。

**验证**：
```bash
uv run pytest tests/tenancy -q          # A9：两租户同进程互不可见；会话结束注册全撤销
uv run pytest tests/events -q           # A11：跨实例订阅、重启不丢已落库事件
```

---

## P7 开放：第三方插件与沙箱

按 design 第九、十节执行。**T3 补沙箱与 T2 的 MCP stub 互不阻塞，可并行。**

- [ ] **P7-1 `sandbox` seam**：`confine(argv, policy)` 单方法 + **`sandboxPolicy` 单独持有策略**
      （否则 shell 与 fs 会限到不同根目录）。梯子：Linux bwrap → Landlock + seccomp；
      macOS Seatbelt 仅开发机；**探测不可用即 fail-closed 拒跑**。
- [ ] **P7-2 T3 收编 skill 通道**：`core/executor.py:110-118` 的 `get_executor()` 预留位
      换成 `SandboxExecutor`。现状 `skill/service.py:307-374` 有超时/killpg/env 白名单/一次性目录，
      **缺的是 OS 边界本身**：脚本可读宿主 `.env`、网络全敞开。
- [ ] **P7-3 T2 MCP client stub**：`pyproject.toml:23` 已声明 `mcp[cli]` 但全仓零 import。
      stub 是受信的一等插件，把远端 tools 注册进 `tools` seam（命名空间化），
      远端存活映射为服务 provide/撤销，**epoch 依赖驱动照常工作**。
- [ ] **P7-4 限额**：CPU/内存用 cgroup v2（容器原生；裸机 `systemd-run --scope`），
      轻量档 rlimit；常驻型加每租户实例数/并发上限 + 空闲回收。
- [ ] **P7-5 出网**：**默认全禁**；按插件声明经平台正向代理放行白名单域名，代理侧记账审计。
- [ ] **P7-6 来源校验**：整包 sha256 已有（`skill/service.py:163-166`），
      扩展为安装记录 hash + 版本 pin。**禁止安装期执行代码** —— 与 dsh 相反的一条，
      租户上传物只解包、只静态校验，任何构建/安装钩子一律不执行。
- [ ] **P7-7 A10 对抗性测试**：恶意插件不能读其它租户数据、不能拿到 DB/Redis/MinIO 凭证、
      不能注册 ctx 服务、不能挂 waterfall；超限被终止；来源校验失败拒绝安装。

**验证**：
```bash
uv run pytest tests/isolation -q
grep -rn "importlib" --include="*.py" .    # 确认无「租户上传物被同进程 import」的路径
```

---

## 全局验证命令

```bash
uv run pytest -q                    # 全量
uv run lint-imports                 # 依赖方向三条契约（A2/A3）
uv run ruff check .                 # 若项目已配
uv run python -m agentos dump-config
```

## 高风险文件

| 文件 | 行数 | 风险 |
|---|---|---|
| `runtime/agent.py` | 1712 | 上帝类，P4 整体重写；11 处硬编码依赖漏一处就等于 A2 不成立 |
| `runtime/subagent.py` | — | 与主循环同构且已漂移，合并时容易丢掉差异语义 |
| `tools/registry.py` + 26 个工具 | 3134 | P3 最大工作量，拆包时容易漏注册 |
| `session/service.py:432-466` | — | 上下文加载逻辑，P4 切真源的核心 |
| `core/config.py:143` | — | 22 个文件 import，改动面广 |
| `database/registry.py` | — | 漏登记会让 autogenerate 误判删表（CLAUDE.md 第 7 条） |
| Alembic 版本链 | 21 版本 | 已入历史的迁移**不得删改**，只能新增 upgrade/downgrade |

## `task.py start` 前的待办

- [x] 补齐 design 第九、十节（第三方插件公民等级 / `sandbox` seam）
- [x] 补齐 design 第八节（数据层归属 / Alembic 每插件迁移链）
- [x] `implement.jsonl` / `check.jsonl` 填入真实条目（seed `_example` 行已删；
      21 + 19 条，全部研究报告与相关 spec 均已登记）
- [x] 按调研 D 的结论修正 design 3.3（import-linter 契约表达力）与 6.4（FastAPI 动态挂卸）
- [x] 按六份调研的结论回改 prd / design / implement（内核 contextvars 反转 D9、
      乐观追加 D10、类型检查器 D11、`schedule` 并入 `jobs` D12、六处事件缺口、
      19 → 18 个首批 seam、规模 2000–3000 行）
- [x] 体检在途任务 `08-14-统一大模型调用入口`（2880 行文档 + 332 行代码），据此细化 D3、
      P0-0/P0-1、P2 起点，并把其 `design.md` 的可复用局部登记进 `implement.jsonl`
- [ ] 确认 design 3.2 的顶层包扩充（22 → 约 60）是否接受
- [ ] 确认 `08-14-统一大模型调用入口` 归档，并授权先 commit 保存 332 行未提交现场（不 push）
- [ ] 用户明确批准最终规划摘要



