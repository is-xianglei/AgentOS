# Workflow 进度事件 — 执行计划

依据 `prd.md`（需求/验收）与 `design.md`（技术方案）。按序执行，每步跑校验后再进下一步。

## 校验命令

```bash
uv run --with ruff ruff check .           # line-length 100, target py314（pyproject.toml:34-36）
uv run --with ruff ruff format --check .
uv run pytest -q                          # testpaths=["tests"]（pyproject.toml:26-28）
uv run pytest tests/test_workflow_orchestration.py tests/test_stream_assembler.py \
  tests/test_subagent_spec.py tests/test_logging_chain.py -q   # 事件 / 子代理链路回归
uv run python -c "from core.config import settings; print(settings.llm_provider)"  # Settings 可加载
uv run alembic heads                      # 实施前确认 head 仍是 0022_create_workflows
uv run alembic upgrade head && uv run alembic downgrade -1 && uv run alembic upgrade head  # 迁移双向
```

数据库连接直接用 `.env` 中已配置的值，不改地址、不自建服务。若外部服务连不上，
先报告具体错误与配置项名，不擅自安装或覆盖配置。本任务不新增配置项。

## 步骤

### S1 事件契约（`core/events.py`）
1. `:28-40` `EventType` 追加 `WORKFLOW_PROGRESS = "workflow_progress"`，docstring 的
   「9 种」改「10 种」并注明仅 workflow 编排者产出。
2. `:193-208` `ErrorInfo` 之后新增 `WorkflowPhase`、`WorkflowProgressStatus` 两个
   `str, Enum` 与 `WorkflowProgressInfo` frozen dataclass（design §1.2），`to_dict()`
   非 None 才输出键。
3. `:211-229` `StreamEvent` 加 `progress: WorkflowProgressInfo | None = None`；
   `:333-335` 之后加 `workflow_progress()` 工厂；`:337-365` `to_dict()` 在 `error`
   之后追加 `progress` 序列化。
4. `:62-93` `Actor` 末尾加 `agent_id: int | None = None`，`to_dict_full` 与
   `to_dict_compact` 都在非 None 时输出；`:100-102` `subagent_actor` 加 keyword-only
   `agent_id: int | None = None` 并透传（design §2）。
5. **回归重点**：`tests/test_stream_assembler.py:274` 的 actor 断言必须原样通过
   （agent_id 为 None 不输出键）；`ORCHESTRATOR_ACTOR` 序列化不变。
   新增 `tests/test_events_actor.py` 覆盖 design §6.3。

### S2 agent_id 传递链（Spec / Runner / Repository）
1. `tools/subagents/definition.py:85` `display_name` 之后加 `agent_id: int | None = None`
   与中文注释（design §3.1）。内置 spec（`tools/subagents/registry.py:43,55,69,82`）不动。
2. `team/subagent_run_repository.py:13` `create()` 加 keyword-only
   `agent_id: int | None = None`，构造 `SubAgentRunRecord` 时传入（design §5.2）。
3. `runtime/subagent.py:135` 改 `run_repo.create(session_id, member_name, prompt,
   agent_id=spec.agent_id)`；`:137` 改 `subagent_actor(member_name, run_id=..., task=prompt,
   agent_id=spec.agent_id)`；`:136` 注释补「workflow 成员额外带 agent_id」。
4. `runtime/workflow.py:162-168` `SubAgentSpec(...)` 加 `agent_id=config.agent_id`。
5. **回归重点**：`tests/test_subagent_spec.py` 补内置 spec `agent_id is None`；
   `tests/test_logging_chain.py` 全过（它 monkeypatch 了 `SubAgentRunner`，不受影响）；
   补 design §6.5 针对 `runtime/subagent.py:135-137` 的透传用例。
   `team/service.py:165,175` 只调 `list_by_session`，确认无需改。

### S3 数据库列与迁移
1. `team/models.py:101` `member_name` 之后加 `agent_id` 列（design §5.1）：
   FK `agents.id`、`ondelete="SET NULL"`、`nullable=True`、`index=True`、中文 comment。
   `team/models.py:3` 的 sqlalchemy 导入已含 `ForeignKey`，无需新增。
2. 新建 `alembic/versions/0023_subagent_run_agent_id.py`，
   `down_revision = "0022_create_workflows"`（`0022_create_workflows.py:12`）。
   照 `0022_create_workflows.py:135-152` 的 add_column → create_foreign_key →
   create_index 三步，FK 命名 `fk_subagent_runs_agent_id_agents`、索引
   `ix_subagent_runs_agent_id`；downgrade 逆序（design §5.3）。
3. `database/registry.py:29` 已登记 `team.models`，不改。
4. **回归重点**：`uv run alembic heads` 只有一个 head；双向迁移命令跑通；
   `uv run alembic check`（若可用）不报 ORM 与库结构差异。

### S4 编排器发事件（`runtime/workflow.py`）
1. `:25` 导入追加 `WorkflowPhase, WorkflowProgressInfo, WorkflowProgressStatus`；
   `:4` 模块 docstring 的「不新增事件类型」改写（design §4.6）。
2. `BaseOrchestrator`（`:99-218`）在 `_emit_final_text`（`:198`）前新增
   `_emit_progress()` 与 `_run_member_with_progress()`（design §4.1）；
   `run_member`（`:187-196`）改为调用后者并追加 `ordinal` / `total` 关键字参数。
   `CancelledError` 分支不发 `failed`。
3. Sequential `run()`（`:227-257`）：`:230` 改 `enumerate(self.members, start=1)`，
   `:241` 传 `ordinal` / `total`（design §4.2）。
4. Parallel：`run()` `:275-277` 配置加载后逐成员发 `member/running`；
   `_run_member_isolated`（`:297-325`）成功前发 `done`，`:312-324` 失败分支在
   `logger.exception` 后、`raise` 前发 `failed`；`:295` `_merge` 前发 `merging/running`、
   返回后 `merging/done`，非取消异常 `merging/failed`（design §4.3）。
   全败（`:290-294`）不进 merging。
5. LlmRouting `run()`（`:370-379`）：`:376` `_route` 前后发 `routing/running` /
   `routing/done(config=chosen)`，异常 `routing/failed`；`:377` 改用
   `_run_member_with_progress`（design §4.4）。
6. **回归重点**：`tests/test_workflow_orchestration.py:243-244` 改为过滤
   `WORKFLOW_PROGRESS` 后断言（design §6.1）；`:307` 与 `:871` 不改。
   `_FakeSubAgentRunner`（`:123-142`）无需改动，`spec.agent_id` 可直接读。
   确认 Parallel 的 `running` 全部先于任何 `_FakeSubAgentRunner.run` 调用。

### S5 测试
在 `tests/test_workflow_orchestration.py` 按 design §6.2 表新增 8 个用例，加一个
`_progress(bus)` 提取 helper；`tests/test_events_actor.py`（§6.3）、
`tests/test_subagent_spec.py`（§6.4）、`runtime/subagent.py` 透传（§6.5）各补齐。
异步用例沿用 `pytestmark = pytest.mark.anyio`（`:36`）。
覆盖成功路径、失败路径（成员失败 / 路由失败 / 全败）与关键副作用（进度事件顺序）。

### S6 code-spec 与 README 回写
1. `.trellis/spec/backend/workflow-orchestration.md`：
   - `:172`「不新增事件类型」→ 编排者额外发 `workflow_progress`；
   - `:221`「编排阶段不额外发事件类型」→ 从已接受取舍中删除，改为本任务的取舍
     （不发路由理由、不落库、配置期失败不发 failed）；
   - `:265` Base 用例事件序补进度事件；
   - §2 Signatures 补 `WorkflowProgressInfo` / `subagent_actor(agent_id=)` /
     `SubAgentRunRepository.create(agent_id=)`；§2.4 DB 补 `0023`；
   - §6.1 表 `:296` 的「bus 事件恰为」改为「过滤进度事件后恰为」，并补新增用例行；
   - §1 关键文件补 `core/events.py`、`team/models.py`、`alembic/versions/0023_...`；
   - 设计背景指向本任务目录。
2. `README.md:57-61` SSE 段补一行：workflow 会话额外产出 `workflow_progress` 事件，
   `progress.phase` / `status` 受控取值，成员事件 `actor.agent_id` 可归节点。
3. `.trellis/spec/backend/index.md:23` 一句话描述补「进度事件」。

## 提交前检查

- [ ] `tests/test_stream_assembler.py:274` 原样通过；内置子代理事件形状零变化（AC4）
- [ ] `alembic heads` 单 head，`upgrade / downgrade -1 / upgrade` 三步跑通（AC6）
- [ ] `subagent_runs.agent_id` 对内置子代理与 team 路径为 NULL（AC5）
- [ ] Sequential / Parallel / Routing 进度事件序与 design §4.2–4.4 一致（AC1–AC3）
- [ ] `CancelledError` 路径不发任何 `failed`
- [ ] `runtime/workflow.py:4` docstring 与 code-spec 三处记述已改写（AC8）
- [ ] 全量 `uv run pytest -q` 与 ruff 通过，如实报告命令与结果（AC7）
- [ ] 未新增配置项；不自动 `git push`
