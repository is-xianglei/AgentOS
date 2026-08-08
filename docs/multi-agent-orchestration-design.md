# AgentOS 多智能体编排设计方案

本文基于三份一手材料:本地 Spring AI `2.0.1-SNAPSHOT` 源码通读、Google ADK 与 Spring AI
Alibaba 的实现调查、AgentOS 当前代码审计。目标是给出 Sequential / Parallel / Routing / Loop
四种编排能力的抽象设计,不含代码改动。

## 1. 三个必须先纠正的前提

**Spring AI 官方核心库没有 Agent 抽象。** `2.0.1-SNAPSHOT` 全仓库 grep `SequentialAgent`
零命中,14 个顶层模块无任何 `*agent*` 目录。官方只有一篇《Building Effective Agents》博客,
给的是纯示例代码(`ChainWorkflow` 之类),不是框架 API。

**那四个类名来自 Google ADK。** `google.adk.agents.{SequentialAgent, ParallelAgent, LoopAgent}`
是原始出处,配套 `output_key` + `session.state` 数据流。Java 侧的同名实现在 Spring AI Alibaba
(`com.alibaba.cloud.ai.graph.agent.flow.agent`),底层是自研 graph,官方自述"Java 版 LangGraph",
上层 Flow Agent 的 API 形态与 ADK 同构。它借的是 spring-ai 的 `ChatClient` / `ToolCallback`,
不是 spring-ai 的 agent 能力——因为后者不存在。

**ADK 2.6 已把这三个模板类标记 `@deprecated`,转向 graph-based `Workflow`。** 这是最重要的信号:
你想借鉴的是 ADK 1.x 的设计快照,而它的作者正在撤。原因可推:固定形状的容器类表达力不足,
一旦出现"并行分支里第二条要提前退出并把结果喂给第三条"这类需求,Sequential/Parallel/Loop
的组合就开始需要打洞。

**由此决定的选型:采纳 ADK 的统一 Agent 契约,采纳 LangGraph/Alibaba 的 per-key reducer 状态模型,
但不做图引擎,也不把四种模式做成四个终态类。** 落点是**可嵌套的编排组合子**——四种模式是
同一个 `Agent` 接口的四种实现,彼此可任意嵌套,共享一套状态与事件语义。这比硬编码强,
比引入完整 StateGraph 轻,且不阻断日后长出图引擎(组合子是图的一个受限子集)。

## 2. Spring AI 四大抽象拆解

### 2.1 ChatModel:能力探测优于能力枚举

分层是 `Model<TReq, TRes>` → `ChatModel extends Model<Prompt, ChatResponse>, StreamingChatModel`。
真正值得抄的不是这个继承链,而是三点:

**Prompt 与 ChatOptions 分离。** `Prompt` 表达"说什么"(消息序列),`ChatOptions` 表达"怎么调"
(temperature/maxTokens/model)。分离后同一段消息可挂不同调用参数,这是编排里"同一 prompt
用不同模型档位跑多路投票"的前提。AgentOS 现在 `LLMClient.stream(messages, system_prompt,
tools, model)` 是散参数,编排层需要"把一次调用的全部参数当成一个可传递、可覆盖的值对象"。

**能力用子接口探测,不用大而全接口枚举。** `ToolCallingChatOptions` / `StructuredOutputChatOptions`
是 `ChatOptions` 的子接口,框架用 `instanceof` 探测,不支持的模型自然跳过。Python 对应物是
`Protocol` + `isinstance`,或直接可选字段。反面教材是把所有 provider 特性堆进一个基类。

**options 三级合并规则单一。** 模型默认 → 客户端默认 → 单次调用,规则是"非空即覆盖",
唯一例外 `stopSequences` 做列表拼接。2.0 把 1.x 的 Jackson 反射式 merge 整个删掉了,
`ModelOptionsUtils` 现在全文 42 行只剩一个 `mergeOption`。这个"用类型化 Builder 取代反射合并"
的收缩方向值得记住:AgentOS 用 Pydantic `model_copy(update=...)` + `exclude_unset` 即可,
不需要 Java 那套自引用泛型 Builder。

### 2.2 Advisor:本方案最核心的借鉴对象

`Advisor extends Ordered` 分裂为 `CallAdvisor.adviseCall(request, chain)` 与
`StreamAdvisor.adviseStream(request, chain)`,链是 `DefaultAroundAdvisorChain`。四个设计点:

**模型调用本身就是链上最后一个节点。** `ChatModelCallAdvisor` / `ChatModelStreamAdvisor`
以 `LOWEST_PRECEDENCE` 注册在链尾,链里没有"链尾特判"。直接收益:测试时替换链尾即得 fake
model,不必 mock SDK。AgentOS 当前 `_run_llm_loop` 里 LLM 调用是硬编码的 `self.llm.stream(...)`,
这是零测试状态下最难验证的一段。

**不可变载荷 + 可变共享 context。** `ChatClientRequest(prompt, context)` 与
`ChatClientResponse(chatResponse, context)` 都是 record,`context` 是贯穿整链的可变 map,
约定键名通信(`ChatMemory.CONVERSATION_ID`、`ChatClientAttributes.OUTPUT_FORMAT`)。
AgentOS 的 `ToolContext` 已是同源思路,应把它从"工具入参"提升为"贯穿整条编排链的运行上下文"。

**`chain.copy(after)` 让循环成为链上可重入的一环。** 这是 2.0 最大的架构变化:工具调用循环
从各 ChatModel 内部**整体迁出**,落到 `ToolCallingAdvisor`——一个 `do { ... } while (isToolCall)`
显式循环,每轮通过 `callAdvisorChain.copy(this).nextCall(...)` 重放"自己下游的链段"。
后果是循环内每一轮都能被日志、记忆、安全 advisor 覆盖,而不是循环整体被包在外面看不见内部。
`internalToolExecutionEnabled` 标志因此被彻底删除。

这一点对 AgentOS 是直接的架构升级方向:Agent Loop 不应写死在 runtime 里,应是"可重入的链段重放"。

**循环内外的边界由排序表达。** `ToolCallingAdvisor` order 是 `HIGHEST_PRECEDENCE + 300`,
memory advisor 是 `+200`,更靠外,所以记忆默认只记完整回合;把 memory order 调进 300 以内
它就参与每轮迭代。这是设计精髓,但**实现是包袱**:靠魔数决定语义过于隐晦。Python 里应显式
声明链结构(列表顺序即执行顺序),不要抄 `Ordered` 常量。

同样不该抄的:call/stream 双接口双实现导致每个 advisor 写两遍逻辑;为阻塞 `before` 让路的
`publishOn(boundedElastic)`;Reactor 的 span 父子修正与 `contextWrite`。AgentOS 用
async generator 统一表达单条与流式(单条即长度 1 的流),这些复杂度天然不存在。

### 2.3 Tool:三段式 ToolContext 与外置的执行循环

`ToolCallback.call(String toolInput, ToolContext ctx) -> String` 是阻塞的字符串进字符串出。
值得借鉴的是**分层**和**ToolContext 的三段式**:

`ToolContext` 解决"工具需要业务上下文,但这些上下文绝不能暴露给模型"。三处保证:
`JsonSchemaGenerator` 生成 schema 时显式跳过 `ToolContext` 类型参数;执行期由
`DefaultToolCallingManager.buildToolContext` 注入;跨进程时经 MCP 请求的 `_meta` 透传。
AgentOS 的 `ToolContext`(含 `db` / `bus` / `session_id` / `allowed_tool_names`)已是这个思路,
且比 Spring AI 更完整(带了 AsyncSession 和事件总线)。

另外两个值得抄的机制:

**`ToolCallLimits` 预算闸门。** 每工具 40 次、单轮总计 150 次上限,超限按 `THROW` 或
`RETURN_ERROR_RESPONSE`(把限额说明当作 tool result 回灌给模型)两种策略。AgentOS 现在只有
`max_tool_iterations=50` 一个轮次上限,没有"单个工具被反复调用"的防护。编排引入并行后
成本失控风险放大,这个双层预算应当补上。

**`ToolSearchTool` 动态工具发现。** 它本身是一个 `@Tool`,配 Lucene/regex/VectorStore 三种索引,
`ToolSearchToolCallingAdvisor` 按轮把检索命中的工具注入下一次请求。大工具集场景的成熟解法。

明确不足、AgentOS 不应重复的:同批 tool calls 是 `for` 循环**串行**执行(无 `gather`);
无超时抽象;工具结果强制 String(图片被 base64 塞进 JSON);**完全没有权限/审批机制**
(grep `approv|human.in.the.loop|permission` 零命中)。AgentOS 的 `permission/` 域在这点上
领先 Spring AI 一个层级,不要在编排设计中丢掉它。

### 2.4 Memory 与 Session:这是反面教材

**Spring AI 没有 Session 概念。** 会话的全部身份就是消息表里一个 `conversation_id VARCHAR(36)`
裸字符串,无所有者、无元数据、无状态、无租户列。`ChatMemory` 只有 `add/get/clear`。

`MessageWindowChatMemory` 有两个细节值得抄:SystemMessage 无条件保留、超限只砍非 system 消息;
2.0 新增的**轮次对齐**——`cutIndex` 会继续前推直到窗口首条非 system 消息是 `USER`,避免留下
没有对应用户消息的孤儿 assistant/tool 消息。AgentOS 的 `CompactService` 做类似裁剪时应确认
有同等保护。另一个值得抄的是 `isMemoryAlreadyInPrompt()` 子序列匹配幂等检查,防止工具回环时
重复注入历史。

但整体是反面教材:窗口按**条数**不按 token(20 条长消息可能远超上下文);`saveAll` 语义是
**全量替换**,每次 add 都"读全量→内存裁剪→全量删插",写放大 O(n) 且 read-modify-write 无锁无版本,
并发丢更新;`JdbcChatMemoryRepository` **显式过滤丢弃** `ToolResponseMessage` 与带 toolCalls 的
AssistantMessage(只 warn 一句),`MessageRowMapper` 对 `case TOOL -> null`——换个 Repository
实现会改变记忆语义(Neo4j/Redis 完整,JDBC/Mongo 残缺);无软删除、表无主键;**没有任何
记忆提取/压缩/摘要能力**(全仓库搜 summariz/compress 只有文档 ETL 增强和 RAG 查询改写两处,
都不是记忆)。

AgentOS 已有 6 张记忆表、按 Turn 冻结的 `MemoryRecallService.get_or_create_context`、
`memory/jobs` 的提取/dream/retention 队列、`SessionSnapshot` + micro/auto compact。
**在 Memory 与 Session 上,AgentOS 全面领先 Spring AI,不存在借鉴空间,只需吸收两条裁剪细节。**

### 2.5 一句话总结借鉴清单

| 抽象 | 采纳 | 拒绝 |
| --- | --- | --- |
| ChatModel | Prompt/Options 分离、能力子接口探测、单一合并规则 | 同步接口继承流式接口、泛型骨架 |
| ChatClient | 不可变载荷 + 可变 context、原型拷贝式默认值 | fluent Builder 自引用泛型 |
| Advisor | **链尾即模型调用、`chain.copy` 循环重入、before/after 简化契约** | Ordered 魔数排序、call/stream 双实现、Reactor |
| Tool | ToolContext 三段式、双层调用预算、动态工具发现 | String 进出、串行执行、无审批 |
| Memory | 窗口裁剪对齐 USER 轮次、注入幂等检查 | 裸 conversationId、全量替换写、丢弃工具消息 |

## 3. AgentOS 现状诊断

### 3.1 已具备的地基(编排可直接站上去)

`core/event_bus.py` 的 `StreamBus` 是会话级 `asyncio.Queue`,**主代理与所有并发子代理共享同一实例**,
`emit` 注入单调 sequence。`core/events.py` 的 `ActorRole` 已内建 `orchestrator / subagent / teammate`
三类角色,`Actor(role, name, run_id, task, team)` 区分并发来源。**多 agent 事件汇聚已经解决**,
这是最关键的既有资产。

`tools/subagents/definition.py` 的 `SubAgentSpec`(frozen dataclass:`agent_type` / `system_prompt` /
`allowed_tools` / `disallowed_tools` / `model` / `max_rounds` / `shell_access` / `omit_inherited_memory`)
+ `SubAgentRunner.run()` 构成"规格 → 隔离执行 → 结构化报告"的执行原语,独立 `AsyncSessionLocal()`、
进程级 `asyncio.Semaphore(subagent_max_concurrency=3)`、失败落 `subagent_runs` 审计均已就绪。

`interaction/` 的 `RuntimeSuspensionRecord`(每会话唯一活跃暂停点 partial unique index、
status 状态机、version 乐观锁)+ `AgentRuntime.resume(...)` 是完整的"暂停点持久化 + 跨请求续跑"。
**这就是 LangGraph checkpoint / ADK resumability 在 AgentOS 的等价物,已经存在。**

其余可复用件:`LLMClient.complete_structured`(强制 tool_use + 校验失败回灌重试一次)可直接做
Routing 裁决;`hooks/` 的四点钩子 + fail-open 注册表已有"Stop 钩子触发再编排"的先例
(`verification_trigger`);`task/` 的 `blocked_by` 依赖 + `claim_task` 竞争认领是 DAG 基础;
`permission/service.py` 的 `evaluate(..., agent_type=)` 让新 agent 自动获得按类型的权限裁决。

### 3.2 真正的障碍(必须先解决,否则编排会变成第四份实现)

**障碍一:三份平行的 ReAct 实现。** `AgentRuntime._run_llm_loop`(runtime/agent.py:552)、
`SubAgentRunner._run_react_loop`、`SubAgentRunner._run_teammate_loop`——消息来源、持久化方式、
挂起能力各不相同。`AgentRuntime` 已 1713 行,ReAct、挂起恢复、Plan Mode、hook、memory、
team 唤醒全耦合在 `_produce` / `_run_llm_loop` / `_execute_tool_uses` 三个方法里。

**如果在此之上加编排层,必然产生第四份 ReAct。这是本方案的第一优先级前置任务。**

**障碍二:并行是假的。** `_execute_tool_uses`(runtime/agent.py:796)对同批 tool_use 是 `for`
循环顺序 `await`,**没有 `asyncio.gather`**。`subagent_max_concurrency=3` 的信号量只在多个
独立请求间生效。teammate 推进则是 `_wake_team_members`(L1661)在请求内顺序 for、每人单步,
无后台常驻调度器。ParallelAgent 语义目前无法真实成立。

**障碍三:两套 Agent 体系互不相通。** `AgentRecord`(表 `agents`:`name` / `system_prompt` /
`model_name` / 工具绑定表)是用户配置,**没有 agent 类型字段**;`SubAgentSpec` 是代码内建常量
(`AgentType` 枚举四个值)。编排若要"按用户配置的 Agent 组队",两者必须打通。

**障碍四:安全模型挡死自下而上的派发。** `GLOBAL_SUBAGENT_DENIED_TOOLS` 硬禁子代理再派生
(Agent/TeamCreate/TeamSpawn),且子代理无审批通道——`ask` 直接坍缩为拒绝
(runtime/subagent.py:253)。**这个约束是对的,不要放开**;正确做法是让编排器持有派发权,
而不是让子层 agent 获得派发权。

**障碍五:零测试。** `tests/` 目录已从磁盘删除(33 个测试文件全部 `D`)。主循环依赖精细的
分步 commit 语义(挂起先提交再发事件、子代理独立 session),编排插入新提交点极易破坏恢复语义,
而当前无任何回归网。

**顺手发现的 bug:** `AgentType.GENERAL_PURPOSE = "general-purpose"`(连字符),而
`TeamSpawnInput.agent_type` 默认 `"general_purpose"`(下划线,tools/builtin/team.py:70)、
`team/service.py:201` 同为下划线;`runtime/agent.py:1681` 唤醒时 `AgentType(member.agent_type
or "general_purpose")` 对默认值抛 `ValueError`,被 try/except 吞成 `TEAMMATE_WAKE_FAILED`。
**默认 agent_type 的队友可能一次都唤不醒。** 应在阶段一顺手修掉。

## 4. 目标架构

四层,依赖单向向下。新增的只有 `orchestration/` 一个 feature 包和 `runtime/step.py` 一个原语文件。

```
orchestration/   编排层:Agent 协议 + 四种组合子 + 编排上下文/状态/事件冒泡
      ↓
runtime/step.py  执行原语:唯一一份 ReAct 单步实现(AgentStep),被所有上层复用
      ↓
llm/ tools/ permission/ hooks/   能力层(现状不变)
      ↓
session/ memory/ task/ team/ interaction/   持久化域(现状不变)
```

### 4.1 统一契约:一切 agent 都是 `Agent`

采纳 ADK 的核心判断——**工作流 agent 与 LLM agent 是同一个抽象的不同实现**。ADK 用
`BaseAgent.run_async(ctx) -> AsyncGenerator[Event]` 统一,区别只在 `_run_async_impl` 里是
"调 LLM"还是"确定性调度 sub_agents"。AgentOS 用 Python Protocol 表达会更干净:

```python
class Agent(Protocol):
    """一切可执行单元的统一契约:LLM agent 与编排组合子共用。"""

    name: str

    def run(self, ctx: OrchestrationContext) -> AsyncIterator[StreamEvent]: ...
```

三条不变量:

1. **返回 async iterator 而非返回值。** 单条响应就是长度为 1 的流。这一刀砍掉了 Spring AI
   call/stream 双实现的全部复杂度——不需要 `MessageAggregator`,不需要 `publishOn`。
2. **输出经 `ctx.state` 传递,不经返回值。** 借 ADK 的 `output_key`:agent 声明自己的输出键,
   执行完把结果写进共享状态,下游用占位符读取。返回值通道只用于流式事件。
3. **组合子不感知子节点类型。** `SequentialAgent(children=[...])` 里的每个 child 只要满足
   `Agent` 协议即可,是 LLM agent 还是另一个组合子无关——这是嵌套组合的全部机制。

### 4.2 关键决策:先提取 `AgentStep`,再谈编排

`AgentStep` 是从三份 ReAct 里提取出的**唯一一份**单步实现,职责边界严格:

```python
@dataclass(frozen=True)
class StepRequest:
    """一次 LLM 调用的完整参数,对应 Spring AI 的 Prompt + ChatOptions。"""
    messages: list[dict[str, Any]]
    system_blocks: list[TextBlockParam]
    tools: ToolRegistry
    model: str
    max_tokens: int | None = None


@dataclass(frozen=True)
class StepResult:
    content: list[dict[str, Any]]
    tool_uses: tuple[ToolUse, ...]
    stop_reason: str | None
    usage: Usage
    suspension: SuspensionSignal | None   # 权限 ask / 交互工具触发的挂起
```

`AgentStep` 只做一件事:**发一次 LLM 调用,翻译流式事件,执行本轮工具,返回结果**。它不管
循环、不管持久化策略、不管挂起恢复——这些是调用方的职责。三个既有 ReAct 循环随后都改为
"准备 `StepRequest` → 调 `AgentStep` → 按各自策略持久化 → 决定是否再来一轮"。

理由:Spring AI 2.0 把工具循环从 ChatModel 内部整体迁出到 `ToolCallingAdvisor`,收益是循环
每一轮都可被拦截。AgentOS 的对应动作就是把"单步"和"循环"切开。切开之后:

- 编排层的每个 LLM agent 节点 = `while` 包 `AgentStep`,与主循环共用一份语义;
- 中间件可以插在 `AgentStep` 的入口/出口,自动覆盖所有 agent(主代理、子代理、队友、编排节点);
- 首次具备可测性——替换 `AgentStep` 即得 fake,不必 mock Anthropic SDK。

**这是全案的前置任务。跳过它,编排层必然写出第四份 ReAct,`AgentRuntime` 会涨到 2500 行。**

### 4.3 中间件链:借 Advisor,不借 Ordered

```python
StepHandler = Callable[[StepRequest, OrchestrationContext], AsyncIterator[StreamEvent]]
Middleware = Callable[[StepHandler], StepHandler]
```

链由列表显式组装,顺序即执行顺序,**不用 order 魔数**。链尾是 `AgentStep` 本身(对应 Spring AI
把 `ChatModelCallAdvisor` 注册在 `LOWEST_PRECEDENCE`)。已有的 `hooks/` 不改动,它是"业务扩展点",
中间件是"框架切面",两者定位不同:`PRE_TOOL_USE` 关心单个工具,中间件关心整次调用。

初始中间件清单(全部从现有代码平移,不新增能力):记忆注入(`MemoryRecallService` 冻结上下文)、
上下文压缩(`CompactService.maybe_compact`)、预算闸门(轮次 + 单工具调用次数,补 Spring AI
的 `ToolCallLimits` 双层语义)、用量累计(跨轮 usage 聚合,当前缺失)、结构化日志。

**关于循环重入:** Spring AI 用 `chain.copy(this)` 让循环体每轮重放自己下游的链段。AgentOS 里
这个语义天然成立——中间件链在 agent 节点内部按轮次调用,每轮都完整穿过一次链,不需要 `copy`
这种补丁。这是 Python 闭包组合相对 Java 有状态 Deque 链的直接优势。

### 4.4 底层协议只保留 `stream`,不做多供应商适配层

两个刻意的取舍,均有实证支撑:

**`ChatModel` 不同时提供 `complete` 与 `stream`。** 双方法会沿调用链一路分叉:Spring AI 为此
付出 `CallAdvisor` / `StreamAdvisor` 两套接口,Alibaba 进一步付出独立的 4 方法
`StreamingModelInterceptor`,并在 `AgentLlmNode` 内按 `_stream_` 标志走两条不同分支。
本方案只定义 `stream`,`complete` 退化为"消费完流并聚合"的辅助函数(等价 Spring AI 的
`MessageAggregator`),中间件因此只需实现一次。

**暂不做 OpenAI / 通义 / DeepSeek / Ollama 适配层。** 它与多智能体编排正交——编排要的是
"能跑一次模型调用",不是"能跑任意供应商"。过早抽象会把 Anthropic 特有能力(cache
breakpoint、thinking block、强制 tool_use 做结构化输出)挤成各供应商的旁路开关,这正是
Spring AI `ChatOptions` 被打出一堆洞的成因。做法是先定义 `ModelRequest` / `ModelResponse` /
`ModelChunk` 值对象把 SDK 类型挡在边界外,**真出现第二个供应商时再补适配器**——届时补的
成本远低于现在猜错的成本。

## 5. 四种编排语义

四者都是 `Agent` 协议实现,可任意嵌套。核心设计:**语义只由"子节点如何被调度"和"状态如何合并"
两个维度决定**,其余(事件、挂起、权限、预算)完全共用。

### 5.1 SequentialAgent

顺序执行 `children`,前一个的输出通过 `ctx.state` 供后一个读取。对应 Anthropic 的 prompt chaining。

```python
async def run(self, ctx):
    # 游标必须读入,不能只写出:否则恢复时从头重跑整条流水线(见第 8 节)
    for index in range(ctx.resume_cursor(self.name, default=0), len(self.children)):
        ctx.set_cursor(self.name, index)               # 断点续跑锚点
        async for event in self.children[index].run(ctx):
            yield event
        if ctx.should_halt():          # 子节点挂起或触发终止
            return
```

关键约定:`cursor` 落进挂起记录,恢复时从 `cursor` 而非从头继续(ADK 的
`SequentialAgentState.current_sub_agent` 同义)。

### 5.2 ParallelAgent

`asyncio.gather` 并发执行 `children`,各分支状态隔离,结束时按 reducer 合并。对应 Anthropic
的 parallelization(sectioning / voting 两个变体)。

三个必须明确的语义:

**分支隔离。** 每个 child 拿到 `ctx.branch(child.name)` 派生的上下文:独立 `AsyncSession`
(复用 `SubAgentRunner` 已有的 `AsyncSessionLocal()` 模式)、独立消息视图、**共享 `StreamBus`**。
ADK 的 branch 只隔离历史可见性而 state 仍全局共享,导致官方要靠"约定不同 output_key"防竞态——
AgentOS 不抄这个缺陷:**分支状态写入先落各自的 scratch 区,合并时才进父状态**。

**事件合并。** 各分支事件带 `Actor.branch` 标识,直接 emit 到共享 bus。这里不需要 ADK 那套
`TaskGroup + Queue + resume_signal` 背压机制——`StreamBus` 已是 `maxsize=1000` 的队列,
背压天然由 queue 提供。

**并发闸门。** 复用现有进程级 `asyncio.Semaphore(subagent_max_concurrency)`,并在 `ParallelAgent`
上增加节点级 `max_concurrency`(对应 Alibaba `ParallelAgent.maxConcurrency`)。**必须有上限**:
无闸门的并行 + 每分支自带 ReAct 循环,成本呈乘积放大。

**失败策略显式化。** `fail_fast`(任一分支异常即取消其余,`gather(return_exceptions=False)`)
或 `collect_all`(收集全部结果与异常,交 reducer 裁决)。不设默认隐式行为——投票场景要
`collect_all`,流水线预处理场景要 `fail_fast`。

### 5.3 LoopAgent

重复执行 body 直到满足退出条件。对应 Anthropic 的 evaluator-optimizer(body = 生成 + 评审)。

三种终止条件,可组合:

- **计数**:`max_iterations` 硬上限,必填,无默认无限;
- **升级信号**:body 内任意 agent 写入 `ctx.escalate()` 即退出(ADK 的 `EventActions.escalate`,
  官方示例用 `exit_loop` 工具设置)。AgentOS 落地为一个内建工具 `LoopExit`,只在 Loop 上下文中
  可见——工具可见性按节点裁剪已由 `ToolRegistry.only()` 支持;
- **谓词**:`Callable[[OrchestrationContext], bool]` 读状态判定,用于"评分达标即停"。

**注意:AgentOS 不应把 LoopAgent 与 `max_tool_iterations` 混淆。** 后者是单个 agent 内部的
ReAct 轮次上限(工具调用循环),前者是编排级的宏迭代(整个 body 重跑)。两者是不同层级的循环,
预算需分别计量。

**预算耗尽必须是显式失败,不能静默完成。** Alibaba graph-core 的 `recursionLimit`(默认 100)
命中后**不抛异常**,直接走 `handleCompletion` 以当前状态正常收尾——与 LangGraph 抛
`GraphRecursionError` 相反。后果是一个死循环编排会安静返回残缺结果,调用方无从分辨"跑完了"
和"被截断了"。AgentOS 的编排预算耗尽必须落到独立的终止状态(如 `budget_exhausted`)并发事件,
现有 `_finalize_without_tools` 的优雅降级只适用于单 agent 层,不可上推到编排层。

### 5.4 RoutingAgent

用 LLM 从候选 children 中选一个执行。对应 Anthropic 的 routing。

```python
async def run(self, ctx):
    decision = await self.llm.complete_structured(
        messages=ctx.routing_messages(),
        output_format=RouteDecision,          # {target: str, reason: str}
        system_prompt=self.routing_prompt,
        model=self.router_model,              # 可用更便宜档位
    )
    target = self.children.get(decision.target) or self.fallback
    yield ctx.emit_routing_event(decision)    # 决策必须可观测
    async for event in target.run(ctx):
        yield event
```

三点设计:**直接复用 `LLMClient.complete_structured`**(已有强制 tool_use + 校验失败重试一次);
**必须有 `fallback`**(对应 Alibaba `LlmRoutingAgent.fallbackAgent`),LLM 返回未知目标时不抛异常;
**路由决策本身是一个事件**,`reason` 字段进事件流,否则线上无法解释"为什么走了这条分支"。

路由提示词可复用 `tools/subagents/registry.py:routing_catalog()` 的既有模式(它已在为主代理
生成子代理路由说明)。

## 6. 状态与数据流

### 6.1 双通道:消息列表 + KV 状态

ADK 是"消息列表(session.events)与 KV(session.state)并存",但 KV **没有 reducer**;
LangGraph 与 Alibaba 是"纯 KV + per-key reducer"(`KeyStrategy` 的 REPLACE/APPEND/MERGE)。

**AgentOS 取两者之长:消息列表沿用现有 `session_messages` 表(不动),新增 KV 状态带 reducer。**

理由:消息列表是 LLM 上下文的载体,AgentOS 已有完整的持久化 + 快照 + 压缩 + Turn 冻结记忆,
没有理由重做;KV 状态是编排节点间的结构化数据通道,当前完全缺失。

```python
@dataclass(frozen=True)
class StateKey:
    name: str
    reducer: Reducer = Reducer.REPLACE     # REPLACE | APPEND | MERGE
    scope: Scope = Scope.RUN               # RUN(单次编排) | SESSION(跨轮) | WORKSPACE(跨会话)
```

`scope` 借 ADK 的 `temp:` / 无前缀 / `user:` / `app:` 前缀作用域,但用显式枚举字段而非字符串前缀
约定——前缀约定是隐式契约,容易写错且无法静态检查。

`WORKSPACE` 作用域的状态**不新建表**:它就是 `memory/` 域的记忆项,应通过 `MemoryService`
的公开方法读写(遵循 CLAUDE.md 第 9 条:跨域读写在数据所属域的 Service 上补方法)。
`RUN` 与 `SESSION` 作用域落编排自己的表(见第 9 节)。

### 6.2 输出键与占位符注入

子节点声明 `output_key`,执行完把最终文本(或结构化结果)写入状态;下游节点的 prompt 里用
`{key}` 占位符引用,渲染发生在组装 `StepRequest` 之前。

与 ADK 的差异:**ADK 的 `{key?}` 容忍缺失,AgentOS 应默认严格失败。** 编排里一个占位符没被填上
通常意味着上游没跑或键名写错,静默替换成空串会让 LLM 拿到残缺 prompt 却照样输出,
这种故障极难定位。需要可选语义时显式声明默认值。

### 6.3 并行合并

reducer 在**父节点**上按 key 声明,分支写入先落 `ctx.branch_scratch[branch_name]`,
`ParallelAgent` 收束时统一 apply。三种内建 reducer 覆盖主要场景:

- `REPLACE`:后写覆盖(需配合确定性顺序,并行下不推荐);
- `APPEND`:列表拼接,sectioning 场景(各分支处理不同片段);
- `MERGE`:dict 浅合并,voting 场景配合自定义裁决函数。

投票/裁决**不做成第五种 reducer**,而是"ParallelAgent 后接一个 LLM agent 节点"——用组合表达,
不往 reducer 里塞 LLM 调用。这是保持组合子正交的关键。

**状态的不可变性必须由类型强制,不能只靠约定。** Alibaba 的 `OverAllState` 类注释明确标注
非线程安全,节点拿到的是**活引用**,只有 `ParallelNode` 会 `state.snapShot()` 给每个分支;
"只读 + 返回 patch"在那里是文档约定,写坏了编译期发现不了。AgentOS 应让状态对象 frozen,
patch 是唯一写通道,让"节点直接改状态"在类型层面写不出来。这是并行下唯一可靠的隔离方式。

**失败模式补第三种 `RACE`。** Alibaba 的 `ParallelNode` 除 `ALL_OF` 还支持 `ANY_OF`:
首个成功分支胜出,其余 cancel。对"多模型竞速取最快"有用,Python 侧用
`asyncio.wait(..., return_when=FIRST_COMPLETED)` + 显式 cancel 即可,成本很低。
`NodeSpec.failure_mode` 因此取 `fail_fast | collect_all | race` 三值。

## 7. 事件冒泡与流式

ADK 靠 async generator 链式 `yield` 逐层上抛,Alibaba 靠 graph runtime 的 Flux 上浮。
AgentOS 已有更简单的答案:**`StreamBus` 是会话级共享实例,任何深度的嵌套节点直接 `emit`,
无需逐层冒泡。**

需要扩展的只有事件协议:

- `Actor` 增加 `branch: str | None`(并行分支标识)与 `node_path: str`(如
  `root/parallel-review/reviewer-a`),用于前端按节点分组渲染;
- 新增事件类型:`node_start` / `node_end`(编排节点生命周期)、`routing_decision`(含 reason)、
  `loop_iteration`(含轮次序号与退出原因)。三者都是可观测性刚需,不是可选项;
- `ActorRole` 增加 `orchestrator` 之外的语义?**不需要**——编排节点本身不产生文本,
  它产生的是 `node_*` 结构事件,真正的文本仍由叶子 LLM agent 以 `subagent` 角色发出。

**顺序保证:** `StreamBus.emit` 已注入单调 sequence,并行分支的事件在总线上交错但每个分支内部
有序。前端按 `(branch, sequence)` 分组即可正确渲染,不需要在后端做重排。

## 8. 中断、恢复与人工介入

**这是 AgentOS 相对 Spring AI 的最大优势项,也是编排设计里最容易搞坏的部分。**

现有机制:`RuntimeSuspensionRecord` 每会话唯一活跃暂停点(partial unique index)+ status 状态机
+ version 乐观锁,`AgentRuntime.resume(session_id, request_id, kind, response_payload)` 续跑。

编排引入后需要三处扩展,**不改变"每会话唯一活跃暂停点"这个约束**:

**一、游标必须外化,这是组合子能否替代图引擎的分水岭。**

Alibaba graph-core 的 `Checkpoint(id, state, nodeId, nextNodeId)` 存全量状态快照,恢复时从
`nextNodeId` **精确续跑,已执行节点不重放**。图引擎相对组合子唯一难以替代的能力就是这个
"可寻址的执行位置"。

组合子**可以**有等价地址——`node_path` + 每层游标构成的游标栈,可序列化:

```
[("root/seq", 2), ("root/seq/loop", 3)]
```

但前提是**游标从上下文读入,而不是只写出去**。反例(错误写法):

```python
for index, child in enumerate(self.children):
    ctx.checkpoint(cursor=index)      # 写出去了,但恢复时仍从 0 开始遍历
```

正确写法:

```python
start = ctx.resume_cursor(self.name, default=0)   # 恢复时非 0
for index in range(start, len(self.children)):
    ctx.set_cursor(self.name, index)
```

差别很细但致命:前者进程重启后从头重跑整条流水线,后者精确续跑。**任何跨 await 且可能挂起的
位置信息,都不允许只存在 Python 局部变量里。** 这是本方案对组合子路线的核心约束,
等价于 ADK 的 `SequentialAgentState.current_sub_agent` / `LoopAgentState.times_looped`。

挂起载荷因此需要装:`node_path` + 游标栈 + RUN 作用域状态快照。AgentOS 已有持久化载体
(`RuntimeSuspensionRecord`),只需扩展字段。**不要新建 CheckpointStore**——Alibaba 有 9 个
saver 实现是通用框架的负担,AgentOS 再建一套会出现两个恢复机制打架。

**二、并行分支挂起的语义必须明确。** 一个分支触发权限 `ask` 时:其余分支**已启动的继续跑到完成**,
未启动的不再启动,整个 `ParallelAgent` 转为挂起,已完成分支的结果落 scratch 区持久化。
恢复时只重跑挂起的那个分支。

理由:取消已在跑的分支会浪费已花的 token 且中途状态难以清理;让全部分支跑完再挂起最简单可靠。
代价是恢复延迟,可接受。

**三、子代理审批通道的定位不变。** 子代理 `ask` 坍缩为 deny 这个设计**保留**。编排节点需要人工
审批时,由**编排器**发起挂起(它运行在主会话上下文,有审批通道),而不是让叶子 agent 获得审批能力。
同理,`GLOBAL_SUBAGENT_DENIED_TOOLS` 禁止子代理递归派生的约束保留——派发权只属于编排器。

## 9. 数据模型

新增三张表,归 `orchestration/` 域。全部继承 `Base` 的审计字段与软删除,登记进
`database/registry.py`,提供显式 upgrade/downgrade 迁移。

**`orchestration_definitions`(编排定义,可复用模板)**
`workspace_id` / `name` / `description` / `spec`(JSONB,嵌套的节点树) / `is_enabled` /
`created_by_user_id`。`spec` 存 Pydantic 校验过的节点树,不存 Python 对象;加载时
`model_validate` 还原为组合子实例。

**`orchestration_runs`(编排运行实例)**
`definition_id` / `session_id` / `turn_id` / `status`(running / awaiting_interaction /
completed / failed / interrupted,与 `session_turns.status` 取值对齐) / `cursor`(JSONB,
当前 node_path 与各层游标) / `state`(JSONB,RUN 作用域 KV) / `usage`(JSONB,累计 token)。

**`orchestration_node_runs`(节点级运行审计)**
`run_id` / `node_path` / `branch` / `node_type` / `status` / `input_state` / `output_state` /
`usage` / `error`。这张表是可观测性与成本归因的基础,**不是可选项**:没有它,一次并行编排的
token 花在哪个节点上无法回答。

与既有表的关系:`AgentRecord` 增加 `agent_type` 字段并补"从 AgentRecord 生成 SubAgentSpec"的
映射(打通障碍三);`subagent_runs` 保留不动(它记录一次性子代理运行,与 node_runs 是不同粒度,
node_runs 记编排节点,subagent_runs 记该节点内实际派出的子代理)。

`orchestration_definitions.spec` 的节点树 schema 草案:

```python
class NodeSpec(BaseModel):
    kind: Literal["llm", "sequential", "parallel", "loop", "routing"]
    name: str
    children: list[NodeSpec] = Field(default_factory=list)
    agent_id: int | None = None              # kind == "llm" 时指向 AgentRecord
    output_key: str | None = None
    state_keys: list[StateKeySpec] = Field(default_factory=list)
    max_iterations: int | None = None        # kind == "loop"
    max_concurrency: int | None = None       # kind == "parallel"
    failure_mode: Literal["fail_fast", "collect_all", "race"] = "fail_fast"
```

## 10. 分阶段落地

顺序有强依赖,不建议调换。每阶段结束应可独立验证。

**阶段零:补回归网(前置,不可跳)**
`tests/` 已空。至少恢复主循环、挂起恢复、权限裁决、子代理并发、压缩这五组测试。
JSONB / 锁 / 事务隔离按 CLAUDE.md 第 2 条走真实 PostgreSQL 集成测试。
顺手修 `agent_type` 连字符与下划线不一致的 bug。

**阶段一:提取 `AgentStep`(核心重构)**
从 `_run_llm_loop` / `_run_react_loop` / `_run_teammate_loop` 提取唯一一份单步实现,
三处改为调用它。行为必须完全等价——这是纯重构,靠阶段零的测试保证。
产出:`runtime/step.py`,`AgentRuntime` 应显著瘦身。

**阶段二:中间件链**
落 `StepHandler` / `Middleware`,把记忆注入、压缩、预算闸门、用量累计、日志从主循环平移进中间件。
补 Spring AI `ToolCallLimits` 的单工具调用上限。`hooks/` 不动。

**阶段三:`asyncio.gather` 修真并行**
`_execute_tool_uses` 改为并发执行同批 tool_use(需处理:工具间 DB 会话隔离、事件顺序、
权限 ask 与并发的交互)。这一步独立于编排,本身就是现有能力的正确性修复。

**阶段四:`Agent` 协议 + Sequential + Routing**
先落两个最简单的组合子:Sequential 无并发问题,Routing 可直接复用 `complete_structured`。
配套 KV 状态(RUN 作用域)、占位符注入、`node_*` 事件、`orchestration_*` 三张表与迁移。

**阶段五:Parallel + Loop**
依赖阶段三的并发基础。Parallel 落分支隔离、reducer、失败策略、并发闸门;
Loop 落三种终止条件与 `LoopExit` 内建工具。

**阶段六:打通 AgentRecord**
`AgentRecord` 加 `agent_type`,实现 `AgentRecord → SubAgentSpec` 映射,
让用户配置的 Agent 可作为编排节点。API 层暴露编排定义的 CRUD 与触发。

阶段零到三是"修地基",不产出新功能但决定后面能否走通;阶段四到六才是多智能体本身。
如果时间只够一半,**做零到三比做四到六更有价值**——前者让现有三条 agent 链路正确且可测,
后者建在流沙上。

## 11. 明确不做

**不做图引擎(StateGraph / 条件边 / 任意拓扑)。** 组合子是图的受限子集,能表达 Anthropic 五种
workflow 模式的全部。真需要任意拓扑时再演进,届时组合子可作为图的糖。过早引入图引擎会带来
调度器、拓扑校验、环检测、通道系统一整套复杂度,与当前规模不匹配。

这个判断经 Alibaba graph-core 实证检验后成立,补充三条依据:

- **并行场景图引擎买到的是零。** `CompiledGraph` 在 compile 期检测到某条边
  `targets.size() > 1` 才合成 `ParallelNode`,并施加两条编译期硬约束:所有分支必须汇聚到
  同一目标节点,分支上不允许条件边。这与 `ParallelAgent(children=[...])` **完全等价**——
  最需要图的场景恰好没提供任何超出组合子的能力,`ParallelNode` 本质是静态语言缺少灵活
  调度时的补偿。
- **组合子丢掉的四项里只有一项真需要。** ①任意拓扑跳转与回边:可用 Loop + 组合子覆盖
  (ReAct 本身就是 while 循环);②基于图位置的 checkpoint 恢复:**真需要**,已在第 8 节
  升级为硬约束;③图结构导出/可视化;④图级别守卫。②之外的三项均非当期需求。
- **图引擎自身的复杂度大头是 Java/Reactor 包袱。** `NodeExecutor` 近千行,大半在做流桥接
  (`GraphFlux` / `ParallelGraphFlux` 归并 / 流结束后回写状态);`AsyncGenerator` 整类已
  `@Deprecated`,模块整体迁往 Reactor 后三套异步模型并存互转;`expandDeep` 是为防栈溢出
  打的补丁。这些代价在 Python asyncio 里几乎为零,不构成引入图引擎的理由,也不构成
  组合子路线的风险。

**不做 LLM 自主的 agent 间转移(ADK `transfer_to_agent` / OpenAI handoff)。** 那是"agent 动态
决定控制流",与本方案"确定性编排 + Routing 节点做受控决策"是两条路线。AgentOS 现有的
`Agent` 工具(子代理调用,控制权不转移)已覆盖 ADK `AgentTool` 的语义,足够。同时开两条路线
会让"谁在决定下一步"变得不可推理。

**不放开子代理递归派发,不给子代理审批通道。** 见第 8 节。派发权与审批权只属于编排器。

**不为 Spring AI 的 Memory 抽象让路。** AgentOS 的 Session / Memory 领域模型全面领先,
只吸收"裁剪对齐 USER 轮次"和"注入幂等检查"两条细节。

**不引入 `Ordered` 式魔数排序。** 中间件链用列表顺序显式表达。

**不做 teammate 后台常驻调度器。** 它是独立议题(当前是请求内单步唤醒),与编排耦合会同时
放大两处风险。编排要并行就用 `ParallelAgent`,不要借道 teammate。

## 12. 待定问题

**编排触发入口。** 三种可能:独立 API(`POST /api/orchestrations/{id}/run`)、
主代理的一个工具(`RunOrchestration`)、或会话绑定编排定义后自动生效。倾向前两者并存——
独立 API 服务确定性流水线,工具形态让主代理按需调用编排。需要产品侧确认使用场景。

**编排定义的编写方式。** JSONB spec 适合 API/UI 构造,但手写嵌套 JSON 体验差。
是否需要 Python DSL(`Sequential(Parallel(a, b), Loop(c, max_iterations=3))`)作为代码内定义方式,
与 JSONB 双轨并存?倾向需要——内建编排(如验收流水线)用 DSL 更清晰。

**RUN 状态的持久化时机。** 每节点结束即 commit(恢复精确,写放大)还是仅在挂起点 commit
(写少,崩溃丢进度)。倾向前者,与现有"分步 commit 用于增量持久化"的约定一致,
但需实测写入开销。参考:Alibaba 的 `Checkpoint` 存**全量状态快照而非增量**,以写放大换恢复
逻辑的极简;AgentOS 状态规模小,JSONB 全量快照同样是合理起点。

## 附录:草稿分层与本方案的对应关系

| 草稿层 | 本方案落点 | 差异说明 |
| --- | --- | --- |
| `BaseAgent` / `AgentExecutor` | 4.1 `Agent` Protocol + 4.2 `AgentStep` | 一致。`AgentStep` 是必须先做的前置项,否则编排会成为第四份 ReAct 实现 |
| `FlowNode` | 组合子自身(`SequentialAgent` 等) | **不引入独立 FlowNode 类型**:组合子实现同一个 `Agent` Protocol,才能任意嵌套 |
| `FlowState` / `StatePatch` | 6.1 `StateKey` + reducer | 一致,且比 Alibaba 更严:状态 frozen,patch 是唯一写通道 |
| `WorkflowExecutor` | 组合子的 `run` + `OrchestrationContext` | 单层足够。但要把"判停止 / 超限 / 中断"与"执行单节点"分开,别混成一个方法 |
| `Sequential/Parallel/Routing/Loop` | 第 5 节 | 一致。`Conditional` 归入 Routing,不单列 |
| `LLMClient` / `ToolRegistry` / `Memory` | 沿用现有 `llm/` `tools/` `memory/` | 不新建,通过中间件接入(4.3) |
| `CheckpointStore` | 扩展 `RuntimeSuspensionRecord` | **不新建**:再建一套会与现有 `resume()` 打架 |
| `EventSink` | 沿用 `StreamBus` | 一致。这是本方案相对 ADK 的结构优势(第 7 节) |
| `ChatModel` 双方法 | 4.4 只保留 `stream` | 已否决 `complete` 独立方法 |
| 多供应商模型适配层 | 4.4 暂不做 | 与编排正交,真出现第二供应商再补 |

"模式类是薄薄的控制流组合层"这一原则经实证成立:Alibaba 的 `buildCoreGraph` 均为 36–63 行纯
`addNode` / `addEdge`,Sequential 与 Conditional 甚至不 import 任何 Spring AI 类。但要注意
**它的薄有代价**:模式语义被切成"构建期接线"(`strategy/`)与"运行期节点"(`flow/node/`、
`agent/loop/`)两半,读代码要来回跳。本方案让组合子直接持有运行期逻辑(`run` 方法内即控制流),
以少一层间接换取语义集中。
