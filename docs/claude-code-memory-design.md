# Claude Code Memory 系统设计文档

> 基于 claude-code 逆向 TypeScript 源码(`claude-code-rev-main`)的深度分析。
> 本文覆盖存储、召回、自动提取、CLAUDE.md 层级、上下文注入、Team 记忆、Agent 记忆七大子系统。

---

## 0. 一句话总览

Claude Code 的记忆系统是一个 **「每个 Git 仓库一份、以 Markdown 文件为存储、以 `MEMORY.md` 为索引、由子 LLM 做相关性挑选召回、按文件 mtime 标注时效、统一以 `<system-reminder>` 注入」** 的持久化系统。

它的核心设计哲学有三条:

1. **存储对模型透明,召回由主进程编排。** 模型只用 `Write`/`Grep`/`Read` 这些普通文件工具读写记忆,它感知不到扫描、相关性打分、去重、时效标注这些「魔法」——这些全部发生在主进程侧,不消耗任何 model turn。
2. **索引常驻,内容按需。** 粗粒度的 `MEMORY.md` 索引每次会话开局就进上下文;细粒度的 topic 文件只在与当前 query 相关时,由一个便宜的 Sonnet 侧调用挑出最多 5 条注入。
3. **记忆 ≠ 指令。** 模型写给「未来的自己」的是 memory(`memdir`);人写给模型的是 instructions(`CLAUDE.md`)。二者走同一条注入通道,但语义边界严格分开。

---

## 1. 两条平行通道:memdir vs CLAUDE.md

记忆相关的信息其实由**两套独立系统**承载,理解它们的分工是理解全局的前提。

| 维度 | CLAUDE.md 层(instructions) | memdir(auto memory) |
|---|---|---|
| 归属 | **人写**,签入仓库或本地 | **LLM 自己写** |
| 目的 | 指令 / 约定(你必须遵守) | 事实 / 偏好 / 上下文(供参考) |
| 位置 | 项目内多级目录 + `~/.claude/` | `~/.claude/projects/<git-root>/memory/` |
| 是否层级 | 是,从 cwd 向上多级发现 | 否,每仓库一份 |
| 加载方式 | eager 全量加载文件正文 | eager 只加载 `MEMORY.md` 索引,正文按需召回 |
| 分类约束 | 无 | 严格 4 类:user / feedback / project / reference |
| 更新周期 | 手动 | 每 N 轮对话自动提取 |

设计意图:**CLAUDE.md 是「用户 → 模型」的意图声明通道,memdir 是「模型 → 未来的自己」的知识沉淀通道。** 两者都注入到第一条 user message,但 `memoryTypes.ts` 明确规定「已经写在 CLAUDE.md 里的内容」不该再存进 memdir。

---

## 2. 数据结构与存储

### 2.1 单条 memory 的物理格式

每条 memory 是一个独立的 `.md` 文件,带 YAML frontmatter(样板 `MEMORY_FRONTMATTER_EXAMPLE`,`memoryTypes.ts:261`):

```markdown
---
name: {{memory name}}
description: {{one-line description — used to decide relevance in future conversations, so be specific}}
type: {{user, feedback, project, reference}}
---

{{memory content — for feedback/project types, structure as: rule/fact, then **Why:** and **How to apply:** lines}}
```

三个 frontmatter 字段分工明确:

- **`name`** — 人类可读标题,只在索引里用。
- **`description`** — **最关键的字段**。它是「召回时给挑选器 LLM 看的一句摘要」。写不好这条 memory 就永远召不回来。
- **`type`** — 四选一枚举,`parseMemoryType` 做白名单校验;无效值 fallback 到 `undefined`(兼容 legacy 无 type 的文件)。

内存中的表示(`memoryScan.ts`):

```ts
type MemoryHeader = {
  filename: string
  filePath: string
  mtimeMs: number      // ← 时效由文件系统 mtime 驱动,而非 frontmatter 里的 date 字段
  description: string | null
  type: MemoryType | undefined
}
```

**注意:没有创建/更新时间字段。** 所有「新旧」判断都用文件系统 mtime,这是刻意选择(见 §5 时效)。

### 2.2 四种类型(封闭 taxonomy)

`memoryTypes.ts:14` 定义了**封闭的**四元组,这是整个系统的「灵魂」:

```ts
export const MEMORY_TYPES = ['user', 'feedback', 'project', 'reference'] as const
```

| 类型 | 语义 | 何时写 | body 结构约定 |
|---|---|---|---|
| **user** | 用户角色 / 偏好 / 知识背景 | 学到用户身份、技术栈、职责 | 无 |
| **feedback** | 用户对「如何工作」的反馈 | 用户纠正("no not that")**或**确认("yes exactly") | `规则 → **Why:** → **How to apply:**` 三段式 |
| **project** | 项目当前状态 / 决策 / 时间线 | 学到 who/why/when,**相对日期转绝对日期** | `事实/决定 → **Why:** → **How to apply:**` |
| **reference** | 外部系统指针(Linear / Grafana / Slack) | 学到「某类信息去某处找」 | 无 |

**为什么强制封闭四类?** 文件头注释点明:开放式记忆容易退化成「活动日志」。`WHAT_NOT_TO_SAVE_SECTION` 明确排除:

- 代码模式、架构、文件路径、目录结构(grep/git 可推导)
- git 历史(`git log`/`git blame` 就够)
- 已修复的 bug 解法(代码 + commit message 就够)
- 已经写在 CLAUDE.md 里的内容
- 临时任务细节、当前对话上下文

而且有一条**「用户显式要求也不例外」**的兜底门:

> 如果用户让你保存 PR 列表或活动摘要,反过来问他这里面什么是**令人意外的**或**非显而易见的**——那才是值得留下的部分。

注释里标注了 eval 数据(`case 3, 0/2 → 3/3`),说明这条是被评测驱动加进来的。

### 2.3 `MEMORY.md` 索引文件

- 文件名常量 `ENTRYPOINT_NAME = 'MEMORY.md'`。
- **它不是 memory,而是指向 memory 的索引。**
- 双重上限:`MAX_ENTRYPOINT_LINES = 200` 行 / `MAX_ENTRYPOINT_BYTES = 25_000` 字节(字节上限是为了抓「行数没超但单行几千字」的失控情况,线上真实观测过 197KB / 200 行以内)。
- 截断逻辑 `truncateEntrypointContent`:先按行切,再按字节切到最后一个换行,末尾追加带原因的 WARNING。

对模型的约束(prompt 原文):

> `MEMORY.md` is an index, not a memory — each entry should be one line, under ~150 characters: `- [Title](file.md) — one-line hook`. It has no frontmatter. Never write memory content directly into MEMORY.md.

**`MEMORY.md` 每次会话开局就进上下文**(通过 CLAUDE.md 加载器搭便车,见 §6),对模型而言是「开局就在脑子里的目录」,无需 tool 调用;而具体 topic 文件要靠召回或 grep 才能加载。

---

## 3. 存储路径

### 3.1 目录计算公式

核心是 `getAutoMemPath()`(`paths.ts`,memoize 缓存,key 为 `getProjectRoot()`)。解析优先级:

1. 环境变量 `CLAUDE_COWORK_MEMORY_PATH_OVERRIDE`(Cowork / SDK 场景强制指定)
2. `settings.json` 的 `autoMemoryDirectory`(**仅信任 policy / local / user 三档,故意排除 `projectSettings`**——防止恶意 repo 把它设成 `~/.ssh` 之类拿到写权限)
3. 默认公式:

```
~/.claude/projects/<sanitized-canonical-git-root>/memory/
```

- 基础目录默认 `~/.claude`,可被 `CLAUDE_CODE_REMOTE_MEMORY_DIR` 覆盖(远程/容器开发)。
- 用 `findCanonicalGitRoot` 而非 cwd,使得**同一 repo 的所有 worktree 共享同一份记忆**。

### 3.2 目录结构

```
~/.claude/projects/<repo>/memory/
├── MEMORY.md                       # 索引,每次进上下文
├── user_role.md                    # 单条 memory:frontmatter + body
├── feedback_testing.md
├── project_auth_rewrite.md
├── reference_linear.md
├── team/                           # Team 记忆子目录(见 §7)
│   └── MEMORY.md
└── logs/YYYY/MM/YYYY-MM-DD.md      # 仅 KAIROS(永续 session)模式
```

命名规则不硬性强制,但 prompt 提示模型按 `<type>_<topic>.md` 起名。

### 3.3 路径安全

`validateMemoryPath` 拒绝一批危险输入:相对路径、根/近根路径、Windows 盘根(`C:`)、UNC 路径(`\\server`)、含 null byte、`~/.` 之类会展开成 `$HOME` 的花招。`isAutoMemPath()` 用于文件系统读写策略层的白名单判断。

---

## 4. 召回机制(核心)

这是整个系统最精巧的部分。召回**不是**关键词匹配,**不是**向量语义检索,**不是**全量加载,而是 **「LLM-in-the-loop 挑选」**:用一个便宜的 Sonnet 侧调用,把候选文件的 manifest 和用户 query 丢过去,让它返回最多 5 个文件名。

### 4.1 触发时机:异步 prefetch

`startRelevantMemoryPrefetch(messages, ctx)` 在**每个用户回合开始时**触发。门控条件:

1. `isAutoMemoryEnabled()` 通过
2. GrowthBook 特性 `tengu_moth_copse` 开启
3. 存在最后一条 non-meta user 消息
4. **输入包含空格**(单个词 query 语义不足,直接跳过)
5. 本 session 累计 surfaced 字节 < `MAX_SESSION_BYTES`(60 KB)

**关键词是 "prefetch"**:它不阻塞回合,**在主模型 stream 和工具执行的同时并行跑**。通过 `using` 语义绑定,回合结束自动 abort 未完成的请求;查询点「就绪则消费,未就绪则跳过、下轮再试」。

### 4.2 挑选流程(findRelevantMemories)

```
用户 query
   │
   ▼
scanMemoryFiles(dir)            ← 最多 200 条 header,按 mtime 降序
   │  (每文件只读前 30 行拿 frontmatter + mtime,单次 syscall)
   ▼
过滤掉 alreadySurfaced 里的 path
   │
   ▼
formatMemoryManifest → 文本列表
   │   - [feedback] feedback_testing.md (ISO时间戳): 集成测试必须打真实DB
   │   - [user] user_role.md (ISO时间戳): 资深 Go 工程师,前端新手
   ▼
sideQuery(default Sonnet, {
  system: SELECT_MEMORIES_SYSTEM_PROMPT,
  messages: [{user: `Query: ${q}\n\nAvailable memories:\n${manifest}${tools}`}],
  max_tokens: 256,
  output_format: JSON schema { selected_memories: string[] },
  skipSystemPromptPrefix: true    ← 不带主 system prompt,省 token
})
   │
   ▼
jsonParse → 校验文件名在候选集里
   │
   ▼
返回最多 5 条 [{path, mtimeMs}]
```

挑选器 system prompt 里两条值得注意的约束:

> - 如果你不确定某条 memory 对处理 query 有没有用,就**不要**选它。要有选择性、有判断力。
> - 如果提供了「最近用过的工具」列表,不要选这些工具的**用法参考/API 文档**类记忆(Claude 已经在成功用它们了)。**但仍要选关于这些工具的警告、坑、已知问题**——正在用它们时恰恰是这些最重要的时候。

「recentTools」由当前回合内**成功且未出错**的工具名收集而来。这是很精细的假阳性抑制:模型正在成功调用某工具就没必要塞它的文档,但警告类记忆照选。

### 4.3 数量与字节上限

| 约束 | 值 | 含义 |
|---|---|---|
| 候选池 | 200 | mtime top-200 文件 |
| 每次挑选 | 5 | 挑选器 slot 上限 |
| 每回合注入 | 5 | `.slice(0, 5)` |
| 每文件行数 | 200 | 注入时截断 |
| 每文件字节 | 4096 | 注入时截断 |
| 每回合总量 | ~20 KB | 5 × 4KB |
| 每 session 累计 | 60 KB | 超过则本 session 停止召回 |

排序:挑选器返回什么顺序就用什么顺序,**没有独立打分**,相关性完全交给 LLM。

### 4.4 三层去重

1. **挑选前**:过滤 `alreadySurfaced`,Sonnet 的 5 个 slot 不浪费在之前挑过的文件。
2. **挑选后**:再过滤 `readFileState`(模型自己 Read 打开过的)+ `alreadySurfaced`(多目录兜底)。
3. **surfaced 追踪**:扫描历史消息里所有 `relevant_memories` attachment,累加 path 与字节;**过 compact 后自动重置**(旧 attachment 已消失)。

### 4.5 呈现:内容截断而非丢弃

选中的文件用 `readFileInRange(path, 0, 200, 4096, ...)` 读取,被截断则末尾追加:

```
> This memory file was truncated (200 lines / 4096 byte limit).
  Use the FileRead tool to view the complete file at: <path>
```

设计取舍:**选中即注入,超限截断 + 指路**,而非「超限丢弃」——既然已被挑为最相关,frontmatter + 开头上下文就值得注入。

---

## 5. 记忆时效(memoryAge)

### 5.1 无过期、无衰减,只有「注解」

`memoryAge.ts` 全文只有三个纯函数,**没有任何「到期删除」或「分数衰减」逻辑**:

- `memoryAgeDays(mtimeMs)` = `max(0, floor((now - mtimeMs) / 86400000))`,负数(时钟偏移)clamp 到 0。
- `memoryAge(mtimeMs)` → `'today' | 'yesterday' | 'N days ago'`。
- `memoryFreshnessText(mtimeMs)` → 超过 1 天返回警告文本,否则空串。

核心设计意图(注释原文):

> 模型不擅长日期算术——原始 ISO 时间戳不会像 "47 days ago" 那样触发它对陈旧性的推理。

即 **「不给模型 ISO 时间戳,给它人话」**。

### 5.2 陈旧警告文本

超过 1 天的记忆,注入的 header 里会带:

> This memory is N days old. Memories are point-in-time observations, not live state — claims about code behavior or file:line citations may be outdated. Verify against current code before asserting as fact.

加这段的直接动机(注释)很反直觉:**具体的 `file:line` 引用反而让陈旧信息显得更权威**,用户报告过「代码早改了但模型还拿旧引用当事实断言」。

### 5.3 提示层的「信任但验证」配套

read-side 专门有个 `TRUSTING_RECALL_SECTION`(标题从抽象的 "Trusting what you recall" 改成动作导向的 **"Before recommending from memory"**,eval 3/3 vs 0/3——位置和触发词都重要):

- memory 里提到文件路径 → 先验证文件存在
- 提到函数名/flag → 先 grep
- 用户即将基于建议动手 → 先验证
- 索引类 memory(架构快照)→ 问「最近/当前」时优先 `git log`/读代码
- 若召回的 memory 与当前观察冲突,**信任当下所见**,并更新/删除陈旧记忆而非照做

---

## 6. 记忆的自动提取(extractMemories)

memdir 的自动写入器。它是**后台兜底**,与主模型自己的就地写入互斥。

### 6.1 触发时机:stop-hook,不是「会话结束」

提取器在**每次 query loop 结束**(模型给出无 tool_use 的最终回复)时,由 stop-hook fire-and-forget 触发。门控:

- `feature('EXTRACT_MEMORIES')`(编译期开关)
- **`!toolUseContext.agentId`**——只有**主 agent** 触发,子 agent 完全跳过
- `isExtractModeActive()`(GrowthBook flag)
- `isBareMode()` / `-p` 脚本模式整个跳过
- 再过一遍 `isAutoMemoryEnabled()` 与远程模式检查

### 6.2 节流与并发:每 N 轮一次,重叠则 coalesce

closure 里维护若干状态:

- `lastMemoryMessageUuid` — **游标**,只处理「上次提取以来」的新增消息。
- `turnsSinceLastExtraction` — 每轮 +1,达到阈值(默认 1)才真跑。
- `inProgress` — 布尔锁。
- `pendingContext` — 跑的同时又被调,**只留最新 context**(旧的覆盖),当前跑完的 `finally` 里再跑一次 trailing 提取(trailing run 不再做 turns 节流)。
- `inFlightExtractions` — 退出前做 60s 软等待,尽量落盘。

### 6.3 主/副互斥:主 agent 写了,提取器就跳过

主 agent 的 system prompt 里**永远**带完整的「如何保存记忆」指令(见 §8),模型自己就可能 Write/Edit memdir。为避免重复,提取器开跑前扫一遍从游标起的 assistant 消息,只要发现有 `Write`/`Edit` 落在 `isAutoMemPath()` 内,就直接跳过并推进游标。

设计意图:**主 agent 是「同步、就地」写入者;提取器是「异步、后台」兜底**。二者永不并行改同一批消息,既避免竞态,又保证覆盖率。

### 6.4 用什么模型:主对话的「完美 fork」

**提取器不是独立新会话,而是主对话的一份 perfect fork**(`runForkedAgent`):

1. 复用主对话的 system prompt、messages、tools——**完全一致的前缀**。
2. 末尾追加一条 user message,内容是提取指令。
3. 用**相同模型**执行,**复用主对话已建好的 prompt cache**(典型命中 90%+)。

也就是说,提取用的是**主对话所用的同一个 Claude 模型**,没有单独的小模型。fork 参数:`skipTranscript: true`(不入主 transcript,避免 race)、`maxTurns: 5`(硬顶;良好行为 2-4 轮:读→写)。

### 6.5 提取子 agent 的工具白名单

子 agent 带一个 `canUseTool` gate 跑,语义是**只读 + 只能写自己家目录**:

| 工具 | 权限 |
|---|---|
| `Read`, `Grep`, `Glob` | 无条件允许 |
| `Bash` | 只允许只读命令(`ls/find/cat/stat/wc/head/tail`),`rm` 一律拒 |
| `Edit`, `Write` | 只有 `file_path` 满足 `isAutoMemPath()` 才允许 |
| 其他(MCP / Agent / 写类 Bash) | 一律 deny |

### 6.6 提取输入:增量 + 预挂载 manifest

- **增量**:prompt 明文告诉子 agent「分析最近约 N 条消息」,N 只数游标之后的可见消息。游标 fallback:若游标消息已被 compact 清掉,退化成「数全部」而非返回 0,避免永久瘫痪。
- **预挂载**:丢 prompt 前先 `scanMemoryFiles` + `formatMemoryManifest`,把已有文件清单拼进 prompt,子 agent 就能直接决定「更新已有 vs 新建」,省掉一轮 `ls`。

### 6.7 两步写入协议 + 落地

**Step 1** 每条记忆一个文件(frontmatter + body)。**Step 2** 往 `MEMORY.md` 加一行索引 `- [Title](file.md) — hook`。

提取完成后:

- 仅在**成功时**推进游标 `lastMemoryMessageUuid`(失败保持不动,下次重跑)。
- 从写入路径里过滤掉 `MEMORY.md`(索引更新是机械动作,不作为独立 memory 展示)。
- 若有真 memory 被写,通过 `appendSystemMessage` 往主对话追一条 `memory_saved` 系统提示,UI 渲染成通知让用户看到「哪些记忆被保存了」。

### 6.8 KAIROS 变体(永续 session)

`feature('KAIROS')` 下,不写 topic 文件,而是 **append-only** 写到 `logs/YYYY/MM/YYYY-MM-DD.md`;另有夜间 `/dream` skill 把日志蒸馏成 topic 文件 + `MEMORY.md`。这是给「永续 assistant session」的兜底:活期日志 + 定期清算。

---

## 7. CLAUDE.md 层级(instructions)

### 7.1 五级层级(不是三级)

`getMemoryFiles` 按**优先级从低到高**顺序加载(后加载的位置越靠后、权重越高):

| 优先级 | Type | 位置 | 说明 |
|---|---|---|---|
| 最低 | **Managed** | `/etc/claude-code/CLAUDE.md` + 托管 rules | 组织策略,永远加载,永不排除 |
| ↓ | **User** | `~/.claude/CLAUDE.md` + `~/.claude/rules/*.md` | 全局个人 |
| ↓ | **Project** | 从根到 cwd 逐层的 `CLAUDE.md`、`.claude/CLAUDE.md`、`.claude/rules/*.md` | 签入版本库 |
| ↑ | **Local** | 每层的 `CLAUDE.local.md` | gitignore 的私有 |
| 最高 | **AutoMem / TeamMem** | memdir 的 `MEMORY.md` 指针 | 记忆索引 |

**cwd 向上遍历**:一路 `dirname()` 到根,再**反转**从根往 cwd 加载,使 cwd 最近的目录写在最后、权重最高。

**每一目录里有三类 project 文件**:`<dir>/CLAUDE.md`、`<dir>/.claude/CLAUDE.md`、`<dir>/.claude/rules/*.md`。

**worktree 特判**:嵌套 worktree 会同时经过 worktree 根和主 repo 根;此时主 repo 里签入的 project 文件会被跳过(只保留 worktree 自己的检出),但主 repo 的 `CLAUDE.local.md` 仍加载。

### 7.2 `@import` 递归引用

CLAUDE.md 里可写 `@path` 引用其他文件:

- 语法:`@relative/path`、`@./x`、`@~/home`、`@/absolute`,支持 `@file.md#section`(取路径丢 fragment)。
- 用 `marked` lexer 分词,**只在叶子 text token 里生效**——代码块 / 行内代码里的 `@foo` 不会被误解;HTML 注释里的 `@path` 也会被剥离。
- 只允许文本类扩展名白名单,防止把二进制/PDF 塞进上下文。
- **最大深度 5**,`processedPaths` 集合 + 路径归一化防环。
- 外部路径(不在项目 cwd 下)默认拒绝,除非用户在 config 里批准;但 **User 类型的 CLAUDE.md 永远允许 external**(它本就是全局的)。
- 产物顺序:**先父后子**。

### 7.3 `.claude/rules/*.md` 的条件规则

`.claude/rules/` 里的 `.md` 如果 frontmatter 带 `paths:` 字段,就变成**条件规则**:只有当 Read/Edit 的目标文件路径 glob 匹配时才注入(通过 §8 的 nested_memory attachment 按需加载)。无 `paths:` 的是无条件规则,eager 全量进上下文。

### 7.4 排除、缓存与失效

- `claudeMdExcludes`(settings)用 picomatch 匹配排除,但**只作用于 User/Project/Local,Managed/AutoMem/TeamMem 永不排除**。
- `CLAUDE_CODE_DISABLE_CLAUDE_MDS` env 硬关。
- `getMemoryFiles = memoize(...)`,整棵树只加载一次。失效分两种:
  - `clearMemoryFileCaches()` — 纯正确性场景(worktree 切换),**不**触发 `InstructionsLoaded` hook。
  - `resetGetMemoryFilesCache(reason)` — 语义变化场景(compact 后重建),**触发** hook。
- **AutoMem/TeamMem 故意不触发 `InstructionsLoaded`**——它们是另一套记忆系统,不是 CLAUDE.md 意义上的 instructions。

### 7.5 关键巧思:MEMORY.md 搭 CLAUDE.md 的便车

memdir 的 `MEMORY.md` 是**通过 CLAUDE.md 加载器进入 user context** 的,而非走系统提示词。`getMemoryFiles()` 返回数组末尾会追加 `type: 'AutoMem'` / `type: 'TeamMem'` 两条,和 CLAUDE.md 们并列——**这就把记忆索引和 CLAUDE.md 编成了同一条注入通道**。AutoMem/TeamMem 内容还会经 `truncateEntrypointContent` 做 200 行 / 25KB 截断。

---

## 8. 上下文注入(四条路径)

记忆相关的信息一共有**四条注入路径**,载体、频率、cache 友好度各不相同。全局统一的包装是 `<system-reminder>` XML 标签 + user message(不是 system message)。

```
system prompt = [
  identity + guidance,
  ...
  loadMemoryPrompt()   ← 路径④:memdir「行为教学」,不含数据
]

messages = [
  # 首条 user message(isMeta,注入一次)
  <system-reminder>
    # claudeMd
      ├ CLAUDE.md (project / user / managed / local)
      ├ MEMORY.md (auto)     ← 路径①:索引
      └ MEMORY.md (team)
    # currentDate: ...
  </system-reminder>,

  真人 user prompt,

  # 每轮按需追加
  <system-reminder>Contents of /nested/CLAUDE.md</system-reminder>,          ← 路径②:nested
  <system-reminder>Memory (saved 3 days ago): /user_role.md ...</system-reminder>, ← 路径③:召回正文
]
```

### 8.1 路径①:CLAUDE.md + MEMORY.md 索引 → 首条 user message

`getUserContext`(memoized)把所有加载到的 CLAUDE.md + MEMORY.md 索引拼成一段,由 `prependUserContext` 塞进**第一条 user message**:

```
<system-reminder>
As you answer the user's questions, you can use the following context:
# claudeMd
Codebase and user instructions are shown below. ...
Contents of /path/CLAUDE.md (project instructions, checked into the codebase):
<body>
Contents of /path/memory/MEMORY.md (user's auto-memory, persists across conversations):
<MEMORY.md body>
# currentDate
Today's date is 2026-07-04.
      IMPORTANT: this context may or may not be relevant ...
</system-reminder>
```

- **是 user message 而非 system message**,`isMeta: true`(不进人类可见 transcript)。
- TeamMem 额外用 `<team-memory-content source="shared">` 标签包住,便于下游区分共享内容。
- memoized,会话内不变(prompt cache 友好);`/compact`、`/clear` 才失效。
- GrowthBook 分流:`tengu_moth_copse` 组会**从这里摘掉 AutoMem/TeamMem 索引**,改由路径③按需召回。

### 8.2 路径②:nested_memory(深层 CLAUDE.md / 条件规则)

当 Read/Edit 触发到深层子目录时,把该目录及祖先的 CLAUDE.md / 条件规则打包成 attachment,渲染为:

```
<system-reminder>Contents of <path>:\n\n<content></system-reminder>
```

去重通过 `loadedNestedMemoryPaths`(非驱逐 Set)+ `readFileState` 双保险。

### 8.3 路径③:relevant_memories(召回正文)

§4 挑出的 memory 渲染为(每条一个 user message,统一包 `<system-reminder>`):

```
<header>

<memory content>
```

`header` 由 `memoryHeader(path, mtimeMs)` 生成:age>1 天带陈旧警告,否则 `Memory (saved today|yesterday|N days ago): <path>:`。

**关键:header 在 attachment 创建时就固化**(存进 `header?` 字段),渲染时不再调 `Date.now()`——否则「3 days ago」跨小时变「4 days ago」就会 cache miss。resumed session 无存储 header 时才 fallback 实时计算。

### 8.4 路径④:loadMemoryPrompt(行为教学,进系统提示词)

`systemPromptSection('memory', () => loadMemoryPrompt())`——注意它返回的**不是 memory 数据,而是教模型如何用 memdir 的行为指令**:类型定义、when to save、what NOT to save、两步写入、when to access、before recommending、how to search past context。约 100+ 行,和提取器看到的几乎同一份(只是语气从「你有记忆能力」改成「你现在是提取子 agent」)。

TEAMMEM 启用走 `buildCombinedMemoryPrompt`,KAIROS 走 `buildAssistantDailyLogPrompt`。这条走系统提示词,`/clear`、`/compact` 才失效。

> **在默认(非 SDK)模式下,memdir 的行为教学通过 `systemPromptSection` 进默认系统提示词。** 而在 SDK 自定义 system prompt 场景,只有设置了 `CLAUDE_COWORK_MEMORY_PATH_OVERRIDE` 时,才由 `QueryEngine` 显式把 `loadMemoryPrompt()` 拼进去——这是给外部集成方的显式 opt-in 信号。

### 8.5 MEMORY.md 是否每次全量加载?

**是,但有限制**:每次会话开始加载一次(memoize),写进首条 user message,受 200 行 / 25KB 硬顶。会话内不重载,除非 `/compact`(清 `getUserContext` 缓存)或 `/clear`。**topic 文件不全量加载**——只由路径③按 query 语义召回,最多 5 个。

---

## 9. Team 记忆(跨用户共享)

Team 记忆是同一 Git 仓库下**跨用户、跨机器、跨会话**共享的项目级知识库。

### 9.1 存储:auto 目录下的 team/ 子目录

```ts
getTeamMemPath() = join(getAutoMemPath(), 'team') + sep
// → <memoryBase>/projects/<repo>/memory/team/
```

- **不是独立根**,而是私有 auto 目录下的 `team/` 子目录;`isTeamMemoryEnabled()` 显式要求 auto memory 先开启。
- 额外由 GrowthBook flag `tengu_herring_clock` 灰度控制。
- team 目录有**自己的 `MEMORY.md`**,与 auto 的索引平行,两份都会载入上下文。

### 9.2 权威源在服务端,本地是 shadow copy

私有记忆纯本地、永不出机;Team 记忆的**权威副本在 Anthropic 服务端**,走 REST:

```
GET  /api/claude_code/team_memory?repo={owner/repo}             全量拉取
GET  /api/claude_code/team_memory?repo={owner/repo}&view=hashes  仅拉哈希(轻量探测)
PUT  /api/claude_code/team_memory?repo={owner/repo}             upsert 上传
```

- 仓库识别:`getGithubRepo()` 拿 `owner/repo`,**非 github.com 远程直接跳过**。
- 认证:**只支持 First-Party OAuth**(claude.ai 账号),API Key 用户不可用;校验 provider + base URL + scopes,请求头挂 `anthropic-beta` OAuth beta 通道。

### 9.3 数据模型:扁平 KV + per-key 校验和

```ts
TeamMemoryContent = { entries: Record<相对路径, 文本>, entryChecksums?: Record<路径, "sha256:..."> }
TeamMemoryData    = { organizationId, repo, version, lastModified, checksum, content }
```

- `entries` 是**扁平映射**(不是文件树),key 是相对 `team/` 的路径字符串。
- `entryChecksums` 支持 per-key delta 上传。
- 顶层 `checksum` 是整份数据的 ETag,用于 `If-Match` 乐观锁;`version` 是服务端递增整数。
- `SyncState`(由 watcher 拥有,非模块全局,便于测试):`lastKnownChecksum` / `serverChecksums`(本机相信的服务端 per-key hash)/ `serverMaxEntries`(从 413 学到的每仓库上限)。

### 9.4 同步机制:事件驱动 + 防抖,不轮询

- **启动**:先跑一次初始 pull(落盘服务端最新)**再**启 watcher——避免 pull 写盘反过来触发 watcher。
- **监听**:`fs.watch({recursive:true})`。为什么不用 chokidar?团队踩过 fd 泄漏的坑——chokidar 4+ 丢了 fsevents,Bun fallback 用 kqueue,每监听文件占一个 fd,500+ 文件就 500+ 常驻 fd。
- **回推**:任何变化 → `schedulePush()`,`DEBOUNCE_MS = 2000` 每次变更重置定时器,静默 2 秒才 PUT;正在推时再变更则递归重排,避免并发 PUT。
- 另在 `PostToolUse` hook 显式触发,兜底 fs.watch 可能漏掉的「同一 tick 内的写」。

### 9.5 冲突解决:乐观锁 + 三态重试

`pushTeamMemory` 是精华:

1. 冲突重试期间**只读盘一次**(防边写边同步)。
2. 本地算 `sha256`,与 `serverChecksums` 比较,只有变化的进 delta。
3. 按字节分桶(`MAX_PUT_BODY_BYTES = 200_000`,单文件 `MAX_FILE_SIZE_BYTES = 250_000`),超限拆多个 PUT。
4. HTTP 语义:
   - PUT 带 `If-Match`,200 → 更新本地 checksum。
   - **412(并发写)→ 冲突恢复**:`GET ?view=hashes` 只拉哈希(省 ~300KB 带宽)→ 重灌 `serverChecksums` → 回第 2 步重算 delta(队友若推了相同内容,delta 自然不含该 key)。`MAX_CONFLICT_RETRIES = 2`。
   - **413(条目过多)→** 解析结构化错误拿 `max_entries`,写入 `serverMaxEntries`,下次按此 cap 截断(服务端 cap 按 org 可调,客户端不硬编码)。

**冲突哲学**:push 操作**本地胜**(不吞正在打字的用户的编辑);而双向 `syncTeamMemory` 先 pull 再 push、服务端胜。

**永久失败抑制**:`no_oauth` / `no_repo` / 4xx 会置 `pushSuppressedReason`,后续事件全忽略,直到用户删了一个 team 文件(视为自救)或重启会话。背景:曾有一个 no_oauth 设备 2.5 天堆积 167K 次 push 事件,引入抑制才压住死循环。

### 9.6 多层安全防线(共享 = 高风险)

- **路径穿越/符号链接**:`sanitizePathKey` 拒 null byte / URL 编码 / NFKC 归一化后能变 `../` 的字符 / 反斜杠 / 绝对路径;`realpathDeepestExisting` 识别悬空符号链接与 ELOOP;字符串 + realpath 双重校验防前缀攻击(`team-evil/` ≠ `team/`)。
- **客户端密钥扫描**(gitleaks 30+ 规则:AWS/GCP/Azure/Anthropic/OpenAI/GitHub PAT/Slack/Stripe/PEM):上传前扫描,命中的**整个文件被丢弃**,只记 ruleId(**从不记路径或值**)。写入拦截:`FileWrite/Edit` 的 `validateInput` 里若检测到往 team 目录写密钥,**直接拒绝**。Anthropic 自家 key 前缀 `sk-ant-api` 用 `['sk','ant','api'].join('-')` 运行时拼装,防字面量出现在产物里。
- **prompt 层**:显式禁止敏感数据入 team——纵深防御的第一道。

### 9.7 Team 记忆的 prompt 特殊约束

`buildCombinedMemoryPrompt`(auto + team 同时启用):

1. 两个 scope 并列,显式告知模型 **team 每个会话开始都会同步**(避免误以为它是幂等私有文件)。
2. 四种类型各带 scope 指引(个人偏好 → private,代码约定 → team)。
3. private / team **各有一份 `MEMORY.md`**,两步写入时索引写进「对应目录的」`MEMORY.md`。

---

## 10. Agent 记忆(子代理独立记忆)

通过 AgentTool 启动的每个自定义 sub-agent 拥有**独立于主代理**、按 `agentType` 命名空间隔离的记忆。

### 10.1 三种 scope

```ts
type AgentMemoryScope = 'user' | 'project' | 'local'
```

| scope | 路径 | 入 VCS | 跨项目 |
|---|---|---|---|
| `user` | `<memoryBase>/agent-memory/<agentType>/` | 否(家目录) | **是** |
| `project` | `<cwd>/.claude/agent-memory/<agentType>/` | **是**,git 追踪 | 否 |
| `local` | `<cwd>/.claude/agent-memory-local/<agentType>/` | 否(gitignore) | 否 |

- `agentType` 里的 `:`(plugin 命名空间如 `my-plugin:my-agent`)会被替换成 `-`(Windows 禁用冒号)。
- `local` scope 若设 `CLAUDE_CODE_REMOTE_MEMORY_DIR` 会重定向到远程盘。
- prompt 按 scope 定制:user 版提示「保持通用,跨项目适用」,project 版提示「针对本项目 + 会随 VCS 分享给团队」,local 版提示「针对本项目 + 本机」——**把存储介质的物理特性用自然语言告诉模型**,让它自己判断记忆颗粒度。

### 10.2 注入时机:spawn 时静态注入

agent 定义解析阶段就把 `loadAgentMemoryPrompt(agentType, scope)` 拼到该 agent 的 systemPrompt 尾部——**在 spawn 时静态注入,不是运行时按需查询**。该函数是同步的(被 React 的 `getSystemPrompt()` 回调调用),建目录用 `void ensureMemoryDirExists(...)` fire-and-forget。

### 10.3 主 ↔ 子完全隔离

- **主 → 子**:只通过 systemPrompt 注入,父代理没有直接「喂」子代理记忆的机制。
- **子 → 主**:**没有自动回写**。子代理的记忆是它自己的。
- `isAgentMemoryPath` 对三个 scope 的路径前缀分别校验,供分类计数、活动展示、策略判断。

设计意图:**sub-agent 是独立专家,经验独立累积,不污染主代理的通用记忆。** 一个 `security-reviewer` agent 学到的审查心得只属于它自己。

### 10.4 Agent 记忆快照(Snapshot):用 git 分发专家知识

`agentMemorySnapshot.ts` 提供了一套独立于 team 同步的分发方案。

- **路径**:`<cwd>/.claude/agent-memory-snapshots/<agentType>/`——**在项目内、随 git 检入**。
- **内容**:`snapshot.json`(元数据 `{ updatedAt: ISO8601 }`)+ 若干 `.md` 记忆文件。
- **与 project scope 的区别**:`agent-memory/` 是运行时可写的活文件;`agent-memory-snapshots/` 是**发布物**——维护者提炼成「官方版」提交到 git,新成员克隆即用。

**三态决策** `checkAgentMemorySnapshot()`:

| 场景 | 结果 |
|---|---|
| 无 snapshot 文件 | `none` |
| 本地空(无 `.md`) | `initialize`(静默拷贝,不打扰用户) |
| `snapshot.updatedAt > synced.syncedFrom` | `prompt-update`(挂标记,由 UI 提示用户是否更新) |
| 已同步且未更新 | `none` |

- `.snapshot-synced.json`(存在**本地记忆目录**)记 `{ syncedFrom: <上次同步的 snapshot updatedAt> }`,用于识别新版。
- 接受更新时先 unlink 本地所有 `.md` 再拷贝,防止 snapshot 删掉的条目在本地成孤儿。
- **只有 `memory: 'user'` scope 的 agent 走 snapshot 分发**:project scope 本就随 git 追踪不需二次分发,local scope 是本机独有经验不该被覆盖,唯有 user scope(跨项目共享)需要一个「项目相关的起手包」。

---

## 11. 设计亮点总结

1. **三层解耦(存储 / 系统提示 / 召回)。** 存储由模型自己用文件工具做;系统提示注入 taxonomy + 索引;召回由主进程编排 + Sonnet 侧调。模型只看到两个入口:开局的 `MEMORY.md` 索引 + 每轮可能塞进来的 `<system-reminder>`。scan/挑选/去重/时效/调度全是主进程黑盒,不耗 model turn。

2. **索引常驻 + 内容按需的双层结构。** 粗粒度索引便宜、200 行硬顶不会失控、永远可见;细粒度 topic 文件由 LLM 二次挑选、最多 5 条。让「深柜内容多、随时可见内容少」。

3. **封闭四类型 taxonomy。** 把「什么才是 memory」变成一个封闭集合,从源头排除 code/git/architecture 这些可推导的东西,连「用户显式要求」都不例外。多处标注 eval case number,是迭代打磨的产物。

4. **主/副写入互斥。** 主 agent 就地同步写,提取器异步后台兜底,靠游标 + `hasMemoryWritesSince` 严格切分,任何一轮只有其一动手。

5. **Perfect fork + prompt cache 复用。** 提取器复用主对话前缀,cache 命中 90%+,牺牲一点隔离性(不能用 mini 模型)换成本大幅下降。

6. **处处防字节抖动以保 prompt cache。** memoryHeader 固化在 attachment、KAIROS 日期用模板而非字面量、`resetGetMemoryFilesCache` 区分 correctness vs semantic 失效——一切为了 cache 命中。

7. **注入格式统一。** 从 CLAUDE.md 到召回记忆到任何 meta 提示,一律 `<system-reminder>` + user message(isMeta),让模型侧「识别 meta 内容」是一个统一 handle。真正的系统提示词只放行为教学,不放数据。

8. **静默是默认,冲突才打扰。** Team push 本地胜(不吞用户编辑),snapshot initialize 静默、prompt-update 才通知。

9. **多层安全边界。** `autoMemoryDirectory` 设置故意排除 projectSettings(防恶意 repo 撬 `~/.ssh`);提取子 agent 只能写 `isAutoMemPath` 内;Team 记忆做 path traversal 双校验 + 密钥扫描 + prompt 禁令三重防御 + 失败抑制。

---

## 附录 A. 关键常量速查

| 常量 | 值 | 含义 |
|---|---|---|
| `ENTRYPOINT_NAME` | `'MEMORY.md'` | 索引文件名 |
| `MAX_ENTRYPOINT_LINES` | 200 | 索引行数上限 |
| `MAX_ENTRYPOINT_BYTES` | 25 000 | 索引字节上限 |
| `MAX_MEMORY_FILES` | 200 | 候选池(mtime top-200) |
| `FRONTMATTER_MAX_LINES` | 30 | 扫描时每文件读的行数 |
| `MAX_MEMORY_LINES` | 200 | 注入时每文件行数上限 |
| `MAX_MEMORY_BYTES` | 4 096 | 注入时每文件字节上限 |
| `MAX_SESSION_BYTES` | 61 440(60 KB) | session 累计注入上限 |
| 每次挑选/注入上限 | 5 | 挑选器 slot |
| 挑选调用 `max_tokens` | 256 | Sonnet 侧调 |
| `MEMORY_TYPES` | user/feedback/project/reference | 四类闭集 |
| `MAX_INCLUDE_DEPTH` | 5 | `@import` 最大深度 |
| `DEBOUNCE_MS`(team) | 2000 | team push 防抖 |
| `MAX_PUT_BODY_BYTES`(team) | 200 000 | 单次 PUT body 上限 |
| `MAX_FILE_SIZE_BYTES`(team) | 250 000 | team 单文件上限 |
| `MAX_CONFLICT_RETRIES`(team) | 2 | 412 冲突重试次数 |

## 附录 B. 关键源文件索引

| 文件 | 职责 |
|---|---|
| `src/memdir/memoryTypes.ts` | 四类型 taxonomy、prompt sections、frontmatter 样板 |
| `src/memdir/memdir.ts` | prompt 构建、MEMORY.md 读入、目录 ensure、截断 |
| `src/memdir/paths.ts` | 目录解析、安全校验、memoize |
| `src/memdir/memoryScan.ts` | 扫描目录 + manifest 格式化 |
| `src/memdir/findRelevantMemories.ts` | Sonnet 侧调挑选器 |
| `src/memdir/memoryAge.ts` | 时效表述 |
| `src/memdir/teamMemPaths.ts` | Team 目录、路径穿越/符号链接校验 |
| `src/memdir/teamMemPrompts.ts` | auto + team 合并 prompt |
| `src/services/extractMemories/extractMemories.ts` | 后台自动提取(perfect fork) |
| `src/services/extractMemories/prompts.ts` | 提取指令 prompt |
| `src/services/teamMemorySync/index.ts` | REST 同步、delta、ETag、密钥过滤 |
| `src/services/teamMemorySync/watcher.ts` | fs.watch + 防抖 + 失败抑制 |
| `src/services/teamMemorySync/secretScanner.ts` | gitleaks 规则集 |
| `src/tools/AgentTool/agentMemory.ts` | 三 scope 目录 + prompt 注入 |
| `src/tools/AgentTool/agentMemorySnapshot.ts` | 快照三态决策 + 拷贝/替换 |
| `src/utils/claudemd.ts` | CLAUDE.md 五级层级 + @import 加载器 |
| `src/utils/attachments.ts` | prefetch 调度、注入截断、去重 |
| `src/utils/messages.ts` | attachment 渲染成 `<system-reminder>` |
| `src/utils/context.ts` | `getUserContext`,拼首条 user message |
| `src/query/stopHooks.ts` | 触发 extractMemories 的 stop-hook |








