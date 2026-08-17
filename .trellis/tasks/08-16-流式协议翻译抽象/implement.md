# 执行计划：Provider 流式协议翻译抽象

对应 `prd.md` / `design.md`。类型定义与方法签名的唯一事实源是 `design.md`，
本文只排执行顺序、验证命令与门禁，不复制代码。

## 全局约束

1. 一个 Phase 一个 commit（**不 push**），即一个回滚点；Phase 内部的中间步骤
   不单独提交，因为中间态不可运行。
2. 测试挂了先查实现，**不改断言迁就实现**；确需改断言的，在 commit message
   里写清原断言为什么是错的。
3. **不引入新第三方依赖**：异步测试用 `.venv` 里已有的 anyio pytest 插件
   （已核实 `anyio.pytest_plugin` 已注册为 `pytest11` 入口点），不装 pytest-asyncio。
4. 所有异常路径抛中文 `AgentException`，包装底层异常用 `raise ... from exc`。
5. `design.md` 第十四节两项已拍板：`continuation_payload` 的死字段 `actor`
   本任务顺带清理（Phase 3.1）；`ToolUse` 留在 `llm/types.py` 不迁移。

---

## Phase 0：测试基线（不改任何生产代码）

本任务是行为等价重构，没有基线就无法证明「改完和改前一样」，而仓库当前
连 `tests/` 目录都没有。此期结束前**不允许动 `llm/` 与 `runtime/`**。

### 0.1 建测试骨架

- 新建 `tests/conftest.py`，提供 `anyio_backend` fixture 返回 `"asyncio"`；
  异步用例统一 `pytestmark = pytest.mark.anyio`。
- `pyproject.toml` 的 `[tool.pytest.ini_options]` 已有 `testpaths = ["tests"]`
  与 `pythonpath = ["."]`，无需改动，也不要新增依赖。
- 验证：`uv run pytest tests -q` 能正常收集（此时 0 用例，退出码 5 属正常）。

### 0.2 录制 Anthropic chunk 基线 fixture

- 新建 `tests/fixtures/anthropic_chunks.py`，手写三组 chunk 序列，形状以
  `llm/anthropic_client.py:38-90` 实际 yield 的结构为准
  （`event.model_dump(mode="json")` + 合成的 `message_final`）：
  - a. 单轮纯文本；
  - b. text 块 + tool_use 块（入参分多次 `input_json_delta` 下发）；
  - c. thinking 与 text 交错多块。
- **手写而非真实调用录制**：避免联网、避免 API Key 进仓库。

### 0.3 固化当前事件序列

- 新建 `tests/test_stream_baseline.py`：用 0.2 的序列驱动**现有**
  `AnthropicStreamTranslator`，断言 `[e.to_dict() for e in events]` 的完整序列。
- 必须断言到现状的这三点——它们正是 Phase 1 要主动改掉的语义，
  基线里断言清楚，改造时才看得见差异：
  - block index 每次调用从 0 重来；
  - tool_use 块**不发** `block_stop`；
  - tool_use 入参 JSON 非法时兜底成 `{}`。

**门禁 G0**：`uv run pytest tests -v` 全绿，三组场景各有基线；
`git diff` 不含 `llm/`、`runtime/` 任何改动。

---

## Phase 1：入站中立化（R1 + R2 + R4）

> **签名分两期改**（本计划最关键的一条）：Phase 1 只改 `stream()` 的**返回类型**，
> 入参 `system_prompt` / `tools` 保持现状的 `str | list[dict]` 形状，
> Phase 2 才换成 `PromptBlock` / `ToolSpec`。这样每期结束系统都能跑起来；
> 一次性改完则中间没有可运行状态，出问题无法二分定位。

### 1.1 新增中立类型

`llm/types.py` 加 `LLMEventKind`、`LLMStreamEvent`、`LLMTurnResult`
（design 第三节），以及 `ToolSpec`、`PromptBlock`（design 第四节，
本期只定义不接线，Phase 2 用）。

### 1.2 新建装配器并先测后接

- 新建 `runtime/stream_assembler.py::TurnEventAssembler`（design 第六节），
  类级 docstring 写全 R4.1 index 分配与 R4.2 start/stop 配对规则。
- 新建 `tests/test_stream_assembler.py`，覆盖 design 第十一节表格的**全部**情况
  （含 turn 内多次 `consume()` 后 index 不重复、缺 `TURN_RESULT` 抛错、
  未 `consume` 就读 `last_result` 抛错）。用手写 `LLMStreamEvent` 驱动，不碰网络。
- 此步产物可独立跑通，先绿了再往下接。

### 1.3 迁移 Anthropic 适配器

- `AnthropicStreamTranslator`（`llm/types.py:118-226`）迁入
  `llm/anthropic_client.py`，改名 `_AnthropicChunkAdapter`，改为产出
  `LLMStreamEvent`，构造器不再收 `actor` / `session_id`（design 第七节）。
- `chunk_type` / `chunk_text` / `extract_tool_uses` / `text_from_content`
  一并迁入并加 `_` 前缀，`llm/types.py` 不再导出。
- **修掉静默兜底**：tool_use 入参 JSON 解析失败改抛中文 `AgentException`
  （附工具名与 tool_use_id），不再兜成 `{}`。

### 1.4 客户端产出中立事件

- `AnthropicClient.stream()` 返回 `AsyncIterator[LLMStreamEvent]`：SDK 事件不再
  `model_dump()` 原样 yield，改喂适配器；流末尾产出唯一的 `TURN_RESULT`，
  其中 `provider_content` 带上 `message_final` 的 `content`（design 第十节）。
  `_log_cache_usage` 不动。
- `OpenAIClient.stream()` **只改返回类型注解**，方法体维持
  `NotImplementedError`（`openai_client.py:87`）与其后不可达的 `yield`。
- `llm/base.py` 同步返回类型注解与 docstring（删掉里面按 provider 罗列
  原始事件格式的那段——中立化后不再存在「原始事件」这一层）。

### 1.5 runtime 三处改造

按 design 9.1 / 9.2 / 9.3 逐个改：

- `runtime/agent.py`：`_run_llm_loop` 与 `_finalize_without_tools` 换装配器，
  终局信息改读 `assembler.last_result`；`:568-569` 那两行失实注释一并改写。
  **别漏 `:702` 与 `:706`**——`translator.stop_reason` / `translator.last_usage`
  两处属性读取，漏改会直接 `AttributeError`（design 9.1 表格）。
- `runtime/subagent.py`：`:131` / `:443` 两处换装配器，
  两个 ReAct 循环方法的 `translator` 参数（`:341`、`:559`）同步改名换型，
  `_stream_and_translate` → `_stream_and_collect` 返回 `LLMTurnResult`，
  `:367,369,372,587,592` 五处解析调用随之简化。
- `runtime/compact.py`：不建装配器，直接挑 `TURN_RESULT` 取 `result.text`；
  `MessageParam` / `chunk_type` / `text_from_content` 三个导入删除。

### 1.6 迁移基线断言

把 0.3 的基线按 R4 新语义改断言，并在 commit message 写明改动理由：

- index 由「每次调用从 0」改为「turn 内递增」，新增多轮 tool use 下
  index 不重复的断言；
- tool_use 块新增 `block_stop` 断言；
- 入参 JSON 非法由「兜底 `{}`」改为「抛 `AgentException`」。

**门禁 G1**：

```bash
uv run pytest tests -v
rg -n "message_final|extract_tool_uses|text_from_content|AnthropicStreamTranslator|translator" runtime/
```

后者应无业务命中。再做一次冒烟：按 `.env` 现有 DB/Redis 起服务，用默认账号
登录，发一条会触发工具调用的消息，观察 SSE 中 index 全程递增、
`block_start` / `block_stop` 成对。

---

## Phase 2：出站中立化（R3）

### 2.1 工具描述中立化

`tools/base.py:36` `to_param()` → `to_spec()` 返回 `ToolSpec`；
`tools/registry.py:42` `to_anthropic_tools()` → `to_specs()`；
两个文件都删掉 `anthropic.types` 导入。

### 2.2 system prompt 中立化

`prompt/compose.py:48` `compose_lead_blocks()` 返回 `list[PromptBlock]`，
两个缓存断点原样保留（稳定段、易失段各一个）；docstring 里属于 Anthropic
缓存行为的说明（渲染顺序、5m TTL）随编码逻辑搬去客户端。

### 2.3 编码下沉到客户端

- `AnthropicClient._encode_system` / `_encode_tools`：`PromptBlock` →
  `TextBlockParam`（`cache_breakpoint=True` 落成
  `cache_control={"type": "ephemeral"}`）、`ToolSpec` → `ToolParam`；
  `str` 形态的 system 在此归一化。
- `OpenAIClient._encode_system` / `_encode_tools` **本期就实现**并单测。
  它们不依赖流式，是中立类型是否真够两家用的提前验证——编不出来说明
  `PromptBlock` / `ToolSpec` 设计有缺陷，此刻发现成本最低。
- 新建 `tests/test_encoders.py` 断言两家编码结果形状。

### 2.4 打通签名与调用方

- `llm/base.py` 的 `stream()` 入参改 `str | list[PromptBlock] | None`
  与 `list[ToolSpec] | None`（参数名 `system_prompt` 保持不变，见 design 第五节）。
- `runtime/agent.py:11` 删除 `from anthropic.types import TextBlockParam, ToolParam`；
  `:590` / `:610` 的类型注解与 `_finalize_without_tools` 的 `system_prompt`
  参数类型同步换成中立类型。
- **别漏 `:603` 的 plan mode 追加块**——`compose_lead_blocks()` 的返回值紧接着
  被 `append()` 了一个字面量 dict，改返回类型后这里必须换成 `PromptBlock`，
  且 `cache_breakpoint` 保持 `False`（design 9.1 表格）。漏改会让 Anthropic
  收到混形状的 system 列表。

**门禁 G2**：

```bash
uv run pytest tests -v
rg -n "anthropic" runtime/ tools/ prompt/
```

后者应只剩注释说明。冒烟同 G1，另需确认 `_log_cache_usage` 仍打印到
cache 命中（`cache_read_input_tokens` 非 0），证明缓存断点没在编码时丢失。

---

## Phase 3：清理与交付

### 3.1 清理 continuation_payload 死字段（design 14.1）

与流式改造彼此独立，可在 Phase 1 之后任意时点做，但**单独一个 commit**——
它不是流式收口的一部分，混进去会让 revert 粒度失真。

- 删 `runtime/agent.py:1071` 的 `"actor": actor.to_dict_compact(),`。
- 删 `_suspend_for_interaction` 的 `actor: Actor` 参数（`:1051`）——
  删掉上一行后它在方法内已无使用者。
- 删两处调用点的实参：`:903`、`:921`。
- **不写 Alembic 迁移**：这是 JSONB 里的 key，不是表列；历史行残留的
  `actor` key 无人读取，旧暂停点在新代码下照常恢复。

验证：

```bash
rg -n '"actor"' runtime/ interaction/     # 应无命中
rg -n "to_dict_compact"                   # 只应剩 core/events.py 的定义与 :342 自用
```

再冒烟一次挂起-恢复：触发一个需审批的工具，暂停后应答，确认能正常续跑。

### 3.2 死代码确认

```bash
rg -n "to_param|to_anthropic_tools|chunk_type|chunk_text|StreamTranslator|create_translator"
```

确认旧名称已无有效引用；`llm/__init__.py` 导出同步更新，
`llm/types.py` 不再残留 Anthropic 专用解析函数。

### 3.3 spec 与文档

- `.trellis/spec/backend/llm-provider-guidelines.md` 补两条约定：
  入站流式适配归各家 Client 私有；自有协议的装配职责（index 分配、块边界）
  归 runtime 侧装配器，不写成各 provider 需遵守的 docstring 契约。
- `CLAUDE.md`「多 Provider LLM 配置」第 10 条复核：OpenAI 流式本期仍未实现，
  原措辞继续成立，**不改**。

### 3.4 提交

commit 粒度：Phase 0 / Phase 1 / Phase 2 各一个，
死字段清理（3.1）单独一个，共 4 个。**不 push**。

---

## 命令速查

```bash
uv run pytest tests -v                          # 全量
uv run pytest tests/test_stream_assembler.py -v # 装配器单测
uvx ruff check llm/ runtime/ tools/ prompt/ tests/
uvx ruff format --check llm/ runtime/ tools/ prompt/ tests/
```

**lint 用 `uvx` 而非 `uv run`**：ruff 不在 `dependencies` 里，`.venv/bin/ruff`
不存在，`uv run ruff` 会报 `Failed to spawn: ruff`。`uvx` 临时取用，
`pyproject.toml` 的 `[tool.ruff]`（line-length 100、target py314）照常生效。
本任务不动依赖清单——把 ruff 加进 dev 依赖属无关变更，要做应另开提交。

## 回滚点

| 门禁 | 回滚方式 | 影响面 |
| --- | --- | --- |
| G0 | 删 `tests/` | 无生产代码改动 |
| G1 | `git revert` 该 commit | 回到 Anthropic 直连流式 |
| G2 | `git revert` 该 commit | 保留入站中立化，仅出站回退 |

数据库无影响：本任务不改表结构、不改消息落库格式
（`provider_content` 保证落库内容与改造前逐字节一致，见 design 第十节）。




