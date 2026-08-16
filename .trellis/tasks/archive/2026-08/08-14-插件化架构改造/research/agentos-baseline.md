# 研究：AgentOS 现状基线（改造起点）

调查时间：2026-08-14，分支 `feature/unify-llm-client`。

## 零、两个必须先知道的现实

1. **工作区当前 import 即失败**（已实测复现）：
   ```
   ModuleNotFoundError: No module named 'llm.client'
   ```
   `llm/client.py` 已删除，但 7 个调用方仍引用：
   `runtime/agent.py:32`、`runtime/subagent.py:21`、`runtime/compact.py:6`、
   `memory/recall/service.py:13`、`memory/jobs/{service.py:14, dream.py:17, extraction.py:15}`。
   这是 `.trellis/tasks/08-14-统一大模型调用入口` 任务的中途断点（`llm/factory.py` 计划中但未创建）。

2. **测试已全部删除**：`tests/` 目录不存在，全仓 0 个 `test_*.py`。
   提交 e872cdc 一次性删除 25 个测试文件（15250 行），其中含真实 PostgreSQL 集成测试。
   `pyproject.toml:26-28` 仍配 `testpaths=["tests"]`。

## 一、规模与分层

约 32000 行 Python（不含 alembic 的 3820 行）。单体 FastAPI，按 feature 域分包
（models/schemas/repository/service/api 五件套），**无插件机制、无 DI 容器**。

路由静态聚合：`api/router.py:3-17` 硬 import 15 个域，:20-38 逐个 `include_router`；
`main.py:37-52` 应用工厂串 CORS/异常/中间件/lifespan（:24-33 建表 + 工具目录同步、:39 hook 注册）。

## 二、主循环：`runtime/agent.py` 的 `AgentRuntime`（1712 行上帝类）

`_run_llm_loop()`（552-702 行），`for` + `for-else` 结构：

```python
for _ in range(settings.max_tool_iterations):          # 上限 50
    history, current, through_message_id = await self.session_service.load_context_for_turn(...)
    history = await self.compact_service.maybe_compact(...)          # 压缩焊在循环内
    async for chunk in self.llm.stream(context, system_prompt, tools=tools, model=...):
        if chunk.get("type") == "message_final": final_content = chunk.get("content")
        for event in translator.translate(chunk): await self.bus.emit(event)
    tool_uses = extract_tool_uses(final_content)
    assistant_message = await self.session_service.add_message(...)  # 每轮落库
    if not tool_uses:
        continuation = await self._apply_stop_hooks(...)
        if continuation is not None: continue
        break
    interaction = await self._execute_tool_uses(...)
    if interaction is not None:
        return TurnLoopResult(suspended=True, ...)                    # 人工交互挂起点
else:
    await self._finalize_without_tools(...)                           # 轮次耗尽收尾
```

外层是生产者-消费者：`run()` 起后台 task `_produce()`，事件 emit 到 `StreamBus`，SSE 端抽干。
挂起/恢复：遇交互工具或权限 `ask` 时把剩余 tool_uses 序列化成 continuation 存
`RuntimeSuspensionRecord`，`resume()` 加锁重放。

**循环内硬编码依赖清单**（插件化要拆的全部）：

| 依赖 | 位置 |
|---|---|
| Anthropic SDK 类型 | `agent.py:11` `from anthropic.types import TextBlockParam, ToolParam` |
| `LLMClient()` 直接实例化 | `agent.py:113`（无工厂、无注入） |
| `AnthropicStreamTranslator` | `agent.py:565` |
| `build_tool_registry()` | `agent.py:114, 1451` |
| 10 个具体 Service 构造 | `agent.py:107-121`；循环内再 new SkillService(:574)/TaskService(:193)/AgentService(:1463) |
| Plan Mode 工具白名单 | `agent.py:63-75` `PLAN_MODE_ALLOWED_TOOLS`；:58-62 `INTERACTION_TOOL_KINDS` |
| 工具名字符串特判 | `agent.py:800-901` `"Agent"`/`"EnterPlanMode"`/`"ExitPlanMode"` if-else |
| 权限 + DANGEROUS_TOOLS | `agent.py:41, 903, 1110-1114` |
| Hook 全局单例 | `agent.py:1624, 1650` `get_hook_registry().trigger(...)` |
| 队友唤醒 | `agent.py:1673-1693` 直接 import `AsyncSessionLocal` + `SubAgentRunner` + `TeamService` |

**第二个循环**：`runtime/subagent.py:330-391` `_run_react_loop()`，同构
（`for _ in range(spec.max_rounds)`），消息不落库，同样硬编码 `LLMClient()` 与 `build_tool_registry()`。
子代理无审批通道，`ask` 坍缩为拒绝。

## 三、工具系统

`BaseTool`（`tools/base.py:31-53`）：`name`/`description`/`input_model`（Pydantic），
`to_param()` **直接产出 `anthropic.types.ToolParam`**（格式耦合点），`async run(args, ctx) -> str`。
**工具输出统一是 str。**

`ToolContext`（`tools/base.py:15-28`）dataclass：
`session_id / db / bus / turn_id / user_id / workspace_id / allowed_tool_names / allowed_skill_names /
shell_access / shell_tmp_root`。工具内经 `ctx.db` 自行 new Service。

注册：`tools/registry.py` 顶部静态 import 24 个工具，`build_tool_registry()` 硬编码列表实例化，
**在 5 处被反复调用**（agent.py:114/1451、subagent.py:76、tools/service.py:35/87/109）——无单一装配点。

唯一执行交汇点 `ToolService.run`（`tools/service.py:165-278`）：PreToolUse hook（可 block / 改写入参）
→ 工具体 → PostToolUse hook（仅观测）。权限裁决在更高层（主循环）。

**缺失**：统一工具超时（仅 Bash 自带、Skill 有 `skill_exec_timeout`）、统一输出截断。
Shell 安全是**静态命令白名单**（`tools/shell_policy.py`，三档 `FULL/READ_ONLY/TMP_WRITABLE`），
文件头自述「不是 OS 级沙箱」。

## 四、LLM 层（重构中）

`llm/base.py:10-45` `BaseLLMClient(ABC)` 两方法：`stream()`（产 `dict[str,Any]` 原始事件 +
自定义 `message_final` 收尾帧）、`complete_structured[T: BaseModel]()`。
- `AnthropicClient`（204 行）：`AsyncAnthropic.messages.stream()`，`max_tokens=131072` 硬编码；
  `complete_structured` 用 tool-use 强制 + 失败回灌重试一次。
- `OpenAIClient`（83 行）：**`stream()` 直接 `raise NotImplementedError`**。
- **无 `llm/factory.py`**，调用方未接线。

`llm/types.py:118-220` `AnthropicStreamTranslator` 是唯一流协议翻译。

## 五、会话与事件

存储（`session/models.py`）：`sessions` / `session_messages`（`content` JSONB **直接存 Anthropic 内容块**）/
`session_turns`（UUID + 状态机 + CHECK 约束）/ `session_snapshots`（压缩快照 + `through_message_id` 水位）。
上下文加载 = 快照 + 水位后消息 + 当前 Turn 原样（`session/service.py:432-466`）。

事件总线 `core/event_bus.py:11-50` `StreamBus`：**会话级、进程内 `asyncio.Queue(maxsize=1000)`**，
每次请求 new 一个。**不跨进程、不持久化**（Redis 配置项存在但全项目零 import）。
事件类型 `core/events.py`：`StreamEvent`（10 种 EventType）+ `RuntimeEvent` + 三类 `Actor`。
推前端：**SSE**，无 WebSocket。

**关键差距**：真源是 `session_messages` 关系表，`StreamEvent` 不落库——
**没有事件溯源，没有日志重放**。这与 dsh 的「模型可见即已记录」是根本性差异。

## 六、DI 现状

FastAPI `Depends` **只存在于 API 层**（`get_db` / `get_current_user` / `get_current_workspace_id`）。
Service 层无容器无接口：构造函数收 `db: AsyncSession`，然后**直接 new 其它具体 Service**
（`agent/service.py:44-49` new 三个跨域 Service；全仓 30 个文件有 `XxxService(db)` 直接实例化）。
少数支持可选注入：`CompactService(repo, llm=None)`、`MemoryRecallService(db, llm=None)`、
`SkillService(db, storage=None, executor=None)`。

## 七、配置

`core/config.py:14-139` 单个 `Settings(BaseSettings)`，前缀 `AGENTOS_`，`extra="forbid"`，
自定义源顺序 **init > dotenv > env > secrets**（.env 覆盖 shell 环境变量）。
模块级全局单例 `settings = Settings()`（:143），**22 个文件直接 import**，
且 `tools/subagents/registry.py:97` 在 **import 期**读配置构建 SUBAGENT_SPECS。
无环境分层、无运行时刷新、无命名空间隔离。约 80 个字段，其中 memory 独占 39 项。

## 八、已有扩展点雏形（全部启动期静态装配，不可逆）

| 雏形 | 成熟度 | 缺什么 |
|---|---|---|
| `ToolRegistry`（tools/registry.py:38-125） | ★★★ | 注册列表硬编码，无 entry-point/配置装载 |
| `HookRegistry`（hooks/__init__.py:83-143，4 事件点） | ★★★ | 全局单例、仅进程内回调、无优先级/卸载 |
| `SubAgentSpec` 注册表（tools/subagents/） | ★★★ | AgentType 是**封闭 enum**，SPECS import 期冻结 |
| Agent 实体（DB 级可配置提示词/模型/工具白名单） | ★★★ | 只能**收窄**内建集合，不能引入新实现 |
| Skill 系统（zip→MinIO→三工具） | ★★★ | 唯一支持运行时上传新能力的通道，但只是「资料+脚本」非代码插件 |
| `BaseLLMClient` | ★★ | 工厂缺失、调用方未接线 |
| `Executor` / `ObjectStorage`（ABC + 单例工厂） | ★★ | 切换逻辑不存在 |
| `permission`（四层优先级 DB 规则） | ★★ | DANGEROUS_TOOLS 硬编码 |
| `prompt/compose.py` | ★ | 纯函数固定管线，无段注册机制 |
| `database/registry.py` | ★ | 手工清单，单一 metadata，插件无法自带表/迁移 |

## 九、明确不存在的能力

插件加载/卸载 · DI 容器/按 key 查找 · OS 级沙箱 · 调度器/cron ·
UI/前端（纯后端 API+SSE）· MCP 集成（`pyproject.toml:23` 声明 `mcp[cli]` 但零 import）·
Redis/跨实例事件 · LLM 工厂 · WebSocket · **事件溯源/日志重放**
