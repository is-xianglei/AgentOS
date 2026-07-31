# 内置 SubAgent 复刻方案

参照 Claude Code 的内置 SubAgent 实现，补全 AgentOS 中 Explore / Plan /
Verification 三种子代理。当前这三种的 `system_prompt` 是字面占位
（`tools/subagents/registry.py:19-32`），`allowed_tools` 全为 `None`。

## 一、结论先行

「完美复刻」不成立，有三类差异必须区别对待，硬搬会造出空壳：

| 类别 | 内容 |
|---|---|
| 可直接搬 | 只读约束的逐条枚举法、Verification 的反理性化机制、`VERDICT:` 可解析锚点、强制命令输出格式、`when_to_use` 字段 |
| 需改造 | 黑名单成员（AgentOS 无 `NotebookEdit` / `ExitPlanMode`）、模型分档（`LLMClient.stream()` 未开放 `model` 形参）、`omitClaudeMd`（AgentOS 注入的是 memory 而非 CLAUDE.md） |
| 不复刻 | `loadAgentsDir` 自定义 agent 加载（755 行）、GrowthBook 特性开关、`background: true` 后台执行 |

真正的阻塞点不是提示词缺失，而是两个结构问题，见第四节。

## 二、Claude Code 的事实

内置 agent 共 6 个（`src/tools/AgentTool/builtInAgents.ts:45-69`）：
`general-purpose`、`statusline-setup`、`Explore`、`Plan`、`claude-code-guide`、
`verification`。后三个在特性开关后面，`verification` 默认关闭
（`tengu_hive_evidence` 默认 false）。

一个内置 agent 就是一个 `BuiltInAgentDefinition` 常量，没有类继承，
没有独立执行器。字段即全部能力：

| 字段 | 作用 |
|---|---|
| `agentType` | 类型标识 |
| `whenToUse` | 给主 agent 看的路由依据，独立于 systemPrompt，会进 Agent 工具描述 |
| `disallowedTools` | 黑名单 |
| `tools` | 白名单（Plan 写 `tools: EXPLORE_AGENT.tools`） |
| `model` | `'inherit'` / `'haiku'` |
| `omitClaudeMd` | 是否跳过注入 CLAUDE.md |
| `background` | 是否后台执行（verification 为 true） |
| `criticalSystemReminder_EXPERIMENTAL` | 核心约束的二次注入 |
| `getSystemPrompt()` | 函数而非字面量，运行时按环境分支 |

三个 agent 的黑名单完全一致：`Agent`、`ExitPlanMode`、`Edit`、`Write`、
`NotebookEdit`。两点关键：

1. **Bash 不在黑名单**，三个都能跑 shell。
2. **禁 `ExitPlanMode`**：Plan 子代理能产出方案，但不能替用户批准。
   这是「允许 Plan 子代理存在」与「保留人类审批」的兼容解法。

### 只读是怎么保证的

分层，不单靠工具集。工具层禁 `Edit` / `Write` / `NotebookEdit`；
**shell 写文件的旁路靠提示词堵**（`exploreAgent.ts:26-36`）：

```
- Using redirect operators (>, >>, |) or heredocs to write to files
- NEVER use Bash for: mkdir, touch, rm, cp, mv, git add, git commit,
  npm install, pip install, or any file creation/modification
```

同时给正向白名单：`ls, git status, git log, git diff, find, cat, head, tail`。
Bash 太有用（git diff、find）不能禁，但 Bash 能写文件，所以只能这么堵。
提示词结尾再重复一次同样的约束。

### 三个 agent 的核心能力

**Explore** — 定位「file search specialist」，核心指标是快。
`model: 'haiku'`（外部用户）、`omitClaudeMd: true`，注释写明理由：只读搜索
不需要 commit/PR/lint 规则。提示词要求并行发起多个 grep/read 调用。
`whenToUse` 里教调用方传 `"quick"` / `"medium"` / `"very thorough"`
三档彻底度——参数化协议写在描述里，没有加 schema 字段。

**Plan** — 「software architect」。四步流程：理解需求 → 探索现有模式 →
设计方案 → 细化步骤。工具集与只读约束复用 Explore，`model: 'inherit'`。
强制输出结尾带 `### Critical Files for Implementation`，列 3-5 个文件。

**Verification** — 152 行，最特别，因为它是**对抗自己的**。第一句：

> Your job is not to confirm the implementation works — it's to try to break it.

它显式命名模型的两个失败模式：verification avoidance（读代码、叙述打算怎么测、
写 PASS、走人）和「被前 80% 迷惑」（看到漂亮 UI 或通过的测试就放行，
没发现一半按钮没接线、刷新就丢状态）。反制是一整套机制：

- `RECOGNIZE YOUR OWN RATIONALIZATIONS` 一节，把模型会用的借口逐条列出配反驳。
  例：「The code looks correct based on my reading」→ reading is not
  verification. Run it.
- 强制输出格式：每个检查必须有 `**Command run:**` + `**Output observed:**`，
  要求粘贴原始输出而非转述。**没有命令块的 PASS 直接算 skip。**
  并给出 Bad(rejected) / Good 对照示例。
- 威慑：声明调用方可能重跑命令抽查，输出对不上则整份驳回。
- PASS 前置条件：报告须含至少一个对抗性探测（并发 / 边界值 / 幂等 /
  孤儿操作）及其结果。
- FAIL 前置条件：先排除「上游已有防御 / 是故意的 / 不可修」三种情况。
- 结尾必须是 `VERDICT: PASS|FAIL|PARTIAL`，注明 parsed by caller，
  并规定不加粗、无标点变体——这是机器解析的锚点。
- `criticalSystemReminder_EXPERIMENTAL` 把核心约束二次注入。

还有一句直指要害：**「Test suite results are context, not evidence.」**
理由写在后面——实现者也是 LLM，它的测试可能堆满 mock 和循环断言。

写权限是精确切分的：**项目目录只读，`/tmp` 可写**。它需要写临时测试脚本
（并发压测、Playwright），而工具层禁了 Write，所以只能走 Bash 重定向——
这恰好用上了「Bash 不在黑名单」。

## 三、AgentOS 的现状

工具清单（`tools/builtin/`）：`Read Write Edit Glob Grep Bash Agent Skill
SkillRun SkillResource Task* Team* SendMessage ReadInbox ListMessages`。
**没有 `NotebookEdit`、没有 `ExitPlanMode`、没有 `WebFetch`**，
所以 Claude Code 那份黑名单无法照抄，等价物只有 `Agent` + `Write` + `Edit`。

`SubAgentSpec`（`tools/subagents/definition.py:24-45`）现有字段：
`agent_type`、`system_prompt`、`allowed_tools`、`disallowed_tools`。
`disallowed_tools` 默认值是 `("Agent", "TeamCreate", "TeamSpawn", "Bash")`——
**默认禁了 Bash，方向与 Claude Code 相反**。

`resolve_tools` 语义：白名单模式下 `set(allowed) - set(disallowed)`，
黑名单优先于白名单，这一点与 Claude Code 一致。

由此修正一个常见误判：Explore「声称只读却能改文件」的路径是 **Write / Edit**，
不是 Bash——Bash 已被默认黑名单挡住。

## 四、两个结构性阻塞点

这两条比空提示词严重，必须先修。

### 4.1 子代理绕过 permission，是一条提权路径

`tools/service.py:47-48` 的注释写明了架构假设：

```python
# PreToolUse:可拦截(block)或改写入参(updated_input)。permission 已在
# 更高层(orchestrator _execute_tool_uses)裁决过,此处 hook 只能更严,不能提权。
```

`ToolService.run` 自己不裁决 permission，它假设调用方裁决过——而注释点名的
调用方是 orchestrator。`SubAgentRunner._run_react_loop`
（`runtime/subagent.py:227`）直接调 `tool_service.run(...)`，不走那条路径。

orchestrator 侧有完整链路：`PermissionService.evaluate()`
（`runtime/agent.py:494`）、`DANGEROUS_TOOLS` 名单、命中 `ask` 时挂起会话并
emit `permission_request`。子代理一条都没有。

而 `DANGEROUS_TOOLS = frozenset({"Bash", "SkillRun", "Write", "Edit"})`
（`permission/service.py:18`），默认 `ask`。

**后果**：主代理调 Write 要用户点头；主代理派一个 `general_purpose` 子代理去调
Write，不用点头。`allowed_tools=None` 让子代理拿到 `Write`、`Edit`、`SkillRun`
三个危险工具，全部绕过审批门。Bash 只是碰巧被默认黑名单挡住，那是巧合，
不是安全设计。

Bash 工具本身也没有沙箱（`tools/builtin/bash.py:25-29`）：`workdir` 收任意
绝对路径，只校验存在性与是否为目录，命令直接进 `bash -c`，无命令白名单。
环境变量做了最小化（只传 PATH / LANG / HOME），但那不是沙箱。

因此**在 permission 补齐之前，不能给任何子代理放开 Bash**：
Bash + 无裁决 + 无路径沙箱 = 服务端上无人审批的任意命令执行。

### 4.2 ReAct 循环 6 轮上限，Verification 结构上跑不完

`runtime/subagent.py:212`：

```python
for _ in range(6):
```

Verification 的职责是跑 build + 全量测试 + linter + 至少一个对抗性探测，
Claude Code 那份提示词的 REQUIRED STEPS 就有 5 步，前 1-2 轮还要花在读
CLAUDE.md 找测试命令上。

所以 Verification 现在不是提示词没写，是**结构上跑不完**。提示词补得再好，
第 6 轮直接 `raise RuntimeError("子代理工具调用轮次过多，已停止执行")`——
整个 run 失败，报告丢失，调用方拿到异常而不是 `PARTIAL`。

附带两个问题：该 `RuntimeError` 违反项目约定（应为 `AgentException`）；
轮次耗尽应降级返回已有结论，不该丢弃整个 run。

## 五、Verification 的授权路径

补完 permission 后 Verification 依然跑不了测试：Bash 属 `DANGEROUS_TOOLS`，
默认 `ask`，而子代理无挂起/恢复通道，`ask` 只能坍缩为拒绝。

`PermissionService.evaluate` 的规则维度是 **scope(session/global) + tool_name**
（`permission/service.py:40-48`，注释写明「matcher 本期只按 tool_name 全匹配」），
**没有 actor 维度**，无法区分「orchestrator 调 Bash」与「verification 子代理调
Bash」。给 Verification 放行会让同会话主代理也免审批。

三条路：

| 方案 | 内容 | 评价 |
|---|---|---|
| A | 给规则加 actor / agent_type 维度 | 采用 |
| B | 用户会话级预授权 Bash | 零代码，但主代理同时免审批 |
| C | Verification 不给 Bash | 退化成「读代码写报告」，删掉其全部价值 |

**选 A**，因为成本远低于预估：`PermissionRuleRecord` 已有
`matcher: Mapped[dict[str, Any] | None]` JSONB 列
（`permission/models.py:41-45`），comment 就是「细化匹配条件(预留,
空表示按 tool_name 全匹配)」——**这列正是为此预留的**。

因此 A **不动 ORM、不需要 Alembic 迁移**。只需 `evaluate()` 增一个可选
`agent_type` 参数，匹配时读 `matcher`：为空则匹配任意 actor（现存规则全部
向后兼容），`matcher = {"agent_type": "verification"}` 则只对该子代理生效。

## 六、实施阶段

基线：改动前 `uv run pytest -q` 为 **17 passed, 6 warnings**。
每阶段补测试并对齐基线。`tests/` 现无任何 subagent 用例。

### 阶段 0：子代理路径补 permission 裁决（安全，先于一切）

在 `SubAgentRunner._run_react_loop` 与 `_run_teammate_loop` 调
`tool_service.run(...)` 之前插入 `PermissionService.evaluate()`。
`evaluate` 只需 `session_id`，子代理手上有，无阻塞。

行为映射（`ask` 的处理复用 orchestrator 的 `deny` 语义，
见 `runtime/agent.py:511-514`）：

| evaluate 结果 | orchestrator | 子代理 |
|---|---|---|
| `allow` | 执行 | 执行 |
| `deny` | 落拒绝 tool_result，继续循环 | 同 |
| `ask` | 挂起会话等审批 | **按 deny 处理**，tool_result 说明「该工具需用户审批，子代理上下文中不可用」 |

子代理没有挂起/恢复通道，所以 `ask` 只能坍缩。此阶段不动提示词，纯补裁决，
行为保守，无待定项。

测试：危险工具被拒且 tool_result 含说明、非危险工具正常执行、
命中 deny 后循环继续不中断。

### 阶段 1：轮次上限可配 + 耗尽降级

- `for _ in range(6)` → `for _ in range(spec.max_rounds)`。
- 耗尽时不 `raise`，返回已有文本 + 截断说明（对应 Claude Code 的 `PARTIAL`）。
- 保留的异常改用 `AgentException`，不用裸 `RuntimeError`。

测试：轮次耗尽返回降级文本而非抛异常、`max_rounds` 生效。

### 阶段 2：扩展 SubAgentSpec

`tools/subagents/definition.py` 增字段：

```python
when_to_use: str = ""          # 进 Agent 工具描述，给主代理做路由依据
model: str | None = None       # None 表示继承主会话模型
max_rounds: int = 6            # 按类型区分；verification 需显著放宽
```

`disallowed_tools` 改为按类型显式声明，不再依赖含 Bash 的统一默认值：
Explore / Plan 加 `Write`、`Edit`；Verification 只禁 `Write`、`Edit`，
**保留 Bash**（跑测试与写 `/tmp` 临时脚本的唯一途径）。

`LLMClient.stream()`（`llm/client.py:25`）增 `model` 形参并透传
`spec.model`——同文件 96、132 行的方法已有 `model or self.model` 写法，照抄。

测试：`resolve_tools` 裁剪结果断言 Explore 不含 `Write`/`Edit`；
`model` 为 None 时回落主会话模型。

### 阶段 3：Explore / Plan 提示词 + 路由依据

改 `tools/subagents/registry.py`，两份中文提示词（项目要求中文），
照搬 Claude Code 的只读约束枚举法，包括堵住 `>` `>>` `|` 和 heredoc、
给出正向命令白名单、结尾重复约束。

Explore 要点：定位「代码检索专家」、强调快、要求并行发起多个检索调用、
`when_to_use` 中说明彻底度三档、`model` 可设为更快档位。

Plan 要点：四步流程、复用 Explore 的只读约束、强制结尾输出
「实施关键文件」清单 3-5 个。

改 `tools/builtin/agent.py`：`_AGENT_TYPES` 现只列枚举值，改为列
`agent_type: when_to_use` 键值对。这是当前四种类型「除名字外无区别」的
另一半成因——模型没有路由依据。

测试：工具描述包含各类型的 `when_to_use` 文本。

### 阶段 4：permission 规则加 actor 维度

`evaluate()` 增可选 `agent_type: str | None`；`find_match` 支持读 `matcher`
做二级匹配。`matcher` 为空 → 匹配任意 actor（现存规则向后兼容）；
`{"agent_type": "verification"}` → 只对该类型生效。不动 ORM，无迁移。

测试（JSONB 匹配须真实 PostgreSQL 集成测试，见项目测试约定）：
`matcher` 为空的旧规则对所有 actor 生效、带 `agent_type` 的规则只命中该类型、
会话级规则优先于全局规则。

### 阶段 5：Verification 提示词

依赖阶段 1（轮次）与阶段 4（Bash 定向授权），两者缺一则不可用。

必须保留的机制（这些不是修辞，是可解析契约）：

1. 开篇确立对抗定位：目标是尝试破坏实现，不是确认它能工作。
2. 显式命名两个失败模式：验证规避、被前 80% 迷惑。
3. 反理性化清单：逐条列出会用的借口配反驳。
4. 强制输出格式：每个检查含「执行的命令」+「观察到的输出」，
   要求原文粘贴；**无命令块的 PASS 视为跳过**；给 Bad/Good 对照。
5. PASS 前置：须含至少一个对抗性探测及结果。
6. FAIL 前置：先排除已有防御 / 故意为之 / 不可修三种情况。
7. 结尾 `VERDICT: PASS|FAIL|PARTIAL`，供调用方解析，不加粗、无变体。
8. 「测试套件结果是上下文，不是证据」——实现者也是 LLM。

`max_rounds` 需显著放宽（建议 20 以上），否则第 4 点强制的命令执行量跑不完。

AgentOS 无 `criticalSystemReminder` 机制，二次注入可在
`prompt/compose.py` 的 `compose_subagent_prompt` 中按 spec 追加，或暂缺。

## 七、依赖关系与执行顺序

| 阶段 | 内容 | 依赖 |
|---|---|---|
| 0 | 子代理补 permission 裁决 | 无 |
| 1 | 轮次上限可配 + 耗尽降级 | 无 |
| 2 | 扩展 `SubAgentSpec` + `stream()` 加 model | 无 |
| 3 | Explore / Plan 提示词 + 路由依据 | 2 |
| 4 | permission 规则加 actor 维度 | 0 |
| 5 | Verification 提示词 | 1、4 |

0 / 1 / 2 无依赖，可并行开工。Verification 排最后，因为它同时需要轮次放宽
与 Bash 定向授权才有意义。

若要尽快交付可用产物：先做 2 → 3，Explore 与 Plan 本身只读、不需要 Bash、
零权限风险，可先上线；Verification 等 0 → 4 完成后再开。
但阶段 0 是安全修复，不应因此推迟。

## 八、与 plan mode / AskUserQuestion 的次序

一个自然的疑问：是否该先复刻 plan mode 与 `AskUserQuestion`，再做 SubAgent？
结论是 plan mode 必须排在后面，`AskUserQuestion` 可以独立先做。

### 8.1 plan mode 是子代理的消费方，不是前置件

plan mode 在 Claude Code 里**不是会话状态机**，而是注入主代理的系统提示
（attachment），内容为「当前只读、方案写进指定文件、用这些子代理去做」。
其 5 阶段工作流的 Phase 1 明确规定（`src/utils/messages.ts:3236,3240`）：

```
Critical: In this phase you should only use the ${EXPLORE_AGENT.agentType} subagent type.
**Launch up to ${exploreAgentCount} ${EXPLORE_AGENT.agentType} agents IN PARALLEL**
```

并按订阅档位并行派发 1-3 个 Plan agent、3 个 Explore agent
（`src/utils/planModeV2.ts:5-43`），代码直接 import `EXPLORE_AGENT.agentType`。

因此依赖方向是 **子代理 → plan mode**。先做 plan mode 会得到一个没有东西
可派发的空工作流。

附带一点：plan mode 的只读约束同样是提示词层的
（"This supercedes any other instructions you have received"），
唯一写例外是 plan 文件本身——与 Explore 的只读约束同一套办法。

### 8.2 AskUserQuestion 独立，可先做

`AskUserQuestion` **没有授予任何子代理**（AgentTool 的黑白名单中查不到），
它是主代理工具。其实现位置在
`src/components/permissions/AskUserQuestionPermissionRequest/`——
**按 permission request 实现，复用审批的挂起/恢复通道**，不是独立机制。

AgentOS 已有这条通道：`_suspend_for_approval`（`runtime/agent.py:612`）、
`permission_request` 事件、待批指针与恢复路径（`runtime/agent.py:199-239`）。
所以在 AgentOS 落地 `AskUserQuestion` 是「复用现成挂起通道 + 加一个工具」，
成本低，且与阶段 0-5 完全解耦，做不做都不影响 SubAgent 工作。

它也是 plan mode 的前置件——Phase 1 的 "asking them questions" 需要它。

### 8.3 Plan 子代理的语义差异仍然存在

AgentOS 没有 `ExitPlanMode`，Plan 子代理产出的方案回到主代理后没有人类
审批环节。解法不是推迟 Plan 子代理，而是接受它先作为「架构分析器」落地，
等 plan mode 那层再补审批语义——Claude Code 就是这个分层。

### 8.4 综合次序

| 优先级 | 项 | 理由 |
|---|---|---|
| 1 | 阶段 0 | 安全漏洞，不依赖任何新功能 |
| 2 | 阶段 1、2 | 无依赖，解 Verification 的结构阻塞 |
| 3 | 阶段 3 | plan mode 的前置件 |
| 4 | `AskUserQuestion` | 独立，复用现成挂起通道；plan mode 前置件 |
| 5 | plan mode | 依赖 3 与 4 |

## 九、明确不复刻

| 项 | 原因 |
|---|---|
| `loadAgentsDir` 自定义 agent 加载（755 行） | 超出「复刻内置 SubAgent」范围 |
| GrowthBook 特性开关 / A/B 分流 | AgentOS 无对应基础设施 |
| `background: true` 后台执行 | 需接入 job 基础设施，另行评估 |
| `omitClaudeMd` | AgentOS 注入的是 memory 而非 CLAUDE.md，语义不等价 |

## 十、调研中被推翻的判断

记录以下四条，因为它们都是「照抄会出问题」的位置：

1. 内置 agent 是 6 个而非 2 个，`PLAN` 与 `VERIFICATION` 真实存在。
   先前「Claude Code 没做 Plan 子代理」的判断有误。
2. 权限用黑名单 `disallowedTools` 而非白名单，且 **Bash 放开**，
   只读靠提示词逐条枚举。AgentOS 默认禁 Bash，方向相反。
3. AgentOS 的 `disallowed_tools` 默认已含 Bash，
   故「Explore 能改文件」的路径是 Write / Edit，不是 Bash。
4. `permission` 的 `matcher` 列是为 actor 维度预留的，
   故定向授权不需要迁移，成本远低于预估。
5. plan mode 不是会话状态机，而是派发 Explore / Plan 子代理的编排层
   （提示词注入 + 并行派发）。因此它是子代理的消费方，不能先于子代理实现。
   先前把它当作 Plan 子代理的前置件是错的。

