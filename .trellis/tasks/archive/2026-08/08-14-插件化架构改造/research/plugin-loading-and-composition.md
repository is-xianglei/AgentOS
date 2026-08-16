# 研究：插件的发现、加载、配置与组合

来源：`/Users/xianglei/Desktop/deepseek-harness/apps/cli/`、`packages/boot/`、`packages/bundle/`、`packages/preset/`。

## 一、没有插件注册中心，也没有运行时扫描

插件集完全由**分层 patch 组合**声明式决定。每层是一个 YAML patch 数组，逐层叠加在一个
**内容恒为 `[]` 的空根条目列表**之上，最终得到条目树交给 Loader 挂载。

发现机制 = **pnpm 依赖闭包 + `package.json` 的 `dsh.bundle` / `dsh.client` 清单字段**。
没有 `dsh-plugin-*` 命名规范（`dsh-plugin` 只是 README 建议的 GitHub topic，不参与程序发现）。

## 二、启动全流程（`dsh web` = `--profile web`）

```ts
// packages/boot/app-boot/src/index.ts:757-784
export async function boot(binName, absoluteConfigPath, patches?, prepare?, ...) {
  const ctx = new Context()                       // ← 谁创建根 Context：就是这里
  ctx.baseUrl = pathToFileURL(dirname(absoluteConfigPath)).href + '/'
  ctx.provide('dshHomePath', dshHomePath)
  await ctx.plugin(Loader)
  await prepare?.(ctx)                            // 提供 launch-env 快照 + cmdlineArgs
  await mountRootInclude(ctx, absoluteConfigPath, patches, ...)
  await ctx.get('loader')?.await()
  await assertEntriesActivated(ctx, binName)      // 全部 fiber 必须 ACTIVE，否则列名报错退出
  return ctx
}
```

**patch 栈应用顺序（后者胜）**（`apps/cli/src/profile-boot.ts:122-171`）：
`dsh-base` → `dsh-web-app` → profile 自己的 `cordis.patch.yml`（用户层）
→ `$DSH_HOME/cordis.patch.yml`（机器级）→ `--patch` overlay → agent-presets 根注入 → telemetry 开关。

profile 模板（`packages/boot/app-boot/src/profile.ts:114-117`）：
```ts
export const PROFILE_TEMPLATES: Record<string, readonly string[]> = {
  web:      ['@deepseek-ai/dsh-base', '@deepseek-ai/dsh-web-app'],
  headless: ['@deepseek-ai/dsh-base', '@deepseek-ai/dsh-headless'],
}
```

**挂载无固定顺序**——base patch 头注释明写「Row order carries no load semantics
(activation is service-availability driven)」。命令行 flag 通过普通插件链传递：
`web-startup` 行解析 `--host/--port` 后提供 `webStartup` 服务，`webserver` 行
`inject: [webStartup]` 且 `port: !!js ctx.webStartup.port ?? 3080` —— **flag 天然优先于配置，
不需要专门的优先级代码**。

## 三、三个平面的组合，各自可整包替换

| 平面 | 载体 | 内容 | 替换方式 |
|---|---|---|---|
| **进程平面** | bundle（`dsh-base` / `dsh-web-app` / `dsh-headless`） | base 一个 `insert` 约 70 行插件 | `dsh plugin add <自己的 bundle>`；bundle 包本体几乎无代码（`bundle/base/src/index.ts` 全文是 `export {}`） |
| **agent 平面** | agent preset（`standard`/`code`/`minimal`/`cordis`） | `agent.cordis.yml`（252 行组合）+ `preset.yml`（元数据） | 用户在 `$DSH_HOME/.agent-presets/<id>/` 放同构目录；**运行中新建 preset 无需重启即可见** |
| **浏览器平面** | `dsh.client` 清单 + 第二个 Cordis Loader | 约 30 个 `ui-*` 行 | 删行即拔掉该 UI 面 |

**web bundle 的关键动作**：把 base 的**全部 agent 平面行 disable**（tool-bash/tool-fs/plan-mode/
compaction…），末尾插入 `agent-presets`（`default: standard`）—— agent 平面整体移交 preset。

preset 内的服务必须放进带 `isolate` realm 的 `cordis:group`，防止发布进根 realm 造成**跨会话冲突**。
挂载时机：agent **发布前**的 `setup(agentCtx)` 回调里 `mountPreset()`；每 preset 进程内只挂一次
（standing scope），会话按 scope 父子关系「加入」；失败则整个会话创建回滚
（`packages/preset/agent-presets/src/mount.ts:1-15`、`packages/host/apiproxy/src/api-proxy.ts:1212-1243`）。

## 四、配置格式

三份文件，全部热重载：

| 文件 | 作用 | 热更新 |
|---|---|---|
| `$DSH_HOME/profiles/<P>/cordis.patch.yml` | 选插件 + 给参数 | ✅ chokidar watch → 事务性重组整个 patch 栈 → `entry.update()` |
| `$DSH_HOME/settings.yaml` | 运行时设置（Web 设置页写这里） | ✅ |
| `$DSH_HOME/.credentials.yaml` | 凭证 | ✅ |

patch 条目语法：`id` 定位行、`config` **整体替换**（不深合并）、`disabled` 关行、`insert` 加行、
`!!js` 表达式可用（如 `disabled: !!js process.platform === 'win32'`）。

**边界**：`dsh plugin add/remove` 改变 bundle 层列表需要重启；用户 patch 层是活的。

`docs/config-catalog.zh.md` 由 `scripts/gen-config-catalog.ts` 从各插件 schema 生成，
逐包列出 `config:` 块与 `Requires:` 注入服务。
`dsh --profile web --dump-config` 打印带来源注释的组合树而不启动。

## 五、第三方插件约定（两级）

**插件模块级**（纯 Cordis，无 dsh 特有基类）：
```ts
import type { Context } from '@deepseek-ai/cordis'
export const name = 'hello-plugin'
export function apply(ctx: Context) { /* register capabilities */ }
```

**分发包级**：
```json
{ "name": "dsh-hello-plugin", "type": "module", "main": "index.js",
  "files": ["index.js", "cordis.patch.yml"],
  "dsh": { "bundle": { "patch": "./cordis.patch.yml" } } }
```

`dsh plugin --profile <P> add <npm名|github:owner/repo#sha|tarball|本地目录>`
本质是 pnpm 转发器；安装后 `reconcilePlugins`（`apps/cli/src/plugin.ts:59-91`）按**已安装状态**对账：
声明 `dsh.bundle` 的依赖追加进 `bundles`；无声明则保留为普通库依赖并警告一次；
新版本丢了声明则移出层栈。本地开发免打包：`--patch` overlay 里 `name` 直接写绝对路径的 `.ts`。

## 六、UI 是插件（前端即第二个 Cordis 运行时）

1. host 侧 `dsh-client-modules` 扫描 Loader 条目：凡 manifest 声明 `dsh.client`
   （`platform:'web'`、可选 `inject` 图边、`immediately` 预取）且 `exports["./client"]` 指向浏览器 bundle 者，
   组成 `window.__DSH_BOOT__` 条目图注入 index.html，按 `/plugins/<包名>/client.js?rev=<hash>` 服务。
2. 浏览器 shell kernel（`packages/client/web/src/boot.tsx:14-33`）：解析 `__DSH_BOOT__` →
   预取 `immediately` 层 → **挂 vendored Cordis Loader** → 每插件行建一个 entry →
   `loader.await()` + 全 fiber ACTIVE 审计 → 切真实 UI。「shell 零组合决策」，组合全来自 host 图。
3. UI 贡献机制 = **slot 注册表**（`packages/client/ui-slots/src/index.ts:1-25`）：
   所有者用 declaration merging 在 `SlotMap` 声明槽契约，插件 `register` 一次即贡献组件。

## 七、Python SDK 只能做客户端，不能写插件

`python/sdk/src/` 全部源码零 plugin 相关 API。两个包都是**进程外客户端/载体**：
- `deepseek-harness-sdk`：`subprocess.Popen` 拉起运行时，走 **stdio 上按行分隔的 JSON-RPC 2.0**
  （`initialize` / `session/prompt` / `shutdown` + `session.event` 等四种通知）。
- `deepseek-harness-runtime-bin`：按平台打包 `pkg` 单文件 Node 可执行 + 默认 `runtime/cordis.yml`。

Python 影响插件集的唯一方式：`DeepSeekHarness(cordis="path/to/*.cordis.yml")` 设 `DSH_CORDIS_CONFIG`
——但组合里的行仍必须是 Node 可加载的 Cordis 插件。

**Python 接入 agent 能力的正路**：写 MCP 服务器（经 `dsh-mcp-client` 注册为 `mcp__<server>__<tool>`），
或作为被 bash 调用的普通程序，或用 SDK 做编排客户端。
