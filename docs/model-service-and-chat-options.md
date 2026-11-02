# 模型服务与对话选项

本文对应从 HugAgentOS 迁入 AgentOS 和 console 的模型服务、标准/极速模式、模型及思考强度选择。

## 模型服务与权限

模型供应商和角色按工作区隔离。管理员维护供应商、执行连通测试、探测元数据和指派角色；普通用户可读取聊天能力与维护个人默认模型。返回的密钥始终掩码，更新时留空保留原密钥，掩码不能作为新密钥保存。

已适配的协议和用途如下：

| 协议 | 支持用途 | 实际调用 |
| --- | --- | --- |
| Anthropic 原生 | 对话 | Anthropic Messages SDK |
| OpenAI / OpenAI 兼容 | 对话、向量、重排、音频 | Chat Completions、`/embeddings`、`/rerank`、`/audio/transcriptions` |
| DeepSeek、智谱、MiniMax、硅基流动、Kimi、通义兼容 | 对话、向量、重排、音频 | 同一 OpenAI 兼容适配器，以上游实际实现为准 |
| 音频兼容 | 音频 | OpenAI 兼容音频转写 |

没有实现 Responses、Bedrock、Vertex 或各厂商私有原生 SDK。用途选项表示适配器支持的接口形状，不保证指定供应商或模型实现全部用途。新增或修改有效连接配置时须真实连通测试成功才能保存启用；音频测试上传 1 秒静音 WAV。

支持 8 个运行角色：

| 角色键 | 用途 |
| --- | --- |
| `agent` | 主智能体 |
| `subagent` | 子智能体 |
| `subagent_fast` | 快速子智能体 |
| `compact` | 上下文压缩 |
| `memory_selector` | 记忆检索 |
| `memory_extractor` | 记忆提取 |
| `memory_dream` | 记忆整理 |
| `workflow` | 工作流汇总、路由 |

角色只允许引用本工作区已启用的对话模型；停用、删除或改成其他用途前，须先解除角色指派。

## 模型解析顺序

1. 本次会话显式选择的模型。
2. 主智能体场景的当前用户、当前工作区默认模型；其他角色不套用个人主模型偏好。
3. 当前工作区对应运行角色。
4. 当前工作区 `agent` 角色。
5. 当前工作区第一个已启用的对话模型。
6. 环境变量回退：`AGENTOS_LLM_PROVIDER` 下的场景模型，再回退该 Provider 主模型。

没有数据库可用模型且环境回退也无效时，在聊天准入或实际使用该场景时报中文错误。启动不要求环境模型完整配置，数据库、Redis 等现有基础设施自检继续保留。

会话保存实际解析后的供应商 ID；`metadata.model_provider_override` 区分用户显式选择和角色解析结果。工作流和子代理仅在显式选择时继承会话供应商，否则继续走各自角色。压缩和记忆任务保留各自场景角色。

## 会话字段与更新语义

三个选项可随发送消息提交，也可通过会话更新接口保存：

| 字段 | 允许值 | 省略 | 显式 `null` |
| --- | --- | --- | --- |
| `model_provider_id` | 已启用的对话供应商 ID | 新会话用默认，已有会话保留选择 | 清除显式覆盖并重新解析默认 |
| `mode_slug` | `standard`、`turbo` | 新会话标准，已有会话保留 | 拒绝 |
| `thinking_effort` | `fast`、`low`、`medium`、`high`、`xhigh`、`max` | 新会话快速，已有会话保留 | 拒绝 |

已有会话更新使用请求中实际出现的字段，不能把 Schema 默认值误当用户修改。模型切换时，不支持的档位归一到该模型默认档位；极速始终锁定 `fast`。工作流拒绝极速，前端禁用该入口。

## 标准与极速模式

标准模式保留 Agent 工具、技能、计划、上下文压缩、Memory、Hook 和多智能体流程，思考深度按当前选择控制。

极速模式当前没有任何工具。后端目录尚无可靠的真实检索工具，Weather 仍为占位实现，因此没有把占位结果暴露给极速模型；极速提示词明确要求不得声称已执行代码、生成文件或完成不存在的网络查询。需要文件或执行的请求返回可复制正文并建议切标准。

极速旁路用户提示 Hook、Stop Hook、技能目录注入和执行、Memory 检索/目录/提取入队、历史上下文压缩、活动计划读取与计划约束；不创建子代理、不唤醒已有队友。即便会话已有计划、技能或队伍，也不能扩大极速能力边界。工具注册表与网关均收窄，模型循环有调用次数上限。

仅明确支持关闭思考的模型可进入极速。OpenAI 原生协议缺少显式关闭映射时，快速档位保留模型默认行为，能力接口返回 `turbo_supported=false`，前端和后端均阻止极速。

## 思考强度与协议映射

模型服务的多档思考配置支持 1 至 5 档；展示键为 `low/medium/high/xhigh/max`，每档上游值保留字符串或正整数原类型。默认档必须属于配置列表；关闭多档开关时不保留失效档位配置。历史只开开关但未配列表的记录兼容 `medium/high/max`。

| 协议 | 开启思考 | 快速档位 |
| --- | --- | --- |
| OpenAI 原生 | `reasoning_effort=<映射值>` | 仅显式配置 `fast_reasoning_effort=none` 才发送 `reasoning_effort=none` |
| DeepSeek | `extra_body.thinking.type=enabled`；有映射时附带 `reasoning_effort` | `thinking.type=disabled` |
| Chat Template | `chat_template_kwargs.thinking=true` 并携带强度；无映射时 `enable_thinking=true` | `enable_thinking=false` |
| Anthropic 字符串档位 | `thinking.type=adaptive`、`output_config.effort=<字符串>` | `thinking.type=disabled` |
| Anthropic 数值档位 | `thinking.type=enabled`、`budget_tokens=<整数>` | `thinking.type=disabled` |

Anthropic 数值预算须至少 1024 且小于配置最大输出长度。结构化调用按「预算 + 调用方输出需求」分配总额度，并限制在配置上限；开启思考时通过官方 SDK 流式聚合完整响应，避免大额度非流式请求被 SDK 拒绝。工具选择用 `auto`，校验重试保留完整带签名思考块和工具结果关联。

旧环境变量 OpenAI 客户端没有声明私有兼容协议，快速请求不猜测、也不发送第三方关闭字段。具体模型是否接受某字符串档位由供应商决定，界面允许通过真实元数据或手动配置声明。

## Memory 任务兼容

新 Memory Job 冻结模型名称和供应商 ID，执行时沿用冻结供应商。旧 Job 只有冻结模型名、没有供应商 ID 时，继续使用环境端点，避免后来配置的数据库供应商接收到历史任务。数据库供应商不存在或失效时报错，不把明确选择的端点静默换成其他端点。

## 元数据探测边界

探测只读取模型列表元数据：兼容服务 `/models`，Anthropic `/v1/models`。按完整模型名匹配，不做昂贵生成测试或根据模型名字猜能力。上游没有上下文长度时返回未知；没有明确思考档位列表时返回空列表，需查供应商文档后手填。探测成功不等于模型支持所有思考字段或已验证所有用途，连通测试也不能证明每个档位均被上游接受。

## 部署与回滚

新增迁移 `0027_model_service_options`，前置版本为 `0026_permission_workspace_scope`。部署时先备份数据库，确认连接指向目标环境，再由部署者执行：

```sh
uv run alembic upgrade head
```

升级增加模型服务/角色表和会话三个选项列。旧服务进程需要在迁移后由部署者重启，console 同步构建部署。回滚须先移除依赖新列的新代码并停止相关写入，再执行以下命令；这会删除模型服务配置与新会话选项：

```sh
uv run alembic downgrade 0026_permission_workspace_scope
```

2026-11-01 初始实现只对独立 PostgreSQL schema 执行迁移与测试。随后本机运行的模型接口因数据库停留在 `0026_permission_workspace_scope` 而缺少模型表；已按本项目迁移将当前 `.env` 指向的 `public` schema 升级到 `0027_model_service_options`，并确认模型表与会话选项列存在。现有 `8000` 服务未重启，设置页重试后已成功加载模型角色和供应商列表。

验证结果：隔离 schema `codex_model_review_b28869aae44a` 完成 Alembic `0026 → 0027 → 0026 → 0027` 往返；真实 PostgreSQL 定向集成 11 项通过；后端全量 **552 项通过，0 失败、0 跳过**。Console 全量 **501 项通过**，生产构建与 lint 通过且无警告。未运行 ruff（环境未安装）。

浏览器联调使用正常注册的临时账号和本机模拟 OpenAI 兼容上游完成：设置页新增服务、元数据探测、连通性测试、角色指派、个人默认模型和留空密钥编辑界面通过；普通会话标准模式向上游发送选择的 `low` 强度和 24 个工具，极速模式关闭思考且没有工具。刷新后会话配置恢复。主模型 A（`low/high`，默认 `high`，支持极速）与 Workflow 角色模型 B（仅 `medium`，默认 `medium`，不支持极速）配置不同后，工作流跟随 B、只显示 `medium` 并锁定标准模式；显式选 A 后恢复跟随角色，发送仍使用 B/`medium`，个人默认保持 A。最终会话元数据为 `model_provider_override=false`。回复由本机模拟上游生成，不代表真实第三方供应商实测。按授权清理本次隔离联调 schema 与临时数据。

官方参考：Anthropic Python SDK 的 [流式与长请求](https://github.com/anthropics/anthropic-sdk-python#long-requests)、[扩展思考](https://platform.claude.com/docs/en/build-with-claude/extended-thinking)。本项目验证使用已安装 SDK 0.104.1。

清理结果：已删除本次隔离 schema `codex_model_review_b28869aae44a`，并通过全新数据库连接确认该 schema 不存在、`public` 仍存在；临时账号及联调数据随 schema 一并移除。已关闭本次 `127.0.0.1:5511/8811/8812` 验收服务，并确认原有 `8000/5173` 服务仍在监听。
