# 研究：主循环、工具管线、上下文管理、调度的插件化机制

来源：`/Users/xianglei/Desktop/deepseek-harness/packages/`。

## 一、循环本体：朴素三层 while，不是状态机

真循环在 `packages/core/agent-loop/src/agent.ts` 的 `ReactLoopAgent`：
`kick()`（驱动器）→ `turn()`（轮次）→ `step()`（一次模型请求 + 其工具调用）。

```ts
private async kick(): Promise<void> {
  while (await this.turn()) {}          // turn 结束后 inbox 还有活 → 再开一轮
}
// turn()：
while (true) {
  const decision = await this.preStep(target, { turn, step })   // 认领 inbox + agent/pre-step waterfall
  if (decision.kind === 'reject') { turnEnds = { kind: 'blocked' }; return false }
  this.session.append('step/start', { turn, step })
  for (const message of decision.messages)
    this.session.append('user/message', message, { surfaceOp: 'append' })
  const stepEnd = await this.step(decision.assembly)
  if (turnEnds && this.inbox.nextStep.length === 0) {
    await this.dispatch.serial('agent/turn-stopping', { turn, signal })   // 停轮前最后检查点
    if (this.inbox.nextStep.length === 0) break
  }
  target = 'next-step'
}
// step()：
const { request } = await this.buildRequest(..., this.session.deriveMessages(), signal)  // 历史从日志投影
const stream = preparedCall?.stream(request) ?? this.loopCtx.llm.stream(request)
for await (const chunk of stream) this.session.append('assistant/chunk', {...})
if (finish.kind === 'error') {
  const action = await this.dispatch.waterfall('agent/request-error', {...})  // 重试/压缩恢复挂这里
  if (action?.kind !== 'retry') throw new LlmError(...); continue
}
this.session.append('assistant/message', {...})
const toolCalls = message.content.filter(b => b.type === 'tool-call')
if (toolCalls.length === 0) return { kind: 'completed' }
const { concluded } = await executeToolCalls(...)
return concluded ? { kind: 'completed' } : null    // null → 外层再走一步
```

**关键：工具结果不进内存 messages**。所有事实先 `session.append()`，下一步 `deriveMessages()` 从日志重投影。
循环近乎无状态 —— 这是压缩、恢复、fork、回放全部免费的根因。

## 二、循环如何可替换：两层

**接口层**：`Agent` + `AgentFactory` 住在**不含实现**的 `core/agent` 包
（`packages/core/agent/src/runtime-types.ts:64-144`）；`core/agent-loop` 在构造时
`ctx.effect(() => ctx.agents.setFactory(this))`（`agent-loop/src/index.ts:350`）注册为默认工厂。
消费方只用 `ctx.agents.create()/resume()`，**从不 import 具体循环**。

```ts
export interface Agent {
  readonly id: SessionId; readonly session: Session; readonly inbox: Inbox
  readonly status: AgentStatus; readonly ctx: Context      // agent 作用域子上下文
  cancel(cause, options?): void
  whenIdle(): Promise<void>
  runMaintenance<T>(task: (signal: AbortSignal) => Promise<T>): Promise<T>
  followup(m: UserMessage): void   // 排下一轮 + 唤醒
  steer(m: UserMessage): void      // 插到最近 step 边界
  inject(m: UserMessage): void     // 只塞上下文，不唤醒
}
```

**组合层**：`dsh --dump-config` 打出的每个条目都能被 `cordis.patch.yml` 按 id 整体替换。

**不同策略不是换 Runner，而是分层实现**（这点最反直觉）：

| 策略 | 实现方式 |
|---|---|
| 单轮 vs ReAct | 同一循环自然表达：无 tool-call → completed；工具结果带 `concludesTurn:true` 可数据驱动提前终止 |
| plan-execute | `plan-mode` 插件挂 `agent/pre-step` waterfall 改写/拒绝步骤 |
| 多轮目标续跑 | `goal-round-driver` 监听 `agent/status`，quiescence 时 `followup()` 排下一 round（`goal-round-driver/src/index.ts:137-175`），**不动循环** |
| subagent 递归 | `subagent-in-process-driver` 用同一工厂再造 agent，`child.followup(p); await child.whenIdle()` 取末条输出 |

## 三、工具管线 = 固定骨架 + 三个 waterfall 插槽

骨架（记录、归一化、材料化）由 `ToolRuntime` 硬编码；所有策略全是外挂监听器
（`docs/tool-execution-pipeline.zh.md:8-62`）：

```
session 记 tool/call
→ tools/pre-execute   waterfall  hook PreToolUse / 权限 / 沙箱策略；返回 allow|deny|ask
→ ctx.approval 一次性询问        可选服务；缺失时 ask 落为 deny（tools/index.ts:1693-1697）
→ 单调 guards                    ctx.tools.guard(fn)，只能 deny 或弃权（tools/index.ts:1110）
→ tools/execute       waterfall  环绕分发：timeout 包 deadline、retry、metrics
→ ToolDefinition.execute()       fs 写入还要过 fs/write-intent、fs/edit-intent waterfall
→ tools/post-execute  waterfall  accept|block|replace + additionalContexts；spill 外溢、截断挂这
→ registry 归一化                无损 JSON 快照，throw → isError
→ finalizeContent                定义自有的最后变换
→ tools/result        emit       冻结结果通知
→ session 记 tool/result         模型可见的唯一结果
→ additionalContexts 按 FIFO 注入为 user/message
```

内部调度器 `ToolRuntimeScheduler`（`prepare/dispatch/finalize/finish`，`tools/index.ts:451-466`）是
Symbol 键的**内部**接口，**刻意不是插件点** —— 它保证「pre 有序、body 并发、post/result 按模型顺序提交」。

## 四、上下文管理 = contributor 模式

两条供给线：

1. **System prompt / 工具 schema**：`ctx.systemPrompt` 四种注册（`system-prompt/src/index.ts:381-467`）
   `section()` 有序静态片段 / `context()` 动态快照 / `variable()` `{{var}}` 插值 / `tools()` schema 提供方。
   每步开头 `assemble()` 走 `system-prompt/assemble` waterfall 让专家插件最后改写整个装配。
2. **消息历史**：不是谁"贡献 messages"，而是**模型可见即已记录**。插件注入上下文的唯一通道是
   `agent.inject()` → inbox next-step → 下次 `agent/pre-step` 认领 → 记为 `user/message`。

**压缩不属于循环主干**，是 seam（`ctx.compaction` + compaction-basic + command-compact），两个触发器都只是监听器：

```ts
ctx.on('agent/pre-step', async ({ agent, signal }, next) => {          // ① token 预算压力
  await this.compactIfNeeded(agent, 'pressure', signal); return next()
})
ctx.on('agent/request-error', async ({ agent, failure, signal }, next) => {   // ② 供应商确认的溢出
  if (failure.code !== CONTEXT_WINDOW_EXCEEDED_CODE) return next()
  await this.compactIfNeeded(agent, 'context-overflow', signal)
  // surface generation 前进 ⇒ 返回 { kind: 'retry' } 让循环重试本步
})
```

摘要通过 `user/message` + `surfaceOp: {op:'replace', start, end}` 遮蔽旧 surface 节点。

## 五、中断 / 恢复 / 并发

- **中断**：`cancel(cause)` → 清 inbox + `phase.abort.abort()`。单一 AbortSignal 贯穿 pre-step、模型流、每个工具。
  关键：中止后**未启动的工具调用写入合成错误结果**（`tool-calls.ts:249-258`），保证日志里 call/result 永远配对、可回放。
- **恢复**：`persistence.prepare(id)` 载入日志 → 新建 agent；**inbox 也事件溯源**——重放
  `agent/inbox/spliced` 恢复未消费输入（`inbox.ts:32-39`）；模型路由从 `request/header` 事件恢复。
- **并发**：按 `executionMode` 分组（`tool-calls.ts:84-99`）：非并发安全的调用形成**独占屏障**，
  `isConcurrencySafe(args)===true` 的进**有界滚动池**（`maxParallelToolCalls`，settings 热更新）；
  每次启动前**重新分类**；结果严格按模型顺序 `commitReady()` 提交。

## 六、调度（schedule 包）不是 cron

三种规则 `after` / `at` / `every`（≥300s，无 cron 表达式）。持久权威是 `schedule/change` 会话事件。
与循环的关系是**纯消费方**：到期工作等 agent 完全 idle → `runMaintenance` 认领空闲槽 → 追加 dispatch 事件
→ `followup()` 排一个普通后续轮次 ——「**绝不调用 steer()，绝不中断当前轮次**」（`schedule.zh.md:184`）。
冷会话不跑 timer，重开后过期目标标 overdue，每条 every 只补最新一次（at-least-once）。
通用后台任务在 `packages/jobs`（`ctx.jobs`），与循环并行存在。

## 七、事件到 UI

`packages/host/apiproxy` 订阅 `ctx.on('session/event', ...)`，加 view 投影后压帧队列，
经 **SSE**（`apiproxy/src/fetch/handler.ts:234`，推帧 `api-proxy.ts:3475-3493`）推给 web 客户端。
SDK 指引：**回放读 `session/event`，实时控制用 `agent/*`**。
