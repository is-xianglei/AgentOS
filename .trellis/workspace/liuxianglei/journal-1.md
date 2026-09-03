# Journal - liuxianglei (Part 1)

> AI development session journal
> Started: 2026-08-14

---



## Session 1: 统一大模型调用入口：Anthropic/OpenAI 双 provider 抽象

**Date**: 2026-08-16
**Task**: 统一大模型调用入口：Anthropic/OpenAI 双 provider 抽象
**Branch**: `feature/unify-llm-client`

### Summary

将 llm/client.py 拆为 BaseLLMClient 抽象与 Anthropic/OpenAI 双实现，所有调用方经 create_llm_client(scenario=...) 工厂创建；模型按「场景覆盖 > provider 主模型」两级解析，七场景可分别覆盖。provider/api_key/场景名/模型任一缺失或非法均抛中文 AgentException，无静默兜底，main.py lifespan 加启动自检。session_messages 新增 provider/model 成本归因列（迁移 0021_message_llm_attribution，不回填）。遗留三项：(1) AC4 测试项未完成——tests/ 已在 e872cdc 整体删除，本任务未补任何测试，待办契约见 .trellis/spec/backend/llm-provider-guidelines.md 第 6 节，仅用一次性脚本手工验证过双 provider 真实链路；(2) 本分支 0021_message_llm_attribution 与 multi 分支 0021_message_branch 同挂 0020_agent_mvp_comments，合并必然双头，已定本分支先合、由 multi rebase，记录在 design.md；(3) .gitignore:63 的裸 tasks/ 规则匹配任意深度，导致 .trellis/tasks/ 未能入库，任务文档目前仅存本地。

### Git Commits

| Hash | Message |
|------|---------|
| `168f691` | (see git log) |
| `17303c9` | (see git log) |

### Status

[OK] **Completed**


## Session 2: Provider 流式协议翻译抽象

**Date**: 2026-08-17
**Task**: Provider 流式协议翻译抽象
**Branch**: `feature/unify-llm-client`

### Summary

把 Anthropic 专属的流翻译、工具与 system prompt 编码收口到 BaseLLMClient，runtime 层完全 provider 无关。新增中立类型 LLMStreamEvent/LLMTurnResult/ToolSpec/PromptBlock 与 runtime/stream_assembler.py 装配器；入站流式适配下沉为各家 Client 私有；出站 to_specs()/compose_lead_blocks() 去 Anthropic 化，编码下沉到客户端。R4 语义变更：block index 改为 turn 内递增、tool_use 块补发 block_stop、入参 JSON 非法改抛中文 AgentException。建 tests/ 骨架共 27 例。真机冒烟验证 index 递增、start/stop 配对、缓存命中(读取量精确等于前次写入量)、挂起-恢复均正常。

### Git Commits

| Hash | Message |
|------|---------|
| `0ffbabf` | (see git log) |
| `35ac33f` | (see git log) |
| `38335a4` | (see git log) |
| `bd6b9be` | (see git log) |
| `afdbd41` | (see git log) |
| `7a37a8f` | (see git log) |
| `b892c0d` | (see git log) |

### Status

[OK] **Completed**


## Session 3: Workflow 编排引擎：顺序/并行/智能路由三模式落地

**Date**: 2026-09-03
**Task**: Workflow 编排引擎：顺序/并行/智能路由三模式落地
**Branch**: `feature/unify-llm-client`

### Summary

新增 workflow 域与迁移 0022、runtime/workflow.py 三种编排器、发消息接口 workflow_id 分发（含会话锁定防降级）、SubAgentSpec display_name/runtime_name 桥接、workflow LLM 场景四处同步；独立审查修复 Parallel 配置期失败静默跳过（AC7）等 6 项；146 个测试通过；沉淀 code-spec workflow-orchestration.md

### Git Commits

| Hash | Message |
|------|---------|
| `77a4e77` | (see git log) |

### Status

[OK] **Completed**
