# 执行计划：OpenAI 流式实现 + 消息体中立化

> 设计依据见 `design.md`。本文件只管顺序、验证与回滚点。

## 排序原则

先立中立协议与出站翻译（读写两端都依赖它），再改各调用点，
最后实现 `stream()`。这样每个阶段结束时仓库都能跑通测试——
若先实现 `stream()`，中立化未完成期间它无法被端到端验证，
反而要写一次性的临时断言。

**每个阶段结束都是一个可回滚点**：`uv run pytest` 必须通过才能进入下一阶段。

---

## 阶段 1：中立消息协议与出站翻译

1. 在 `llm/types.py` 定义 assistant 中立块结构（`kind: "text" | "tool_use"`），
   提供构造与解析辅助；删除 `provider_content` 字段及其 3 行注释。
2. 在 `llm/anthropic_client.py` 新增 `_encode_messages()`（中立 → Anthropic wire），
   处理 `tool_use` 块与 `tool_result` 块（含 `is_error` 键），
   并把连续 tool_result 合并进同一条 user 消息
   （搬运 `session/service.py:512-520` 的现有合并逻辑）。
3. 在 `llm/openai_client.py` 新增 `_encode_messages()`（中立 → OpenAI wire）：
   assistant 的 tool_use 编成 `tool_calls` 数组，
   tool 结果编成 `role="tool"` 独立消息（**不是** Anthropic 那样塞进 user 消息）。
4. 在 `BaseLLMClient.stream()` 的 docstring 中写明契约变更：
   messages 入参为**中立形状**，翻译由各 Client 内部完成（design §6.4）。
5. 新增 `tests/test_message_neutral.py`：往返等价（AC5）+ 完整会话双向编码（AC6）。

验证：`uv run pytest tests/test_message_neutral.py -v`

> 注意两家的结构性差异：Anthropic 把 tool_result 作为块塞进 `role="user"` 消息
> 并合并连续块（`session/service.py:512-520` 的现有逻辑）；
> OpenAI 每个工具结果是一条独立的 `role="tool"` 消息，带 `tool_call_id`。
> 这个差异正是"出站翻译"必须存在的理由，不能靠共用一套形状糊过去。

---

## 阶段 2：写路径改造（落库改存中立形状）

6. `runtime/agent.py:642`、`:1623`：两处 assistant 落库改存中立块
   （从 `result.text` 与 `result.tool_uses` 组装，不再读 `provider_content`）。
7. `runtime/agent.py` 的 tool 消息落库：确认 `ToolResultMessage.to_content_dict()`
   路径不变（`:988` / `:1031`），本阶段不动。
8. `runtime/subagent.py:308-318`：自建 tool_result 改产中立形状。
9. `runtime/subagent.py:362`、`:582`：内存态 assistant 消息改用中立块；
   `:582` 那条同时影响 `team_members.history` 落库（`:480`），
   须确认 `_load_history()`（`:503`）读回后的形状一致。

验证：`uv run pytest`（此时读路径尚未改，全量应仍通过——
写路径改动不影响现有 27 个用例，它们都不碰落库）

> 本阶段结束时库里存的已是中立形状，但 `_messages_to_context` 仍在做旧的
> Anthropic 编码，两端形状对不上，**运行必然报错**。这是预期的中间态，
> 由阶段 3 收口。不要在此停留或试图让它"先能跑"。

---

## 阶段 3：读路径改造

10. `session/service.py:503-522` `_messages_to_context()`：**删除**出站编码，
    改为原样产出中立消息（tool 消息保持 `ToolResultMessage` 中立 dict，
    不再编成 `tool_result` 块，也不再做连续块合并——该逻辑已搬到
    Anthropic 的 `_encode_messages()`）。
11. `runtime/compact.py:78,93,103`：`_micro_compact` 改读中立字段名。
    此时它遍历的已是中立形状，语义不再随 provider 变。
12. 确认快照落库为中立形状：`compact.py:47-55` 的 `add_snapshot(compacted)`
    入参此时应已是中立消息（design §6.4 的核心目标）。
13. `tools/subagents/trigger.py:54-62`：`_iter_tool_uses()` 改按 `kind` 匹配。
    **这是静默失效点**，改完必须验证 hook 仍能触发。
14. `memory/jobs/extraction.py:620-628`：assistant 分支改按 `kind == "text"` 提取。
    **同为静默失效点**，改完必须验证抽取拿到非空文本。

验证：`uv run pytest`

> 第 12、13 项若遗漏，测试**不会失败**——这两条路径当前零覆盖，
> 且失效形式是静默返回空值而非抛错。必须逐项人工确认，
> 或为其补最小覆盖测试。

---

## 阶段 4：实现 OpenAI 流式

15. 在 `llm/openai_client.py` 实现 `_OpenAIChunkAdapter`（design §4）：
    block_ref 分配、事件产出、收束、usage 映射、错误处理。
16. 实现 `stream()`（design §5）：`create(stream=True)` 裸 chunk 路径，
    传 `stream_options={"include_usage": True}`，`timeout` 走 `NOT_GIVEN`。
17. 新增 `tests/fixtures/openai_chunks.py`：手写裸 chunk 序列
    （纯文本 / 文本+工具 / 多工具 / 坏 JSON / 末 chunk 带 usage），
    对标 `anthropic_chunks.py` 的写法，刻意掺入噪声 chunk。
18. 新增 `tests/test_llm_openai_client.py`：适配器事件序列、
    坏 JSON 抛中文异常（AC2）、usage 与 stop_reason（AC3）。
19. 删除 `test_stream_baseline.py:168` 的 `provider_content` 断言。

验证：
```bash
uv run pytest tests/test_llm_openai_client.py -v
uv run pytest
```

---

## 阶段 5：全量验证

20. 全仓库搜索确认无残留：
    ```bash
    grep -rn "provider_content" --include="*.py" . | grep -v "\.venv"   # 应无输出（AC4）
    grep -rn '"tool_result"\|"tool_use"' --include="*.py" \
      runtime/ session/ tools/ memory/ | grep -v "\.venv"
    # 剩余命中应仅为项目自有协议枚举（core/events.py、llm/types.py 的 LLMEventKind）
    ```
21. 确认 `runtime/` / `tools/` / `prompt/` 对 `anthropic` 零引用（AC / spec §3.6）：
    ```bash
    grep -rn "anthropic" --include="*.py" runtime/ tools/ prompt/ | grep -v "\.venv"
    ```
22. `uv run pytest` 全量通过（AC8，基线 27 → 预期显著增加）。

---

## 阶段 6：真实网关验证（AC10）

23. 确认 `.env` 当前配置项取值（不打印密钥）：
    ```bash
    grep -E "^AGENTOS_(LLM_PROVIDER|OPENAI_BASE_URL|OPENAI_MODEL)=" .env
    ```
24. 以 `AGENTOS_LLM_PROVIDER=openai` 实跑一次纯文本流式、一次带工具流式。

> **验收纪律**：`.env` 配的是第三方网关 `https://4sapi.org/v1`，
> 模型 `gpt-5.6-sol` 非 OpenAI 原生模型名。必须把
> 「SDK 契约已满足」与「该网关实际支持」分开判定并分别报告。
> 上一个任务曾在此网关撞到瞬时空响应（不可复现，非代码缺陷）。
> 网关能力不足**不构成**本任务的实现缺陷，但必须如实报告具体错误，
> 不得含糊带过，也不得据此声称验证通过。

---

## 阶段 7：spec 同步（Phase 3.3）

25. 更新 `.trellis/spec/backend/llm-provider-guidelines.md`：
    - §3.4 能力边界：删除「`OpenAIClient` 仅实现 `complete_structured()`；
      `stream()` 抛 `NotImplementedError`」与「依赖流式的场景当前只能使用 Anthropic」。
    - §6.2 测试清单：删除「消费 `client.stream(...)` 时断言抛 `NotImplementedError`」，
      改为实际的流式测试要求。
    - §3.6 流式分层表：补充 OpenAI 侧的对应实现与
      「`.stream()` helper 因 strict 校验不可用」这一决策记录（design §2.1/§3）。
    - 新增消息体中立化的协议说明与 `kind` 判别键的理由。

## 阶段 8：提交（Phase 3.4）

26. 按阶段分批提交，不混作一个大提交。**不执行 `git push`**。

---

## 回滚点

| 阶段 | 回滚代价 |
|---|---|
| 1 结束 | 仅新增协议与翻译层，无调用点改动，直接 revert |
| 2 结束 | 写路径已改但读路径未改——**此状态不可用于运行**，必须继续到阶段 3 |
| 3 结束 | 中立化完整，Anthropic 走新翻译路径且行为等价，OpenAI 仍不可流式。可作为稳定回滚点——此状态本身是一次有价值的独立交付（库中不再有 provider 形状） |
| 4 结束 | 功能完整 |

阶段 2 与 3 之间是**唯一的不可运行窗口**，中途中断需回滚到阶段 1 结束状态。
