# 实施计划：思考块进中立协议与落库

## 执行顺序

协议先行 → 两家客户端并行补齐 → 开关配置 → 测试基线改写 → 端到端。
每步可独立验证；开关默认关，因此前四步落地后线上行为零变化，回退面小。

### 步骤 0：基线留存

```bash
uv run pytest 2>&1 | tail -20
uv run ruff check . 2>&1 | tail -5
```

**改造前必须先跑**，避免把既有失败算到本次头上。

### 步骤 1：中立协议（R1、R2）

文件：`llm/types.py`

1. 新增 `ThinkingSegment`（流式载体）与 `ThinkingBlock`（持久化形状），
   字段均为 `text` / `signature` / `redacted_data`；
2. `_KIND_THINKING = "thinking"`；`AssistantBlock` 联合扩为三类；
3. `parse_assistant_blocks()` 增加 thinking 分支，未知 kind 仍抛中文异常；
4. `build_assistant_blocks()` 增加思考段入参，**思考块保序且排在最前**；
5. `LLMTurnResult` 增加 `thinking: list[ThinkingSegment]`，默认空列表；
6. 改写 docstring 中「块间顺序不承载语义」的表述，限定为文本与工具块之间。

⚠️ 第 4 步改签名会波及四处生产调用点，见步骤 3。

验证：

```bash
uv run pytest tests/test_message_neutral.py tests/test_neutral_context.py -v
```

此时应**全绿**（新增字段有默认值，旧行为不变）。若红，说明改动破坏了兼容性。

### 步骤 2：装配器与调用点（R2）

- `runtime/stream_assembler.py`：**不加累积逻辑**，仅确认 `THINKING_DELTA`
  转发路径不变（`:106-107`）；终局思考段随 `_last_result` 自动带出。
- 四处 `build_assistant_blocks` 调用点传入 `result.thinking`：

| 序 | 位置 |
|---|---|
| 2.1 | `runtime/agent.py:697` |
| 2.2 | `runtime/agent.py:1735` |
| 2.3 | `runtime/subagent.py:394` |
| 2.4 | `runtime/subagent.py:635` |

### 步骤 3：OpenAI 客户端（R3）

文件：`llm/openai_client.py`

1. `_OpenAIChunkAdapter.adapt()` 读 `delta.reasoning_content` → `THINKING_DELTA`；
   思考块用独立 `block_ref`，**勿与 `_TEXT_REF = 0`、工具块 `1 + index` 撞号**；
2. 适配器累积思考文本，`finalize()` 写入终局思考段（签名 `None`）；
3. `_encode_messages()` 的 assistant 分支拼 `reasoning_content` 字段；
4. 改写 `:110-113` 的过时注释。

### 步骤 4：Anthropic 客户端（R4）

文件：`llm/anthropic_client.py`

1. `_BLOCK_TYPE_MAP` 登记 `redacted_thinking` → `BlockType.THINKING`（消除 F10）；
2. 新增 `_extract_thinking_blocks(content)`，与 `_text_from_content` /
   `_extract_tool_uses` 同构，从终局 content 提取带签名的思考块；
3. `_on_message_final()` 填充 `LLMTurnResult.thinking`；
4. `_encode_assistant_block()` 支持 `ThinkingBlock` 三分支
   （redacted / 带签名 / 无签名跳过），**遍历顺序即落库顺序，不重排**。

### 步骤 5：开关配置（R5）

⚠️ `Settings.model_config` 为 `extra="forbid"`——三处漏改任一处启动即失败。

| 键 | 代码默认 |
|---|---|
| `AGENTOS_OPENAI_THINKING_ENABLED` | `False` |
| `AGENTOS_OPENAI_REASONING_EFFORT` | `None` |
| `AGENTOS_ANTHROPIC_THINKING_ENABLED` | `False` |
| `AGENTOS_ANTHROPIC_THINKING_BUDGET_TOKENS` | `4096` |

1. `core/config.py` 声明四个字段；
2. `.env` 写本地实际值、`.env.example` 写安全占位（**不得写真实密钥**）；
3. 两家 `stream()` 按开关注入参数，关闭时参数**完全不出现**；
4. `budget_tokens` 校验 `1024 <= budget < llm_default_max_tokens`，越界抛中文异常。

验证：

```bash
uv run python -c "from core.config import settings; print(settings.openai_thinking_enabled)"
```

### 步骤 6：测试改写与新增（AC1-AC10）

**推翻既有基线时逐条改写，保留原断言意图，不整体删除用例：**

| 文件 | 动作 |
|---|---|
| `tests/test_llm_openai_client.py:198` | `test_no_thinking_delta_emitted` 反转为「开关开启时产出」 |
| `tests/test_stream_baseline.py:115` | signature 噪声断言 → 终局思考段带签名 |
| `tests/fixtures/anthropic_chunks.py` | 补 `redacted_thinking` 块序列 |
| `tests/test_message_neutral.py` | 补 thinking 块的构造/解析/保序用例 |

新增覆盖：AC2（保序 + 签名不串位）、AC6（redacted 往返）、
AC8（开关关闭时参数逐键一致）、AC9（budget 越界抛错）、AC10（无签名块跳过）。

### 步骤 7：端到端（AC11，硬门槛）

`.env` 置 `AGENTOS_OPENAI_THINKING_ENABLED=true`，发起一次**触发工具调用**的提问：

1. SSE 流出现 thinking 事件；
2. **第二轮请求不报 400** —— 这是 F1 回传约束的直接验证，不可省略；
3. 验证后按需决定 `.env` 是否保留开启。

## 验证命令

```bash
uv run pytest                                      # 全量回归
uv run pytest tests/test_llm_openai_client.py tests/test_stream_baseline.py -v
uv run ruff check .                                # 行宽 100、Python 3.14
uv run python -c "from core.config import settings; print(settings.model_dump())" >/dev/null
```

## 风险文件与回滚点

| 文件 | 风险 |
|---|---|
| `llm/types.py` | 协议地基，`build_assistant_blocks` 签名波及 4 处调用点。步骤 1 单独验证后再往下 |
| `llm/anthropic_client.py` | 新增最多（4 处），且 `_encode_assistant_block` 顺序错误直接触发 400 |
| `llm/openai_client.py` | `block_ref` 撞号会让装配器错配块边界 |
| `core/config.py` + `.env` + `.env.example` | `extra="forbid"`，三处必须同步 |
| `tests/fixtures/anthropic_chunks.py` | 多个测试共用，改坏会连带误伤 baseline 用例 |

**回滚陷阱**：`git revert` 代码后，`.env` 中残留的四个新键会因
`extra="forbid"` 导致启动失败——**回滚时必须同步删除这些键**。

落库数据无需回滚：思考块是新增 kind，旧代码的 `parse_assistant_blocks()`
遇到会抛中文异常而非静默误读，但**已写入的思考块会让回滚后的旧代码读不了新消息**。
若需回滚且库中已有思考块，须一并清理或接受该会话不可续。

## 开工前检查

- [ ] 步骤 0 基线测试结果已留存
- [ ] 确认 `.env` 的 DeepSeek 端点可连通（步骤 7 需要）
- [ ] 确认无 Alembic 迁移需求（PRD F3：`content` 为无 schema JSON）
- [ ] 确认 Phase 3 需回写 `.trellis/spec/backend/llm-provider-guidelines.md` 3.7 节
      （「两类块」→「三类块」）
