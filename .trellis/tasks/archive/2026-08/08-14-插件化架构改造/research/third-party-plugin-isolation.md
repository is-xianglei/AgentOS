# 研究：第三方插件的现实隔离边界与方案分级

- **Query**: 多租户 FastAPI SaaS 允许租户上传/安装第三方代码插件，Python 下现实的隔离边界与分级方案
- **Scope**: mixed（dsh 对照源码 + AgentOS 现状 + 安全机制事实核查）
- **Date**: 2026-08-15

## 结论先行（硬结论）

1. **dsh 的插件是全信任代码，它从未回答过「不可信插件」问题**。`dsh plugin add` 是 pnpm
   转发器，装完即同进程 Node 代码，零隔离；它甚至指导用户放行 git 依赖的安装期构建脚本。
   dsh 全部沙箱设施（sandbox / e2b / code-runtime）针对的都是**模型产物**（模型生成的命令、
   文件效果、模型写的程序），从不针对插件本体。R-T-4 超出 dsh 的威胁模型，抄不到作业。
2. **进程内加载不可信 Python 代码无法安全隔离**（Q1）。CPython 官方口径：解释器不是沙箱，
   安全边界是 OS 进程。RestrictedPython / audit hooks / 进程内 seccomp / subinterpreters
   全部不构成安全边界（逐条论证见第三节）。内核的 isolate/realm 标签只是组合可见性边界。
3. **PRD 验收 A10（恶意插件摸不到其它租户数据与宿主服务槽）只有进程外才能满足**。
   任何「同进程跑租户代码」的方案与 A10 直接互斥，必须选边。
4. **进程外插件能参与「一切皆插件」组合，但只能经宿主侧受信 stub 代理、按协议粒度参与**（Q3）：
   工具粒度成熟（MCP，依赖已在 pyproject.toml:23 但全仓零 import）；粗粒度请求-响应型服务
   可代理；通知型事件可桥接；**waterfall 否决权与持有活对象的服务槽不应也无法开放给租户**。
5. 推荐四级：T0 平台内建（同进程全能力）→ T1 运维装配的可信第三方（同进程全能力，
   安装权在平台不在租户）→ T2 租户自装进程外协议插件（MCP + OS 沙箱 + 限额，
   能力=工具/提示词/资源）→ T3 租户一次性脚本（现有 skill 通道收编，必须补 OS 沙箱）。

## 一、dsh 对照：安全边界画在「模型产物」上，插件本体不设防

### 1.1 `dsh plugin add` 的信任假设（apps/cli/src/plugin.ts）

- :1-11 模块注释自述 thin pnpm forwarder；:129-133 `spawnSync('pnpm', args)` 在 profile
  目录直接执行，可安装 npm 包名 / git / tarball / 本地目录。
- 装后对账 `reconcilePlugins`（:59-91）只看包是否声明 `dsh.bundle`（:36-45），声明即入
  层栈——**没有任何签名、来源或权限审查**。
- :148-155 当 pnpm>=10 拦截 git 依赖的 prepare 构建脚本时，dsh 的处理是**教用户去
  pnpm-workspace.yaml 的 allowBuilds 放行**——安装期执行任意代码被视为正常流程。
- 装好的 bundle 由 Cordis Loader 挂进**同一 Node 进程**（见本目录
  plugin-loading-and-composition.md §一、§五），patch 条目还允许 `!!js` 表达式，配置即代码。

信任假设与 `npm install` 同级：执行安装的人（单机单用户工具的操作者本人）为代码负全责。

### 1.2 dsh 的沙箱设施：对象全部是「模型产物」

| 设施 | 沙箱对象 | 证据（file:line） |
|---|---|---|
| `ctx.sandbox` | 工具即将 spawn 的 argv（模型命令） | `packages/sandbox/sandbox/src/index.ts:1-6`「wrap exact subprocess argv under a host-path file policy」；:163-175 confine 入参即 caller 将要 spawn 的 argv |
| sandbox-local 后端 | 同上；Linux bwrap→Landlock、macOS Seatbelt、Windows ACL 受限令牌，不可用则 fail-closed 拒跑 | `packages/sandbox/sandbox-local/src/index.ts:2-6`；Windows 档自报 partial（:8-19：WRITE_RESTRICTED 必须保留 Everyone、NTFS 硬链接跨路径别名） |
| 消费方 | 全是模型面工具 | grep `ctx.sandbox` 命中：tool-bash / tool-pwsh / terminal-bash / fs-sandbox / tool-fs / tool-str-replace-editor；`packages/shell/bash-sandbox/src/index.ts:1-9`「wraps the exact local bash argv through ctx.sandbox」 |
| sandbox-policy | read-only / workspace-write / danger-full-access 三档**文件效果策略**，按会话解析并写入系统提示词（模型可见） | `packages/sandbox/sandbox-policy/src/index.ts:38-52,135-142` |
| e2b | 把 fs + subprocess 两个 seam 整体替换为同一远程 Linux 世界——搬走的是**模型操作的效果世界**，插件仍在宿主进程 | `packages/e2b/e2b/src/index.ts:1-5` |
| code-runtime | 「runs one model-written program」，实现须「treat programs as hostile peers」——敌意方是模型写的程序，跑在 worker-thread 后端 | `packages/code-runtime/code-runtime/src/index.ts:1-5,95-100` |

威胁模型旁证：`docs/defensive-patterns.zh.md:27-29`「绝不将环境变量或可预测路径暴露给
**不可信输出**」——被清洗 env（`*KEY*/*SECRET*/*TOKEN*/*PASSWORD*`）的是启动的命令，
不可信方是命令与其输出，从来不是插件。

### 1.3 明确结论

dsh 插件 = **可信代码**，与内核同权：同进程、可注册任意服务、挂任意监听、读全部凭证。
其安全边界画法：

    [受信区]   Node 进程 = 内核 + 全部插件 + 凭证
        │  ctx.sandbox.confine(argv) / e2b 整体换世界 / code-runtime worker
        ▼
    [不受信区] 模型产物 = bash 命令、文件效果、模型写的程序

进程外接入面只有三种且全是协议粒度：MCP 工具（`mcp__<server>__<tool>`）、ACP、
Python SDK 的 stdio JSON-RPC 编排客户端（plugin-loading-and-composition.md §七）。

## 二、AgentOS 现有执行/隔离设施盘点

### 2.1 tools/shell_policy.py —— 静态命令白名单，自述非 OS 沙箱

- 文件头（:1-15）：「这是静态命令校验,不是 OS 级沙箱」；三档 FULL / READ_ONLY /
  TMP_WRITABLE（:26-31），FULL 直接放行（:326-327）。
- 手段：只读白名单（:52-72）、验证类运行器（:76-83）、永久拒绝表（:112-121，
  sudo/curl/pip/docker…）、拒绝重定向/heredoc/命令替换（:102-108,335-348）、
  git/sed/awk/find/包管理器专项校验（:171-229）。
- 自认残余风险（:13-14）：TMP_WRITABLE 放行的测试运行器仍能在进程内写项目目录。
- 消费方 `tools/builtin/bash.py:49-62`：约束的是**模型**在受限档位的越权，对进程零约束。

### 2.2 core/executor.py —— 裸子进程执行器（已预留沙箱切换点）

`SubprocessExecutor`（:38-103）已有：argv 数组绝不过 shell（:57-64）、
`start_new_session=True` + 超时 killpg SIGKILL 杀整组（:64,72-90）、输出截断（:92-95）、
env 完全由调用方传入不继承（:44）。**没有**：文件系统确界、网络限制、CPU/内存限额、降权。
`get_executor`（:110-118）注释明言「将来接入容器 / nsjail 时新增 SandboxExecutor,
在此按配置切换,上层不改」——这就是补 OS 沙箱的切入点。

### 2.3 skill 域 —— 现存唯一「租户上传代码并执行」通道（skill/service.py）

1. 上传：zip → `_parse_bundle`（:63-114），zip-slip 防护拒绝绝对路径与 `..` 段
   （:148-161）→ 整包 sha256 判重（:163-166,182-187）→ 逐文件写 MinIO（:192-201）→ 落库。
   权限：workspace owner/admin（:175-179）。
2. 执行 `run_script`（:307-374）：全局开关（:316-320）→ 路径必须在 scripts/ 下（:321-324）
   → 扩展名白名单 SKILL_EXEC_INTERPRETERS 仅 .py/.sh/.js（core/config.py:148-152）→
   每次执行 mkdtemp 物化整个 bundle（:333-353）→ env 白名单仅 PATH/LANG，注释明令
   「严禁下发 ANTHROPIC_API_KEY/DATABASE_URL/MINIO_*」（:357-362）→ SubprocessExecutor
   限时执行（:365-372）→ finally 删目录（:373-374）。模型入口 tools/builtin/skill.py:157。
3. 隔离现状：**有**超时、进程组回收、输出截断、凭证不下发、一次性目录；**无**任何 OS
   边界——脚本继承宿主 UID 与完整网络：可读宿主任意可读文件（含项目源码与 .env）、
   可任意出网、可探测 127.0.0.1 上的 PostgreSQL/Redis 端口，且无 CPU/内存限额。

### 2.4 已声明未使用的依赖

`pyproject.toml:23` 声明 `mcp[cli]>=1.28.1`（官方 MCP Python SDK，含 client、FastMCP
server、stdio 与 streamable-HTTP 传输）；全仓 grep `import mcp` / `from mcp` **零命中**。
PRD 差距表第 10 条（prd.md:125）与此一致。T2 方案的协议底座已在依赖树里待用。

## 三、Q1：进程内加载不可信 Python 代码，能否安全隔离？——不能

1. **CPython 官方安全口径**：解释器不是沙箱，「能执行 Python 代码 = 能执行任意代码」，
   安全边界是 OS 进程；沙箱逃逸类报告不被当作 CPython 漏洞受理（python/cpython
   SECURITY.md 与安全响应团队的一贯处理原则）。
2. **pysandbox 路线已被作者亲手宣判**：CPython 核心开发者 Victor Stinner 2013 年在
   python-dev 公开结论「pysandbox is broken (by design)」——frame、code object、gc、
   类型 MRO 等内省面在 CPython 内部封不完。此后无认真复活者。
3. **RestrictedPython 是纸糊的**：Zope 系编译期受限方言，项目文档自述
   「RestrictedPython is not a sandbox system or a secured environment」。公开逃逸：
   CVE-2023-37271（经生成器/协程的 gi_frame/cr_frame 摸到调用栈帧 → 全局命名空间 →
   任意执行）、CVE-2023-41039（经 str.format_map / string.Formatter 逃逸）。
4. **PEP 578 audit hooks 是审计不是防护**：目标是事后检测/取证，不试图限制能力。
   hook 与不可信代码同进程：不能限内存/CPU，C 扩展内部调用不产生对应事件。可用作
   纵深**监控**（记录 import/exec/socket 事件流水），不可用作边界。
5. **seccomp 用在进程内等于自杀**：seccomp-bpf 按进程/线程生效，无法细分到「进程内某
   段插件代码」；给 FastAPI 主进程装 deny 网络/写盘的过滤器会先杀死服务本身。seccomp
   的正确位置是**子进程**（nsjail、gVisor 皆如此用）。
6. **subinterpreters（PEP 734，Python 3.14 concurrent.interpreters）不是安全边界**：
   PEP 未把安全隔离列为目标；共享进程地址空间，C 扩展与 ctypes 直接穿透。
7. **具体到 AgentOS 的攻击面**：任意同进程代码一行
   `from core.config import settings; settings.jwt_secret_key`（core/config.py:142-143
   模块级单例，:115 秘钥字段）即获得给任意租户签发 JWT 的能力；退一步也能
   `gc.get_objects()` 扫堆、`ctypes` 读内存、绕过 Repository 直接拿 engine 发 SQL——
   租户数据隔离全在应用层（workspace_id 过滤），同进程即全穿。

推论：改造后内核的 isolate/realm 标签（R-K-1/R-T-1）解决的是**组合可见性与冲突**，
不解决**安全**。「同进程 + 任何软件手段」不改变信任等级。

## 四、Q2：现实可行的隔离边界分档

| 档 | 边界 | 能挡住 | 挡不住 | 插件能力上限 |
|---|---|---|---|---|
| ① 同进程加载 | 无，信任即边界 | 什么都挡不住 | — | 全能力：直连 DB、注册 ctx 服务、挂含 waterfall 的全部事件、被 inject、自带 ORM 表 |
| ② 子进程 + OS 沙箱 | 内核强制的 fs/net/信号面 | 越权读写宿主文件、出网（需 Landlock ABI4+ 或 bwrap unshare-net）、干扰宿主进程 | 内核 0-day、/proc 信息面与时序侧信道、Windows 档 partial | 可常驻可一次性；与内核只能经 RPC 对话；DB 只能走平台代理 API |
| ③ 容器 / microVM | 用户态内核（gVisor）或独立客体内核（Firecracker/Kata） | ② 之外再挡掉大部分宿主内核攻击面；cgroup v2 原生限额 | 逃逸类 0-day（概率量级更低）、共享宿主的旁路 | 同 ②，且可自带任意依赖与运行时 |
| ④ 远程沙箱（e2b 类） | 物理/账号级，代码不在你的基础设施上 | 一切宿主侧风险 | 数据出境（隐私/时延/成本）、供应商依赖 | 同 ②，时延最高 |
| ⑤ 进程外协议插件（MCP stdio/HTTP） | **接口档**：定义「能对话什么」，必须叠加 ②/③/④ 之一作执行底座 | 能力面收敛为协议词汇（tools/prompts/resources） | 本身不提供 OS 隔离——stdio 子进程不套 ② 就仍是裸进程 | 见第五节逐项 |

各档要点：

- **②（dsh 同款梯子，可直接抄）**：Linux 首选 bubblewrap（用户命名空间挂载白名单；注意
  部分发行版默认限制非特权 userns，部署期需确认）→ 回退 Landlock（无特权 LSM，内核
  5.13+；ABI4/内核 6.7+ 可限 TCP bind/connect）→ 叠 seccomp 缩系统调用面。dsh 的
  「功能探测 → 回退链 → 不可用即 fail-closed 拒跑」可照搬
  （sandbox-local/src/index.ts:2-6,67-90）。macOS 用 Seatbelt `sandbox-exec -p`：Apple
  标记 deprecated 但每版都在（dsh 原话 :76-84），仅当开发机档；生产 SaaS 应落在 Linux。
  Windows 受限令牌 + ACL 被 dsh 自标 partial（:8-19），不作多租户依据。
- **③**：多租户 SaaS 的生产推荐档。gVisor 在用户态拦 syscall；Firecracker/Kata 每租户
  独立客体内核，冷启动百毫秒级；成本是镜像构建与池化运维。
- **④**：dsh 的启发不是「把插件搬进 e2b」，而是「**沙箱世界，而非沙箱插件**」——fs 与
  subprocess 两个 seam 一起指向同一远程世界（e2b/src/index.ts:1-5），受信插件留在宿主。
  AgentOS 对等物：Executor/沙箱 provider 整体可替换（core/executor.py:110-118 预留位）。
- **⑤ 与 ②③④ 正交**：⑤ 决定「说什么话」，②③④ 决定「身体关在哪」。生产形态：租户插件
  = MCP server，跑在 ② 或 ③ 里，宿主经 stdio（同机）或 streamable HTTP（远端/自托管）连接。
- **支线 WASM（wasmtime）**：内存安全的进程内边界，但 Python 生态约束大（native wheel、
  socket、fs 都要 WASI 兜底），对「租户任意 Python 插件」当下不现实，不作主路径。

## 五、Q3：进程外插件还能参与「一切皆插件」的组合吗？

**机制事实**：ctx 服务槽、waterfall 监听、inject 的本质都是内核地址空间内的 Python 调用
（prd.md:136-153 R-K-1..5）。进程外代码不可能直接持有 ctx 对象，也不可能把回调塞进内核
事件表。**唯一通路：平台自带一个受信的通用代理插件（stub）在内核侧代表远端插件**——
stub 声明 inject、注册服务、挂监听，把每次调用翻译成一次 RPC。远端进程存活由 stub 监督
（心跳/重启），映射为服务的 provide/撤销，epoch 依赖驱动照常工作。

在 stub 形态下各能力的可达度：

| 组合能力 | 可达度 | 说明 |
|---|---|---|
| 提供工具 | **完整**（成熟标准） | MCP 即为此设计：stub=MCP client，把远端 tools 注册进工具 seam；dsh 同款（`dsh-mcp-client` → `mcp__<server>__<tool>`）。MCP prompts/resources 可映射为提示词片段/技能资源 |
| 提供 ctx 服务 | **部分**：仅「粗粒度、请求-响应、参数结果可序列化、容忍毫秒级时延」的服务可代理 | 传值服务（翻译、检索、评分器）可以；持活对象的服务（AsyncSession、StreamBus、返回进程句柄的 subprocess seam）**不可能跨进程** |
| 挂事件监听 | **通知型（emit/parallel）可桥**：stub 订阅后异步转发，远端慢/挂不拖累循环。**waterfall/serial/bail 技术上可桥但不应对租户开放** | 每个监听点 = 热路径上一次同步 RPC 往返；且 waterfall 即否决与改写权（挂在 `agent/request` 上能看见并篡改全部提示词与上下文）——给租户即交出会话主权与数据。确需时只开白名单事件 + 严格超时 + 显式 fail-open/closed 语义 |
| 被 inject / 依赖驱动 | **对 stub 完整** | stub 是正常一等插件，装载/卸载/换代全走内核机制 |
| 自带 ORM 表 / Alembic | **不可达（政策性）** | 不给租户代码 DB 直连；持久化用平台提供的租户作用域 KV/文档 API（另议） |

dsh 佐证：三个进程外接入面（MCP 工具、ACP、Python SDK stdio JSON-RPC）全部止步协议
粒度，服务槽与事件表从未跨进程（plugin-loading-and-composition.md §七）。dsh 的 UI 平面
看似「跨进程组合」，实际是在浏览器再起一个**受信的**完整 Cordis 运行时——租户代码
不受信，此路对本题不成立。

**给本次改造的关键约束表述**：「一切皆插件」应精确化为「**一切皆插件，插件分公民等级**」
——组合机制（条目/patch/epoch/依赖驱动）对所有等级统一；**能力词汇按等级裁剪**。
试图给进程外租户插件完整 ctx 权能只有两条路：放进程内（违背 A10，prd.md:263-265），
或把内核 API 整体 RPC 化（时延与失败语义摧毁热路径）。两条都不可取。

## 六、Q4：多租户附加要求的可行做法

1. **资源限额**：
   - 时间：已有（executor 超时 + killpg 整组回收，core/executor.py:72-90）。
   - CPU/内存：生产档用 cgroup v2（容器运行时原生；裸机可 `systemd-run --scope
     -p MemoryMax= -p CPUQuota=` 包裹 argv）；轻量档在子进程内自限 rlimit
     （RLIMIT_CPU/AS/NOFILE/NPROC，经 wrapper 脚本或 preexec；macOS 无 cgroup 仅此档）。
   - 输出/磁盘：输出截断已有（core/executor.py:92-95）；磁盘用沙箱 tmpfs 或容器存储配额。
   - 常驻型（MCP server）：每租户实例数/并发上限 + 空闲回收，防囤进程。
2. **租户间隔离**：
   - 目录：一次性 mkdtemp 已有（skill/service.py:333）；沙箱档再把 fs 白名单确界到该目录。
   - 网络：默认全禁出网（bwrap unshare-net / Landlock ABI4 / 容器网络策略）；按插件声明
     经**平台正向代理**放行白名单域名，代理侧记账审计。现状 skill 脚本网络全敞开是最大缺口。
   - 数据：DB/Redis/MinIO 凭证绝不下发（skill/service.py:357-362 已是纪律，保持）；插件
     读写业务数据只能携带**租户作用域短时 token** 回调平台 API，由平台 Service 层执行既有
     权限规则——即「跨域只许 Service 调 Service」在进程边界上的推广。
   - 身份：沙箱档可加 per-租户 UID/子 UID 映射作纵深。
3. **来源校验**：
   - 完整性：整包 sha256 已有（skill/service.py:163-166）；扩展为安装记录 hash + 版本 pin
     （学 dsh 的 `github:owner/repo#sha` pin 语义，plugin-loading-and-composition.md §五）。
   - 真实性：市场渠道用签名（sigstore/minisign）+ 审核流水线（静态扫描、依赖 SBOM）；
     租户私有插件至少 hash 固定 + 管理员确认。
   - **禁止安装期执行代码**：与 dsh 必须相反的一条——dsh 教用户放行 pnpm prepare 构建
     脚本（plugin.ts:148-155，因其信任插件）；AgentOS 对租户上传物只解包、只静态校验，
     任何构建/安装钩子一律不执行；依赖装配在沙箱内完成，或要求上传自包含产物。

## 七、推荐分级方案

| 等级 | 谁能装 | 跑在哪 | 能做什么 | 不能做什么 |
|---|---|---|---|---|
| **T0 平台内建插件** | 随代码发布 | 同进程 | 全能力：ctx 服务、五种分发的事件、inject、自带 ORM 表与迁移 | —（受工程纪律与评审约束） |
| **T1 可信第三方插件** | **仅运维/平台管理员**在部署期经组合文件装配 | 同进程 | 同 T0 | 绝不进租户自助安装流程；必须审核 + 签名 + 版本 pin |
| **T2 租户协议插件**（R-T-4 的正主） | 租户 workspace owner/admin | 进程外 MCP server：底座 = ② 子进程+OS 沙箱起步、③ 容器为生产目标；或租户自托管经 streamable HTTP | 提供工具（命名空间化注册）、提示词片段、资源；带租户 token 回调平台 API 读写本租户数据；被 stub 代理后参与依赖驱动装载；订阅白名单**通知型**事件 | 不得注册任意 ctx 服务；不得挂 waterfall/serial/bail 否决点；无 DB/Redis/MinIO 直连；无宿主 fs；默认无出网；受 CPU/内存/时间/并发限额 |
| **T3 租户一次性脚本**（现有 skill 通道收编） | 租户 workspace owner/admin | 进程外一次性子进程，**必须补 OS 沙箱底座**（现状裸跑） | 短生命周期计算：读自身物化 bundle，argv/stdin 进、stdout 出 | 无常驻、无组合参与、无网络、无平台 API 回调（需要回调就升 T2） |

可行性顺序（非计划）：T3 补沙箱（get_executor 预留位换 SandboxExecutor）与 T2 的 MCP
client stub（mcp 依赖已在树上）互不阻塞；T2 的「服务代理/事件桥」属第五节受限扩展，
可后置。T1 与 T2 的本质区别不是技术是**安装权**：谁按下安装键，谁就进入了信任边界。

## 引用文件

- dsh（根：/Users/xianglei/Desktop/deepseek-harness）：`apps/cli/src/plugin.ts`、
  `packages/sandbox/sandbox/src/index.ts`、`packages/sandbox/sandbox-local/src/index.ts`、
  `packages/sandbox/sandbox-policy/src/index.ts`、`packages/shell/bash-sandbox/src/index.ts`、
  `packages/e2b/e2b/src/index.ts`、`packages/code-runtime/code-runtime/src/index.ts`、
  `docs/defensive-patterns.zh.md`
- AgentOS：`tools/shell_policy.py`、`core/executor.py`、`core/config.py`、
  `skill/service.py`、`tools/builtin/bash.py`、`tools/builtin/skill.py`、`pyproject.toml`
- 本任务既有材料：`research/plugin-loading-and-composition.md`（§五、§七）、
  `research/agentos-baseline.md`、`prd.md`（R-T-4:201-203、A10:263-265、R-K:136-153）

## Caveats / Not Found

- dsh 仓库内**未找到**任何针对「插件本体」的隔离/审查代码或文档（检索
  untrusted / trust boundary / sandbox 关键词，命中全部指向模型产物侧）——
  这本身就是第一节结论的证据：该问题在 dsh 的威胁模型中不存在。
- RestrictedPython 逃逸以 2023 年两例 CVE 为据；未逐一复核 2026 年内新披露，
  但不存在任何「已修复到可跑不可信代码」的版本主张，结论不受影响。
- 未实测 dsh 运行时行为，全部结论基于源码与既有 research 静态阅读；未在本机执行任何
  租户脚本或沙箱探测命令。
- e2b/gVisor/Firecracker 的时延与运维成本未做基准测试，档位对比为定性结论。
- 前端/浏览器插件平面不在本文范围（PRD 第五节已排除）。
