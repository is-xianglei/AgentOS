# 研究：tools 域拆包逐工具清单（P3-3 可执行化）

- **Query**: 把 implement.md P3-3（「3134 行 + 24 个工具拆包」）变成可执行清单；回答 Q1–Q4
- **Scope**: internal，纯静态阅读（工作区 `llm.client` import 断裂未修，未运行任何代码）
- **Date**: 2026-08-15

## 〇、先更正计数：是 26 个工具，不是 24 个

`tools/registry.py:89-125` 的 `build_tool_registry()` 实例化 **26** 个工具；
`tools/builtin/__init__.py:28-58` 的 `__all__` 亦为 26 项。文档链上有三个数字并存：

| 出处 | 数字 |
|---|---|
| design.md:228、:247 / implement.md:114、:116、:261 / prd.md:122 / research/agentos-baseline.md:85 | 24 |
| prompt/compose.py:53 注释（「22 个工具约 2.8k token」） | 22 |
| 实际（registry.py:89-125 逐项数；builtin/`__all__` 交叉验证） | **26** |

走势说明工具数在涨而文档没跟着改（何时开始失真未考证 git 历史）。
**3134 行仍准确**：`wc -l` 实测 tools/ 全包合计 3134。

行数分布——拆包对象远不只 26 个工具类，非工具文件占大头：

| 文件 | 行数 | 内容 |
|---|---|---|
| tools/shell_policy.py | 355 | Bash 的命令级访问策略（FULL / READ_ONLY / TMP_WRITABLE 三档） |
| tools/service.py | 278 | ToolCatalogService（DB 目录）+ ToolService.run（执行编排 + hooks） |
| tools/subagents/（4 文件） | 684 | AgentType / SubAgentSpec / SPECS 冻结 / 验收触发阈值 / 提示词 |
| tools/repository.py + models.py + api.py + schemas.py | 380 | tools、tool_calls 两张表的域五件套 |
| tools/base.py + registry.py + builtin/`__init__.py` + 两个 `__init__` | 236 | ToolContext / BaseTool / ToolRegistry / 静态聚合 |
| tools/builtin/ 14 个工具文件 | 1201 | 26 个工具类 |

## 一、逐工具主表（26 行，一个不漏）

- 「位置」列给类定义行区间，括号内是文件总行数（多工具文件的共享 helper 未分摊）。
- 「成组」列标注不能单独成包的组，细节见表后。
- 「档」= 第六节的 A（几乎照搬）/ B（要改返回值或依赖）/ C（要重构）。
- 路径省略前缀 `tools/builtin/`。

| # | 工具名 | 位置 | 返回值现状 | ToolContext 取用 | 直接依赖 / 越界 | 成组 | seam | 档 |
|---|---|---|---|---|---|---|---|---|
| 1 | echo | echo.py:10-16（16） | 裸 str：原样回显 | 无 | 无 | 独立 | （演示，去留待决策） | A |
| 2 | Weather | weather.py:10-16（16） | 裸 str：假数据文案 | 无 | 无 | 独立 | （演示，去留待决策） | A |
| 3 | Read | read.py:15-45（45） | str，行号约定 `{n}\t{line}` | 无 | 直接 `open()` 读盘 | fs 五件套 | fs | B |
| 4 | Write | write.py:14-29（29） | str 中文成功文案 | 无 | 直接 `open()` 写盘 | fs 五件套 | fs（+approval） | B |
| 5 | Edit | edit.py:16-56（56） | str 成功文案含替换计数 | 无 | 直接 `open()` 读写盘 | fs 五件套 | fs（+approval） | B |
| 6 | Glob | glob.py:15-45（45） | str：路径 `\n` 列表，超限加统计前缀 | 无 | `Path.glob` 直接扫盘 | fs 五件套 | fs | B |
| 7 | Grep | grep.py:19-83（83） | str：`file:line:content` 行 + 摘要行 | 无 | `open()` 逐文件扫盘 | fs 五件套 | fs | B |
| 8 | Bash | bash.py:21-108（108） | str：`[退出码: N]` + `=== stdout/stderr ===` 分段；策略拒绝走 `error: ` 前缀（:62） | session_id（日志）、shell_access、shell_tmp_root | `get_executor()` 全局单例（core/executor.py:110-118）、`os.environ`、shell_policy 前置校验（:49-53） | Bash+shell_policy | shell | C |
| 9 | EnterPlanMode | plan_mode.py:46-54（80） | `run()` 恒抛异常；真实输出由循环合成（runtime/agent.py:1312-1338，文案取自 plan/prompts.py:17） | 无（不执行） | core.errors | plan/交互组 | approval + plan_mode 插件 | C |
| 10 | ExitPlanMode | plan_mode.py:57-66 | 恒抛异常；审批输出由 runtime/agent.py:1340-1391 合成 | 无 | schema 的 `allowedPrompts` 别名直透 interaction payload（runtime/agent.py:1354） | plan/交互组 | approval + plan_mode 插件 | C |
| 11 | WritePlan | plan_mode.py:69-80 | str 成功文案（版本 + 字数） | session_id、db | `plan.service.PlanService`（:79） | plan/交互组 | plan_mode（消费 plan 域） | C |
| 12 | AskUserQuestion | ask_user_question.py:10-20（20） | 恒抛异常；应答文本由 runtime/agent.py:1393-1428 合成 | 无 | input_model 直接复用 `interaction.schemas.AskUserQuestionRequestPayload`（:4-7） | plan/交互组 | approval / interaction | C |
| 13 | TaskCreate | task.py:47-61（155） | str = `json.dumps`(TaskResponse dump) | session_id、db、bus（透传 TaskService 发事件） | `task.service.TaskService`、task.schemas | task 四件套 | 无独立 seam：tool_task 包 inject TaskService | A |
| 14 | TaskGet | task.py:71-82 | 同上；不存在时返回中文提示串 | session_id、db | 同上；**绕过 Service 直捅 `service.repo.get`**（task.py:79） | task 四件套 | 同上 | A |
| 15 | TaskUpdate | task.py:119-137 | json.dumps | session_id、db、bus | TaskService | task 四件套 | 同上 | A |
| 16 | TaskList | task.py:147-155 | str：checkbox 文本 `[ ] #id: subject`（非 JSON，:19-31 渲染） | session_id、db | TaskService | task 四件套 | 同上 | A |
| 17 | TeamCreate | team.py:44-60（226） | json.dumps(TeamResponse) | session_id、db | `team.service.TeamService`、team.schemas | team 六件套 | 无独立 seam：tool_team 包 inject TeamService | A |
| 18 | TeamSpawn | team.py:73-92 | json.dumps(member) | session_id、db | TeamService；agent_type 是自由 str 字段（:70） | team 六件套 | 同上 | A |
| 19 | TeamList | team.py:102-110 | json.dumps(列表) | session_id、db | TeamService | team 六件套 | 同上 | A |
| 20 | SendMessage | team.py:143-185 | json.dumps；**错误也走 JSON 对象**（`BROADCAST_TYPE_UNSUPPORTED`，:162-171，全表唯一） | session_id、db | TeamService | team 六件套 | 同上 | A |
| 21 | ReadInbox | team.py:194-205 | json.dumps(列表)；读并清空（副作用在 Service） | session_id、db | TeamService | team 六件套 | 同上 | A |
| 22 | ListMessages | team.py:214-225 | json.dumps(列表) | session_id、db | TeamService | team 六件套 | 同上 | A |
| 23 | Agent | agent.py:67-141（141） | str = json.dumps(SubAgentRunReport：agent/report/error/verdict/verdict_note，:18-35) | **取用最全**：session_id、bus、turn_id、user_id、workspace_id、allowed_tool_names、allowed_skill_names（:77-89） | `tools.subagents.*`；延迟 import **`database.engine.AsyncSessionLocal`（绕过 ctx.db 自开会话，:128-131）** 与 **`runtime.subagent.SubAgentRunner`（工具反向依赖循环实现）** | Agent+subagents 子包 | subagents | C |
| 24 | Skill | skill.py:62-98（174） | str：伪 XML `<skill>` / `<skill_resources>` 包裹（:87-98）；领域错误转中文文案不抛（:77-79） | workspace_id、allowed_skill_names、db | `skill.service.SkillService` | skill 三件套 | skills | B |
| 25 | SkillResource | skill.py:109-131 | str：文本 mime 内联原文，二进制返回占位描述（:126-131） | 同上 | SkillService | skill 三件套 | skills | B |
| 26 | SkillRun | skill.py:144-174 | str：`执行结果:…` + `--- stdout/stderr ---` 分段（:166-174） | 同上 | SkillService→run_script（内部走 core/executor，skill/service.py:307-374） | skill 三件套 | skills（远期 T3 沙箱） | B |

统计口径：使用 `ctx.db` 的 14 个（WritePlan、Task×4、Team×6、Skill×3）；完全不碰 ToolContext 的 11 个；
Agent 是唯一「拿了 ctx 却另开数据库会话」的工具。`ctx.allowed_tool_names` 唯一消费者是 Agent（向下继承边界），
`ctx.allowed_skill_names` 消费者是 Skill 三件套与 Agent（透传）。

### 成组约束：哪些必须一起拆，不能单独成包

1. **plan/交互组（EnterPlanMode、ExitPlanMode、WritePlan、AskUserQuestion）**。前两者与 AskUserQuestion 的
   `run()` 恒抛异常（plan_mode.py:53-54、:65-66；ask_user_question.py:19-20），执行语义完全住在循环里：
   `INTERACTION_TOOL_KINDS` 命中即挂起走 interaction（runtime/agent.py:58-62、:853-901），
   用户应答后由 `_apply_enter_plan_mode` / `_apply_exit_plan_mode` / `_format_question_answers`
   （runtime/agent.py:1312-1442）合成 tool_result 续跑。工具类本体只是「schema 载体」。
   这 4 个工具的拆包完成定义 = approval seam + plan_mode 插件 + interaction 域联动全部就位，
   单独搬文件没有可交付的意义。plan mode 还有 3 处外围特判：工具白名单
   `PLAN_MODE_ALLOWED_TOOLS`（agent.py:63-75、:801、:1446）、非 plan 模式剔除
   `without("ExitPlanMode","WritePlan")`（:1447）、Plan 下 Agent 只许派 Explore/Plan（:837-851）。
2. **team 六件套**。共享模块级序列化 helper（team.py:25-34）与 TeamService；
   TeamCreate/TeamSpawn 出现在 `GLOBAL_SUBAGENT_DENIED_TOOLS`（tools/subagents/definition.py:45-53）。
   teammate 唤醒不在工具里（design 5.2 表指 agent.py:1673-1693 挂 turn/end），
   工具组与唤醒逻辑将分属两个插件、共享 team 域 Service。
3. **task 四件套**。共享 `_task_data` / `_render_task_list`（task.py:14-31）。
4. **skill 三件套**。共享 `_can_access_skill` / `_is_text_mime` / `_human_size`（skill.py:32-52）；
   发现机制在 prompt 域（skills catalog 注入系统提示，prompt/compose.py:28-29），工具描述明写
   「取自系统提示中的可用 skill 清单」（skill.py:59）——skills Consumer 的拆包必须连着
   systemPrompt seam 的 section 注册一起设计。
5. **Agent 与 subagents 子包**。AgentTool 的 description 在 **import 期**烘焙 `routing_catalog()`
   （agent.py:73），SUBAGENT_SPECS 同为 import 期冻结（tools/subagents/registry.py:97）；
   Agent 工具、subagents 四文件（684 行）、runtime/subagent.py 三者一体，
   对应 design 第四节「打破 `AgentType` 封闭 enum」那行。trigger.py 的循环侧调用点未定位（见 Caveats）。
6. **fs 五件套（Read/Write/Edit/Glob/Grep）**。相互独立可各自成包，但共同改造是接 fs seam
   （现状全部直接 `open()` / `Path.glob` 摸盘）；Write/Edit 同时出现在
   `DANGEROUS_TOOLS`（permission/service.py:18）与 `MUTATING_TOOLS`（tools/subagents/trigger.py:15）。
7. **Bash 与 shell_policy**。三档访问级别由 SubAgentSpec 经 ToolContext 下发（tools/base.py:25-28），
   校验在工具内前置执行（bash.py:49-62）；design 已定 shell_policy 降级为 sandboxPolicy
   的一种策略（design.md:238）。

## 二、公共改造：str 返回值的完整下游影响面

「工具返回值从裸 `str` 改结构化」牵动一条完整管道，共 8 处：

1. `BaseTool.run` / `run_with_dict` 签名（tools/base.py:43-53）；入参校验失败返回
   `"error: {err}. hint: ..."` 字符串（:48）——**`error: ` 前缀是散布各处的事实协议**
   （bash.py:62、runtime/subagent.py:321-328 同款），结构化后要给这类「软错误」正式席位。
2. `ToolService.run` 返回 str（tools/service.py:165-278）；同一个 str 交给三处：
   PreToolUse 拦截回写（:224-229）、`tool_repo.succeed(record, output)`（:263）、
   PostToolUse 的 `tool_output`（:266-277，异常路径 :250-262 传 `str(exc)`）。
3. `tool_calls.output_data` JSONB 列（tools/models.py:94）现在存的是 JSON 化的裸字符串；
   结构化后历史行形态不一致，需迁移或读侧兼容（与 implement.md P3-6 的消息形态迁移同类问题）。
4. 主循环成功路径：`_execute_one` → `StreamEvent.tool_result(ToolInfo(output=str))` → SSE
   （runtime/agent.py:969-980；`ToolInfo.output: str | None`，core/events.py:137）。
5. 主循环落库：`ToolResultMessage(output=str).to_content_dict()` → `session_messages.content`
   （runtime/agent.py:981-992；往返序列化定义在 llm/types.py:29-61）。
6. **合成结果路径**：`_record_tool_response`（runtime/agent.py:995-1035）——plan 拒绝文案、
   权限拒绝文案（:920-930）、交互应答合成文本（:1233-1442）全走同一条 str 管道。
   结构化返回值必须同时定义「循环合成的结果」的结构化形态，否则真实执行与合成两条路径分叉。
7. 子代理路径：`{"type": "tool_result", "content": output}` 直接拼进 messages
   （runtime/subagent.py:307-318）。
8. hooks：`HookContext` 的 `tool_output` 收 str（调用侧 tools/service.py:250-277；
   hooks 包内部定义未逐一核对）。

「str 里塞了结构化信息」的程度排序（决定各自的改造收益与成本）：

- **本来就是 JSON 字符串**（去掉 `json.dumps` 即得结构化结果，成本最低）：Task×3（除 TaskList）、
  Team×6、Agent —— 共 10 个；
- **分段文本协议**（退出码 / stdout / stderr 天然是结构）：Bash（bash.py:87-104）、SkillRun（skill.py:166-174）；
- **伪 XML 标记**：Skill（skill.py:87-98）；
- **行内约定**：Grep 的 `file:line:content`（grep.py:50）、Read 的 `{n}\t` 行号（read.py:38-41）、
  Glob 的超限统计前缀（glob.py:32-36）；
- **给模型看的纯文案**（结构化后要决定「渲染」归哪层）：TaskList（task.py:19-31）、Write/Edit 成功文案、
  echo/Weather。

## 三、Q1：`to_param()` 的解耦点

**结论：类型层耦合共 6 处（3 文件），运行时零障碍；没有任何工具在 schema 里用 Anthropic 独有字段。**

`ToolParam` 是 TypedDict——`to_param()` 运行时产出的就是普通 dict
`{name, description, input_schema}`，只是形状恰好等于 Anthropic wire 格式。需要动的位置：

| 位置 | 现状 | 动作 |
|---|---|---|
| tools/base.py:7 | `from anthropic.types import ToolParam` | 删，换中立类型 |
| tools/base.py:36-41 | `to_param() -> ToolParam` | 返回中立表示（dataclass 或 TypedDict） |
| tools/registry.py:3 | 同 import | 删 |
| tools/registry.py:42-43 | `to_anthropic_tools() -> list[ToolParam]` | 连方法名一起中立化 |
| runtime/agent.py:11、:605 | import + 局部标注 `tools: list[ToolParam]` | 改标注 |
| runtime/subagent.py:226 | 调 `to_anthropic_tools()`（无类型标注） | 跟随方法改名 |

**llm 侧已经不用改**：`BaseLLMClient.stream` 契约本就收 `tools: list[dict[str, Any]]`
（llm/base.py:18），anthropic_client 内部自行 cast（llm/anthropic_client.py:60）。
缺的是 OpenAI 翻译层——`OpenAIClient.stream` 目前是 `NotImplementedError`
（llm/openai_client.py:73-83），「Anthropic 平铺 → OpenAI `{"type":"function","function":{...}}`
嵌套」的翻译尚不存在，这正是 design 第四节「由 provider 翻译」要落的位置。

**Anthropic 独有字段核查**（grep 全仓）：

- `cache_control`：只出现在 prompt/compose.py:64、:73 的 system blocks，不在任何工具 schema；
- `strict: True`：只在 llm/anthropic_client.py:128-135 内部的结构化输出工具
  （`emit_structured_output`），不经 `to_param()` 管道，天然属于 provider 内部；
- 无 `type: custom` 等用法。26 个工具的 input_schema 全部出自
  `input_model.model_json_schema()`（tools/base.py:40），是标准 JSON Schema。

两条连带事实：

- `ToolCatalogService.sync_builtins` 把同一份 `model_json_schema()` 落库
  `tools.input_schema`（tools/service.py:42）并经 API 透出（tools/schemas.py:23）。
  中立化只要保持「JSON Schema 本体不变、只换信封类型」，DB 与 API 层零迁移。
- 中立化后的正向收益：将来给 tools 数组尾部加 `cache_control` 断点
  （prompt/compose.py:48-57 注释描述了 tools+system 一起缓存的布局）有了唯一正确落点——
  `llm_anthropic` provider 的翻译层，而非工具包。

## 四、Q2：`ToolRegistry` 升格为 seam 后的形态

**现状**：

- 注册 = import 期静态聚合：tools/registry.py:8-35 静态 import 26 个类，:89-125 硬编码列表实例化。
  6 个调用点各自重建全量注册表：runtime/agent.py:114、:1451；runtime/subagent.py:76；
  tools/service.py:35、:87、:109。
- 查找 = `get(name)` 字典查，缺失抛业务异常（:45-49）；入参校验 = `validate_input`（:51-61）。
- 过滤 = 不可变视图：`without` / `only` / `with_tools` 产新实例（:72-86）。
  **`with_tools` 全仓零调用**（docstring 声称的 teammate 场景不存在，grep 无命中）——死代码候补。
- 同名语义 = **后写覆盖**（dict 推导 :40；`with_tools` docstring 明言「同名以新传入的为准」）。

**升格后，三件事各归其层**：

1. **工具名冲突 → 注册表核心**。`register(caller, tool)` 重名直接抛、effect 卸载即撤销——
   design.md:71-77 已给出参考实现，与 seam 范式「registry 重名 throw」一致
   （research/capability-seams-and-patterns.md:72-77）。现状「覆盖」语义必须显式废弃；
   确需替换时语义应是「dispose 旧注册再挂新的」。
2. **命名空间（T2 MCP 远端工具）→ stub 代理层**。design.md 9.4 定的是「提供工具（命名空间化）」：
   前缀由内核侧受信 stub 插件在注册时施加，注册表只管唯一性、不认识命名空间语法。
   两处落库约束要预先核对：`tools.runtime_name` unique + String(120)（tools/models.py:31-36）、
   `tool_calls.tool_name` String(120)（:88）——命名空间化后的名字不得超长。
3. **按 Agent 白名单过滤 → 会话级视图/组合层，不进注册表核心**。design 7.1 把「工具白名单」列为
   会话级可覆写项。现状三层过滤全是注册表视图上的集合运算：
   - Agent 绑定：DB tool_ids → runtime_names → `base_registry.only(...)`（runtime/agent.py:1449-1474）；
   - plan mode：`only(*PLAN_MODE_ALLOWED_TOOLS)`，且非 plan 模式也要
     `without("ExitPlanMode","WritePlan")`（:1444-1447）；
   - SubAgent：spec 白/黑名单 + 父边界继承（tools/subagents/definition.py:86-96；runtime/subagent.py:76-79）。
   其中 plan mode 一层 design 5.2 已另有去向（plan_mode 插件挂 `tools/pre-execute` waterfall）。
   **行为等价性警告**：现状 plan mode 是缩小发给模型的 tools 数组（agent.py:597、:605），
   waterfall 否决则模型仍看得见工具、只是调用被拒——token 消耗与模型行为都会变，回归判据要覆盖这点。

**第四件事，design / implement 均未提——DB 工具目录**。`sync_builtins` 在启动期同步目录并把
「不在注册表的行」**软删**（main.py:29；tools/service.py:34-45 的 `mark_missing_deleted`；
tools/repository.py:56-70）；Agent 绑定校验用 `build_tool_registry().names` 做存在性检查
（tools/service.py:87、:109）。插件挂卸变成动态之后，「以全量静态注册表为真源」的同步与校验
必须重新定义（以什么为全集、何时同步），否则**少挂一个插件启动一次 = 该插件的目录行被软删**，
存量 Agent 绑定随之 409（tools/service.py:110-123）。

## 五、Q3：最容易做崩的地方（5 处）与 CI 机器化防护

1. **工具名字符串集合散布 6 处，且已有「幽灵名」先例**。
   `INTERACTION_TOOL_KINDS` + `PLAN_MODE_ALLOWED_TOOLS`（runtime/agent.py:58-75）、
   `without("ExitPlanMode","WritePlan")`（:1447）、`DANGEROUS_TOOLS` + `BUILTIN_AGENT_DEFAULTS`
   （permission/service.py:18、:27-29）、`GLOBAL_SUBAGENT_DENIED_TOOLS`
   （tools/subagents/definition.py:45-53）、`_READ_ONLY_TOOLS` + `_WRITE_TOOLS`
   （tools/subagents/registry.py:34-37）、`MUTATING_TOOLS`（tools/subagents/trigger.py:15）。
   frozenset 失配不报错，只会静默放行或漏拦。**trigger.py:15 里的 `MultiEdit`、`NotebookEdit`
   根本不在注册表**——这类漂移已经实际发生。
   防护：CI 断言「每个硬编码工具名集合 ⊆ 挂载后的 registry names」
   （现状就能先抓出这两个幽灵名）。
2. **DB 目录同步的误删通道**（Q2 第四件事）。
   防护：真实 PostgreSQL 集成测试——挂载全部 T0 工具插件跑 `sync_builtins`，
   断言目录恰有 26 行未软删；「缺一个插件」时的预期行为需先在 design 里定义，再写显式断言。
3. **AgentTool 的两条越界依赖**：延迟 import `runtime.subagent.SubAgentRunner`
   （工具→循环的反向依赖，正是 A2 要消灭的形状）与 `database.engine.AsyncSessionLocal`
   （绕过 ctx.db 自开会话，tools/builtin/agent.py:128-131）。拆包时不换成 subagents seam
   的服务调用，import-linter 契约 2 直接红；同时「独立会话、避免与主会话共用连接」的
   事务语义必须在新 provider 里保留。
   防护：import-linter 追加「`tool_*` 包禁止 import `runtime`、`agent_loop`、`database.engine`」。
4. **交互 4 工具的「假 run()」契约**。EnterPlanMode / ExitPlanMode / AskUserQuestion 的 `run()`
   恒抛（plan_mode.py:53-54、:65-66；ask_user_question.py:19-20），真实行为住在循环特判与
   恢复路径（runtime/agent.py:853-901、:1233-1442）。只搬工具类不搬编排 = 插件挂上即
   「一调用就异常」。
   防护：回归网必须含一条「ask → 审批 → 续跑」E2E 用例（P0 基线的一部分）；
   这 4 个工具的完成定义显式绑定 approval seam。
5. **import 期烘焙的模型可见文案**。AgentTool.description 在 import 期拼 `routing_catalog()`
   （agent.py:73）、SPECS 冻结（tools/subagents/registry.py:97）；skills 清单走系统提示注入
   （prompt/compose.py:28-29）。插件动态挂卸后这些「模型看到的清单」不会自动更新，
   需换 systemPrompt seam 的 section 注册。
   防护：26 个工具的 `to_param()` 输出做快照测试（逐字节 diff，同时守住 Q1 的 schema 不变式）；
   插件挂卸后对系统提示 assemble 结果再做一次快照对比。

横切防护（放在 A5 启动断言旁）：**注册名单断言**——组合文件挂载全部 T0 工具插件后
`assert set(ctx.tools.names) == 期望名单`（输出 diff 而非只比数量），防「拆包漏挂」这一
最平凡也最可能的事故。

## 六、Q4：26 个工具按改造成本分档

| 档 | 定义 | 数量 | 名单 |
|---|---|---|---|
| **A 几乎照搬** | 逻辑零改动：包壳 `plugin.py` + 依赖从「工具内构造 `Service(ctx.db)`」改 inject；返回值已是 JSON 字符串，结构化 ≈ 去掉 `json.dumps` | **12** | echo、Weather、TaskCreate、TaskGet、TaskUpdate、TaskList、TeamCreate、TeamSpawn、TeamList、SendMessage、ReadInbox、ListMessages |
| **B 要改返回值或依赖** | 需接新 seam（fs / skills）或返回值需真正重设计（分段文本 / 伪 XML / 行内约定） | **8** | Read、Write、Edit、Glob、Grep（接 fs seam，现状全是直接 `open()`）；Skill、SkillResource、SkillRun（接 skills seam + 系统提示 catalog 联动） |
| **C 要重构** | 依赖关系或执行语义本身要重做 | **6** | Bash（executor 单例→shell seam、shell_policy 降级、分段返回值最深）；Agent（反向依赖循环 + 自开会话 + import 期描述 + AgentType 封闭 enum）；AskUserQuestion、EnterPlanMode、ExitPlanMode、WritePlan（行为在循环特判里，绑定 approval seam） |

成本锚点：A 档的 task 四件套改完即是「函数插件 + inject 域 Service」的教科书样例，
适合当 P3-3 第一个包立范式；C 档的 Agent 是全表最重的单个工具。
TaskList 归 A 的理由：文本渲染（task.py:19-31）可作为结构化结果的 display 字段保留，不动逻辑。

## 七、对 design.md / implement.md 的修正建议

### design.md 第四节 `tools` 行（design.md:228，连带 :247）

1. **「24 个内建工具」改为「26 个」**（registry.py:89-125 实测；`__all__` 交叉验证）。
2. **「拆成独立 `tool_*` 包」改为「按耦合组拆成约 10 个 `tool_*` 包」**：
   plan/交互 4 件、team 6 件、task 4 件、skill 3 件天然同包（共享 helper 与域 Service，见第一节
   成组约束）；按 26 包切会放大 3.2 节「顶层包 22→约 60」的膨胀且拆散事实耦合。
   包数估算相应下修（26 个工具 → 约 10 个 Consumer 包）。
3. **「关键改造动作」列目前只有 to_param 与返回值两件事，至少补三件**：
   - DB 工具目录的真源重定义（`sync_builtins` / `mark_missing_deleted` / 绑定校验，
     tools/service.py:34-45、:87、:109-123）——不写进 design 就没人做，而它是插件化后的
     静默误删通道；
   - 6 处硬编码工具名集合的归属迁移（分散在 runtime / permission / tools.subagents 三处，
     见第五节第 1 条）；
   - 交互 4 工具与 approval seam 的成组交付关系（它们没有独立于 approval 的完成定义）。
4. 顺带：实现期修正 prompt/compose.py:53 注释的「22 个工具」。

### implement.md P3-3（implement.md:114-116，连带 :261 风险表）

1. 数字同步改 26。
2. **单条任务拆成有依赖顺序的子条目**，建议：
   - **P3-3a** `to_param()` 中立化 + 26 工具 schema 快照测试（小、无前置、先行；
     快照同时服务后续所有子步的回归）；
   - **P3-3b** `ToolRegistry` 升格（effect 注册、重名抛错、`with_tools` 死代码清理）
     + DB 目录同步重定义 + 注册名单断言；
   - **P3-3c** A 档 12 个工具搬包（task 四件套打样）；
   - **P3-3d** B 档 8 个（排在 fs / skills seam 之后：fs 五件套依赖 P3 前段的 `fs`，
     skill 组依赖 `skills` 与 `systemPrompt`）；
   - **P3-3e** C 档 6 个：Bash 随 shell seam；Agent 随 subagents seam；
     **交互 4 工具建议移出 P3-3、并入 P3-5 的 approval 或 P4-5 拆循环**——
     其行为在 runtime/agent.py:853-901、:1233-1442 的循环特判里，approval（P3-5）与
     循环拆解（P4-5）未动之前，这 4 个工具在 P3-3 内没有可独立交付的完成定义。
3. 「最容易做崩」三句话替换为第五节的 5 条崩点 + 对应 CI 断言清单
   （注册名单断言、工具名集合 ⊆ 断言、schema 快照、import-linter 追加规则、PG 目录同步测试）。
4. echo 与 Weather 是演示工具（echo 描述自称「用于验证工具链路」，tools/builtin/echo.py:12）：拆包前需显式决策
   「保留为最小插件示例（对应 dsh 的示例包）」还是按 CLAUDE.md 功能删除纪律移除，不要默认搬运。

## 非工具文件的去向（拆包时同样要有归属）

| 现文件 | 行数 | 去向 |
|---|---|---|
| tools/base.py（ToolContext + BaseTool） | 53 | tools Definition 包；ToolContext 里 shell 专属字段（shell_access / shell_tmp_root，:25-28）应随 shell seam 迁出，db 字段与 5.7「服务持有 sessionmaker」规则的关系需在 Definition 设计时定 |
| tools/registry.py | 125 | tools Definition 包（升格后的注册表本体）；`build_tool_registry()` 消亡，装配移入组合文件 |
| tools/service.py | 278 | 拆两半：ToolService.run 的执行编排（hooks、tool_calls 状态机）→ `tools/execute` 事件链路；ToolCatalogService → tools 域保留（DB 目录 + API 支撑），真源关系重定义（Q2） |
| tools/repository.py + models.py + api.py + schemas.py | 380 | tools 域五件套原地保留（表所有权不变） |
| tools/shell_policy.py | 355 | shell seam：降级为 sandboxPolicy 的一种策略（design.md:238） |
| tools/subagents/ 4 文件 | 684 | subagents seam：definition/registry 升格为 Definition + Provider，trigger 的归属需单独定（其消费者在循环侧，未定位） |

## Caveats / 未逐一核对

- runtime/agent.py 共 1712 行，只精读与工具相关区段（:41-75、:596-620、:790-940、:941-1035、
  :1098-1142、:1290-1474）；teammate 唤醒（:1673-1693）未读，引用自 design 5.2 表。
- hooks/ 包内部（HookContext 字段、HookRegistry 单例实现）未读；
  `tool_output` 为 str 的结论来自调用侧（tools/service.py:196-277）。
- tools/subagents/prompts.py（232 行）只读头部，确认为纯提示词常量与可解析契约声明
  （VERDICT 锚点、关键文件清单等由 registry.py:108-181 解析）。
- session/service.py 的 add_message 落库细节与 load_context_for_turn 对 tool_result 的重组未读；
  ToolResultMessage 往返序列化以 llm/types.py:29-61 为准。
- trigger.py 的循环侧调用点（谁调 assess / compose_hint）未定位。
- 各工具「行数」为类定义行区间，多工具文件的共享 helper 行未分摊到单个工具。
- 「24」何时开始失真未考证 git 历史。
- skill/service.py:307-374 的 run_script 细节未重读，行号引用自 design.md:10.3。
