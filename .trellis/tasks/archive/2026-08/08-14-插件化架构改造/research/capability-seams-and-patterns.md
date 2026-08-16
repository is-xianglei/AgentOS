# 研究：能力 seam 目录与插件编写范式

来源：`/Users/xianglei/Desktop/deepseek-harness/docs/capability-seams.zh.md`、`packages/`。

## 一、seam 的定义：三角色缺一不可

一个 seam = **Service Definition**（Cordis `Service` 抽象类或具体注册表，**绝不是 TS interface**，
拥有稳定的 `ctx.<key>` 与词汇类型）+ **Service Provider**（一或多个实现）+ **Consumer**（通常是面向模型的工具）。
`docs/glossary.zh.md:9`：「seam 是完整能力，绝不是其中一个角色。」

`docs/capability-seams.zh.md` 把每个 `ctx.<key>` 标为三种角色：
- `core` — 单实现主干（session / tools / systemPrompt / agents / commands / …）
- `seam` — 可替换能力（26 个）
- `bundle` — 组合点（**只有 `ctx.agentLoop` 一个**）

## 二、26 个 seam（默认实现加粗）

| ctx 键 | 定义类型 | 实现 |
|---|---|---|
| `ctx.llm` | `LlmRuntime` + 抽象 `LlmAdapter` | **llm-deepseek**、llm-pi-ai、llm-replay |
| `ctx.sessionPersistence` | 抽象 `SessionPersistence` | **jsonl**、sqlite |
| `ctx.settings` | 抽象 `SettingsProvider` | **settings-file** |
| `ctx.credentials` | 抽象 `CredentialProvider` | **credentials-local**（env/.env） |
| `ctx.storage` | `Storage` 枢纽 + `StorageBackend` | **json**、sqlite（可并列多后端） |
| `ctx.sessionQuery` | 会话读取/过滤/搜索 | **sqlite**（全文检索） |
| `ctx.sessionTitle` | 标题折叠 + 唯一异步提供方 | first-prompt-llm / all-prompts-llm |
| `ctx.sessionTelemetry` | 遥测脱敏管道 | otel |
| `ctx.attachments` | 持久二进制附件 | **local**（内容寻址） |
| `ctx.skills` | `SkillRegistry` + `SkillProvider` | **filesystem**、badge |
| `ctx.subprocess` | 抽象 `SubprocessRuntime` | **local**、e2b |
| `ctx.shell` | 抽象 `ShellExecutor` | bash-local、**bash-sandbox**、pwsh-local |
| `ctx.terminals` | `TerminalSessionService` | **terminal-bash**（PTY） |
| `ctx.sandbox` | 抽象 `SandboxProvider` | **local**（bwrap/Landlock/Seatbelt/Win-ACL） |
| `ctx.fs` | 抽象 `FileSystem` | local、**sandbox**、e2b |
| `ctx.approval` | `approval/request` waterfall | acp；**无应答方 → 拒绝** |
| `ctx.codeRuntime` | 抽象 `CodeRuntime` | **worker-thread**（Code Mode） |
| `ctx.compaction` | 抽象 `CompactionEngine` | **basic** |
| `ctx.subagents` | `SubagentRuntime` + `SubagentProvider` | **spawn-in-process**、fork-in-process、acp、codex、claude-code、dsh-sdk |
| `ctx.jobs` | 抽象 `JobRegistry` | **local** |
| `ctx.web` | `WebRuntime` + Search/Fetch Provider | exa / perplexity / deepseek、**fetch-http** |
| `ctx.spillStore` | 抽象 `SpillStore` | **local** |
| `ctx.directoryPicker` | 判别式目录选择 | native、browse |
| `ctx.workflowEngine` | 抽象 `WorkflowEngine` | **worker-thread** |
| `ctx.lsp` | `Lsp implements LspService` | **local** |
| `ctx.userQuestions` | 人工问答提供方席位 | UI 前端注册 |

**关键观察**：seam 接口都极小。例如整个沙箱能力只有一个方法：
```ts
abstract class SandboxProvider extends Service {
  constructor(ctx: Context) { super(ctx, 'sandbox') }
  abstract confine(argv: readonly string[], policy: SandboxPolicy): ConfinedArgv
}
```
拿到即将 spawn 的 argv，按策略**包装 argv**。策略本身归 `sandboxPolicy` 单独持有——
bash 与 fs 才不会限到不同根目录。

**执行世界成组替换**：fs 与 subprocess 共享同一执行世界，两个 provider 一起指向 e2b，
Bash/PTY/LSP 一并搬进远程沙箱，**无需为每个工具做 provider 专用 fork**（`architecture.zh.md:106`）。

## 三、两种多实现机制，选型判据明确

**机制一：同名服务槽（独占）** —— sandbox / subprocess / fs / shell / persistence 用这个。
接缝包只定义抽象 Service 类，名字定死在抽象类构造器里；**同 realm 二次 provide 直接 throw**：
> "one implementation per context; loading a second throws"（`subprocess/src/index.ts:75-78`）

「自由替换」发生在**组合层**——换掉 patch 文件里的一行 `name:`：
```yaml
- id: subprocess
  name: '@deepseek-ai/dsh-subprocess-local'   # 换成 dsh-subprocess-e2b 即整体切远程沙箱
```

**机制二：registry 注册（多实现共存路由）** —— llm / storage / skill 用这个。
接缝本身是**具体** Service，内部持 Map，实现插件调 `registerAdapter/register/registerProvider`，
重名 throw，请求按 key 路由。storage 明写「哪个后端服务哪个消费者是消费者的配置，
绝不是 hub 全局选择」。

**判据**：一次只该有一个胜者、消费者不需要选择 → 服务槽；需要按 key 路由或多份并存 → registry。

## 四、插件编写范式

**两种形态分工**：提供服务的包用 class 插件（默认导出 `Service` 子类）；
只消费/挂监听的包用函数插件（named exports：`name` + `inject` + `Config` + `apply`）。
**有 `Config` 的函数插件必须用 named exports**，default 导出会被 Loader 解包丢掉 schema。

最小完整服务定义（`packages/e2b/e2b/src/index.ts:63-95`）：
```ts
declare module '@deepseek-ai/cordis' {
  interface Context { e2b: E2BRuntime }        // ① 类型增强
}
export class E2BRuntime extends Service {      // ② class 插件
  static Config: z<Config> = z.object({        // ③ Loader 静态读取的 schema
    apiKey: z.string(),
    cwd: z.string().default('/home/user/workspace'),
  })
  constructor(ctx: Context, config: Config) {
    super(ctx, 'e2b')                          // ④ 注册为 ctx.e2b，随 fiber 卸载
  }
}
```

消费（`packages/llm/llm-deepseek/src/index.ts:41-42, 250-256`）：
```ts
export const name = 'llm-deepseek'
export const inject = ['llm']                  // 硬依赖：apply 推迟到 ctx.llm 存在
export function apply(ctx: Context, config: Config): void {
  const registration = ctx.llm.registerAdapter([PROVIDER], adapter)
}
```
三种姿势：**硬依赖**写进 `inject`；**软依赖**用 `ctx.get('credentials')` 判空降级；
**多服务消费者**列多个名字。

## 五、依赖规则（CI 强制）

「**扩展插件依赖 Service Definition，绝不依赖具体 provider**」（`packages/README.md`）。
模块图印证：依赖 `agent-loop` 的**只有示例包**；`tool-bash` 依赖 `shell`（Definition）而非 `bash-local`。

铁证（`packages/shell/tool-bash/src/index.ts`）：
```ts
import type {} from '@deepseek-ai/dsh-jobs'   // :18-21 空类型导入，零运行时代码
export const inject = ['tools', 'shell', 'systemPrompt', 'shellEnv']   // :31 以服务名声明
ctx.shell.start(ctx.shell.resolve(request))   // :370 运行时实例从 ctx 取
```
即：**编译期依赖 Definition 包的类型，运行期只依赖 ctx 键。**

横向依赖未一刀切禁止（provider 可依赖兄弟 provider 复用实现，如 `bash-sandbox → bash-local`），
但方向必须向 Definition 收敛。反向影响不回头 import 而**走能力事件**——
fs-observation-policy 不被 tool-fs 依赖，而是监听 `fs/write-intent` waterfall 行使否决。

## 六、测试范式：真内核 + 从正规扩展点插入的 Fake

不 mock 框架、不 monkey-patch。起一个真 Context，挂真服务栈：
```ts
async function harness(config = {}): Promise<Context> {
  const ctx = new Context()
  await mountAgentLoopTestDependencies(ctx)     // 真 LlmRuntime/SessionStore/SystemPrompt/ToolRuntime
  await ctx.plugin(AgentLoop, { agents: [] })
  await ctx.plugin(RepeatToolGuard, config)     // 被测插件按生产方式加载
  return ctx
}
ctx.llm.registerAdapter(['mock'], new MockAdapter([...脚本]))   // Fake 走正规 registry
```
或直接 waterfall 短路：`ctx.on('llm/stream', (options, _next) => replay(options))`（不调 `next()` 即整层替换）。

**「用 Fake 替换真实插件」= 换一个注册到同一扩展点的实现**，与生产替换同一机制，
所以测试拓扑与生产拓扑同构。
