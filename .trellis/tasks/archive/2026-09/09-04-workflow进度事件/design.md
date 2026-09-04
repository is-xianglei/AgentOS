# Workflow 进度事件 — 技术设计

对应 `prd.md` 的 D1–D5 与 R1–R4。本文只写技术方案，需求与验收见 PRD。
行号均以当前 `feature/unify-llm-client` 分支工作区为准。

## 1. 事件契约

### 1.1 `EventType.WORKFLOW_PROGRESS`
`core/events.py:28-40` 的 `EventType` 追加：

```python
WORKFLOW_PROGRESS = "workflow_progress"
```

docstring「协议事件类型(9 种,所有 actor 通用)」需改为 10 种并注明：该类型仅由
workflow 编排者产出，其余 actor 不发。

### 1.2 `WorkflowProgressInfo`
与 `BlockInfo` / `ToolInfo`（`core/events.py:112-148`）同模式的 frozen dataclass，
放在 `ErrorInfo`（`:193-208`）之后：

```python
class WorkflowPhase(str, Enum):
    """编排进度所处阶段。member 是成员节点,其余是编排者自身的阶段。"""
    MEMBER = "member"
    ROUTING = "routing"
    MERGING = "merging"


class WorkflowProgressStatus(str, Enum):
    """进度节点状态。"""
    RUNNING = "running"
    DONE = "done"
    FAILED = "failed"


@dataclass(frozen=True)
class WorkflowProgressInfo:
    """Workflow 编排进度:phase/status 受控,其余按非 None 输出。

    member 阶段必带 agent_id 与 member_name;routing/done 带被选中的 agent_id;
    ordinal/total 仅 Sequential 的 member 阶段携带(1 起算)。不带成员输出正文与路由理由。
    """

    phase: WorkflowPhase
    status: WorkflowProgressStatus
    agent_id: int | None = None
    member_name: str | None = None
    ordinal: int | None = None
    total: int | None = None

    def to_dict(self) -> dict[str, Any]:
        data: dict[str, Any] = {"phase": self.phase.value, "status": self.status.value}
        if self.agent_id is not None:
            data["agent_id"] = self.agent_id
        if self.member_name is not None:
            data["member_name"] = self.member_name
        if self.ordinal is not None:
            data["ordinal"] = self.ordinal
        if self.total is not None:
            data["total"] = self.total
        return data
```

`member_name` 取 `AgentRuntimeConfig.name`（`agent/service.py:40`，Agent 原名），
**不是** `wf-` 前缀截断后的 `display_name`：前端归节点靠 `agent_id`，`member_name` 只做
展示，给原名更友好；`actor.name` 仍是 `wf-...` 运行标识，两者职责不同。

### 1.3 受控取值表

| phase | 含义 | 允许的 status | 必带字段 | 可选字段 |
|---|---|---|---|---|
| `member` | 单个成员节点 | running / done / failed | agent_id、member_name | ordinal、total（仅 Sequential） |
| `routing` | LlmRouting 决策 | running / done / failed | — | done 时带选中 agent_id、member_name |
| `merging` | Parallel 汇总 | running / done / failed | — | — |

status 语义：`running` 已开始；`done` 正常完成；`failed` 异常终止（不含取消）。
每个 (phase, agent_id) 组合在一轮内 running 恰一次、终态至多一次。

**为什么不用 `detail: dict[str, Any]`**：前端要按字段做状态机，自由字典让契约只存在
于实现代码里，类型层无法约束、测试只能靠字符串键；受控枚举 + 标量字段可在
`WorkflowProgressInfo` 构造期就拦下非法组合，且与 `BlockInfo` / `ToolInfo` 风格一致。

### 1.4 `StreamEvent` 增量
`StreamEvent`（`core/events.py:211-229`）加字段 `progress: WorkflowProgressInfo | None = None`
（放在 `stop_reason` 之后、`sequence` 之前）；工厂方法放在错误工厂（`:333-335`）之后：

```python
# ---- 工厂方法:Workflow 编排进度 ----

@classmethod
def workflow_progress(
    cls, actor: Actor, session_id: int, progress: WorkflowProgressInfo
) -> "StreamEvent":
    return cls(
        type=EventType.WORKFLOW_PROGRESS,
        actor=actor,
        session_id=session_id,
        progress=progress,
    )
```

`to_dict()`（`:337-365`）在 `error` 之后追加：

```python
if self.progress is not None:
    data["progress"] = self.progress.to_dict()
```

该事件不是 `TURN_START`，actor 走 `to_dict_compact()`（`:341-343`），编排者 actor 序列化
恒为 `{"role": "orchestrator", "name": "orchestrator"}`。

### 1.5 payload 示例

Sequential 第 2/3 步成员开始：
```json
{"type": "workflow_progress", "session_id": 42, "sequence": 17,
 "actor": {"role": "orchestrator", "name": "orchestrator"},
 "progress": {"phase": "member", "status": "running",
              "agent_id": 2, "member_name": "审稿人", "ordinal": 2, "total": 3}}
```

Parallel 某成员失败（其余继续）：
```json
{"type": "workflow_progress", "session_id": 42, "sequence": 63,
 "actor": {"role": "orchestrator", "name": "orchestrator"},
 "progress": {"phase": "member", "status": "failed", "agent_id": 2, "member_name": "审稿人"}}
```

Parallel 进入汇总：
```json
{"type": "workflow_progress", "session_id": 42, "sequence": 80,
 "actor": {"role": "orchestrator", "name": "orchestrator"},
 "progress": {"phase": "merging", "status": "running"}}
```

Routing 选中成员：
```json
{"type": "workflow_progress", "session_id": 42, "sequence": 5,
 "actor": {"role": "orchestrator", "name": "orchestrator"},
 "progress": {"phase": "routing", "status": "done", "agent_id": 3, "member_name": "总结者"}}
```

## 2. Actor 扩展

### 2.1 字段
`Actor`（`core/events.py:62-75`）末尾追加：

```python
# 数据库 Agent 主键;仅 workflow 成员携带,内置子代理 / teammate / 主循环为 None。
agent_id: int | None = None
```

docstring 补一句：`agent_id` 供前端把事件稳定归到工作流图节点，name 可能重名或截断。

### 2.2 两个 `to_dict` 的行为
`to_dict_full()`（`:77-86`）与 `to_dict_compact()`（`:88-93`）都在末尾追加：

```python
if self.agent_id is not None:
    data["agent_id"] = self.agent_id
```

compact 也带的原因：`text_delta` / `tool_use` / `tool_result` / `block_*` / `error` 全走
compact 形态（`:339-343`），前端渲染节点内的实时输出要靠这些增量事件归节点；
只在 `turn_start` 带会迫使前端自己维护 run_id → agent_id 映射，与「前端按增量事件归节点」
的需求相悖。多出一个整数键对高频事件的体积影响可忽略。

### 2.3 `subagent_actor` 签名
`core/events.py:100-102` 改为：

```python
def subagent_actor(
    agent_type: str,
    run_id: str,
    task: str | None = None,
    *,
    agent_id: int | None = None,
) -> Actor:
    """构造一次性子代理来源(唯一键是 run_id);agent_id 仅 workflow 成员传入。"""
    return Actor(
        role=ActorRole.SUBAGENT, name=agent_type, run_id=run_id, task=task, agent_id=agent_id
    )
```

`teammate_actor`（`:105-109`）与 `ORCHESTRATOR_ACTOR`（`:97`）不动。

### 2.4 兼容性论证
- frozen dataclass 末尾追加带默认值的字段，不改变位置参数顺序；全库 `Actor(...)` 构造点
  仅 `core/events.py:97,102,109` 三处且全为关键字传参。
- `subagent_actor` 调用点仅 `runtime/subagent.py:137` 与 `tests/test_stream_assembler.py:263`，
  新参数为 keyword-only 且有默认值，后者不改也通过。
- 序列化只在非 None 时输出键，`tests/test_stream_assembler.py:274` 断言的
  `{"role": "subagent", "name": "explore", "run_id": "run-1"}` 原样成立（AC4）。
- `dataclass(frozen=True)` 的 `__eq__` / `__hash__` 会纳入新字段，`ORCHESTRATOR_ACTOR` 是
  模块级单例且 agent_id 恒 None，比较语义不变。

## 3. agent_id 传递链

```
AgentRuntimeConfig.agent_id            agent/service.py:33（已存在，无需改）
  → SubAgentSpec.agent_id              tools/subagents/definition.py:57（新增字段）
     由 runtime/workflow.py:162-168 的 _execute_member 构造时传 config.agent_id
  → SubAgentRunner.run(spec)           runtime/subagent.py:86
     :135  run_repo.create(session_id, member_name, prompt, agent_id=spec.agent_id)
     :137  subagent_actor(member_name, run_id=..., task=prompt, agent_id=spec.agent_id)
  → 该 actor 贯穿 TurnEventAssembler 与全部成员事件
```

### 3.1 `SubAgentSpec.agent_id`
`tools/subagents/definition.py:85` 的 `display_name` 之后追加：

```python
# 数据库 Agent 主键;动态 spec(workflow 成员)传入,供事件与运行记录归到具体 Agent。
# 内置 spec 留空,事件形状与运行记录保持原样。
agent_id: int | None = None
```

内置四个 spec（`tools/subagents/registry.py:43,55,69,82`）不传，保持 None。
`runtime/workflow.py:162-168` 加一行 `agent_id=config.agent_id`。

### 3.2 内置路径与 team 路径
- Agent 工具派生的内置子代理：`spec.agent_id is None` → actor 不带键、运行记录 NULL。
- `run_teammate()`（`runtime/subagent.py:480` 用 `teammate_actor`）不经过 `subagent_actor`
  也不调 `run_repo.create`；`team/service.py:165,175` 只 `list_by_session`。两者零改动。

## 4. 三个编排器的发事件点

### 4.1 公共 helper（`BaseOrchestrator`，`runtime/workflow.py:99-218`）
在 `_emit_final_text`（`:198`）之前新增两个方法：

```python
async def _emit_progress(
    self,
    phase: WorkflowPhase,
    status: WorkflowProgressStatus,
    *,
    config: AgentRuntimeConfig | None = None,
    ordinal: int | None = None,
    total: int | None = None,
) -> None:
    """以编排者身份推送进度事件;member 阶段从 config 取 agent_id 与原名。"""
    await self.bus.emit(
        StreamEvent.workflow_progress(
            ORCHESTRATOR_ACTOR,
            self.session_id,
            WorkflowProgressInfo(
                phase=phase,
                status=status,
                agent_id=config.agent_id if config else None,
                member_name=config.name if config else None,
                ordinal=ordinal,
                total=total,
            ),
        )
    )

async def _run_member_with_progress(
    self,
    db: AsyncSession,
    config: AgentRuntimeConfig,
    prompt: str,
    *,
    ordinal: int | None = None,
    total: int | None = None,
) -> str:
    """成员执行外包一层 running/done/failed 进度;取消不发 failed,保持既有取消语义。"""
    await self._emit_progress(MEMBER, RUNNING, config=config, ordinal=ordinal, total=total)
    try:
        output = await self._execute_member(db, config, prompt)
    except asyncio.CancelledError:
        raise
    except Exception:
        await self._emit_progress(MEMBER, FAILED, config=config, ordinal=ordinal, total=total)
        raise
    await self._emit_progress(MEMBER, DONE, config=config, ordinal=ordinal, total=total)
    return output
```

约定：**`failed` 只与已发出的 `running` 配对**。配置期失败（`_load_member_config`，`:138-148`，
成员停用 / 软删）发生在 `running` 之前，不发 `failed`，由 `WorkflowRuntime._produce`
（`:547-566`）的 `error` 事件承担整体失败告知；这与既有「配置期整体失败」语义一致。

`run_member`（`:187-196`）改为内部调 `_run_member_with_progress`，签名追加
`ordinal` / `total` 关键字参数；它仍是 Sequential 的唯一调用方。

### 4.2 Sequential（`:227-257`）
- `for member in self.members` 改为 `for ordinal, member in enumerate(self.members, start=1)`，
  `total = len(self.members)`。
- `:241` 的 `self.run_member(self.db, member.agent_id, prompt)` 加 `ordinal=ordinal, total=total`。
- 事件序（三成员）：`member/running(1/3)` → 成员 1 自发事件 → `member/done(1/3)` →
  `member/running(2/3)` → … → `member/done(3/3)` → 编排者最终文本块。
- 成员失败：`member/failed(N/M)` 后由 `:242-248` 包成「顺序编排已中止」抛出，后续成员无事件。

### 4.3 Parallel（`:271-295`、`:297-325`、`:327-360`）
- `run()`：`:275-277` 配置加载完成后、`asyncio.gather` 之前，对 `configs` 逐个
  `_emit_progress(MEMBER, RUNNING, config=config)`。全部 `running` 先于任何成员派发，
  前端一次性把所有节点点亮（AC2）。
- `_run_member_isolated()`：成功路径在 `:325` return 之前发 `member/done`；
  `:312-324` 的 `except Exception` 分支在 `logger.exception` 之后、`raise` 之前发
  `member/failed`——这是编排层首次拥有「成员失败」的显式标记；`:310-311` 的
  `CancelledError` 分支不发。
- `run()` 在 `:290-294` 全败判定之后、`:295` `_merge` 之前发 `merging/running`；
  `_merge` 返回后发 `merging/done`。`_merge` 抛非取消异常（LLM 失败、`:350-354`
  空文本）时发 `merging/failed` 再抛。全败时不进入 merging 阶段、不发 merging 事件。
- 事件序：`running×N` → 各成员自发事件交错 → `done`/`failed` 交错 → `merging/running` →
  编排者汇总文本块（`TurnEventAssembler` 逐字）→ `merging/done`。

### 4.4 LlmRouting（`:370-379`、`:381-429`）
- `run()`：`:373-375` 配置加载后发 `routing/running`；`:376` `_route` 调用包一层：
  非取消异常发 `routing/failed` 后重抛（`_route` 内部已把 LLM 异常与「不在成员列表」
  都转成 AgentException，`:409-419`）；成功后发 `routing/done`，`config=chosen`，
  携带选中的 agent_id / member_name。
- `:377` `_execute_member(self.db, chosen, user_content)` 改为
  `_run_member_with_progress(self.db, chosen, user_content)`（不传 ordinal/total）。
- 事件序：`routing/running` → `routing/done(agent_id=X)` → `member/running(X)` →
  成员 X 自发事件 → `member/done(X)` → 编排者最终文本块。未选中成员零条进度事件（AC3）。

### 4.5 顺序与并发保证
- `StreamBus` 是单个 FIFO `asyncio.Queue`（`core/event_bus.py:18-35`），进度事件与成员
  自发事件共用同一队列，`done` 必然排在该成员 `turn_end` 之后（`_execute_member` 的
  `await` 返回时成员事件已全部 emit）。
- Parallel 下各成员的 `done`/`failed` 由各自协程发出，彼此顺序不定，但每个成员内部
  `running` 必先于其 `done`/`failed`（前者在 gather 之前串行发出）。
- 进度事件不落库、不参与 `session_messages` / `session_snapshots`，与消息中立化协议无关。

### 4.6 模块 docstring 与常量
`runtime/workflow.py:4` 的「不新增事件类型」改为「编排阶段以 `workflow_progress` 事件
上报节点状态」；`:25` 的导入追加 `WorkflowPhase, WorkflowProgressInfo, WorkflowProgressStatus`。
为缩短调用行，可在模块级取别名 `_MEMBER = WorkflowPhase.MEMBER` 等，不新造字符串常量。

## 5. 数据库

### 5.1 ORM（`team/models.py:91-115`）
`SubAgentRunRecord` 在 `member_name`（`:101`）之后追加，照 `sessions.agent_id`
（`session/models.py:81-86`）的写法：

```python
agent_id: Mapped[int | None] = mapped_column(
    ForeignKey("agents.id", ondelete="SET NULL"),
    nullable=True,
    index=True,
    comment="成员对应的Agent ID，仅Workflow成员填写，内置子代理与团队成员为空",
)
```

不加 `relationship`：运行记录目前无反查 Agent 对象的读路径，保持最小改动。

### 5.2 Repository（`team/subagent_run_repository.py:13-23`）

```python
async def create(
    self,
    session_id: int,
    member_name: str,
    prompt: str,
    *,
    agent_id: int | None = None,
) -> SubAgentRunRecord:
```

构造时加 `agent_id=agent_id`。仍 add/flush/refresh，不 commit。

### 5.3 迁移 `alembic/versions/0023_subagent_run_agent_id.py`
照 `0022_create_workflows.py:135-152` 加 `sessions.workflow_id` 的三步：

```python
revision = "0023_subagent_run_agent_id"
down_revision = "0022_create_workflows"

def upgrade() -> None:
    op.add_column(
        "subagent_runs",
        sa.Column("agent_id", sa.Integer(), nullable=True,
                  comment="成员对应的Agent ID，仅Workflow成员填写，内置子代理与团队成员为空"),
    )
    op.create_foreign_key(
        "fk_subagent_runs_agent_id_agents", "subagent_runs", "agents",
        ["agent_id"], ["id"], ondelete="SET NULL",
    )
    op.create_index("ix_subagent_runs_agent_id", "subagent_runs", ["agent_id"])

def downgrade() -> None:
    op.drop_index("ix_subagent_runs_agent_id", table_name="subagent_runs")
    op.drop_constraint("fk_subagent_runs_agent_id_agents", "subagent_runs", type_="foreignkey")
    op.drop_column("subagent_runs", "agent_id")
```

存量行不回填：历史运行记录无法可靠反推 Agent，保持 NULL。

### 5.4 registry 无需改
`SubAgentRunRecord` 已经由 `database/registry.py:29` 的 `from team.models import (...)`
登记；本任务只加列不加实体，autogenerate 不会误判。

## 6. 测试策略

### 6.1 改既有断言
`tests/test_workflow_orchestration.py:243-244`：

```python
types = [e.type for e in orchestrator.bus.events if e.type is not EventType.WORKFLOW_PROGRESS]
assert types == [EventType.BLOCK_START, EventType.TEXT_DELTA, EventType.BLOCK_STOP]
```

`:307` 已按 `TEXT_DELTA` 过滤，`:871` 的 orchestrator 是替身（`:844-847`），两处不改。

### 6.2 新增用例（同文件，Fake/monkeypatch 风格，`_RecordingBus` `:61-69` 记录事件对象）
补一个提取 helper：`_progress(bus) -> list[WorkflowProgressInfo]`，按 type 过滤后取
`event.progress`。

| 用例 | 对应 AC | 断言 |
|---|---|---|
| `test_sequential_emits_member_progress_with_ordinal` | AC1 | 三成员各 running/done；`ordinal` 1/2/3、`total` 3；`agent_id`/`member_name` 对应 `_CONFIGS` |
| `test_sequential_emits_failed_before_abort` | AC1 | 失败成员有 failed、无 done；第 3 个成员零条进度 |
| `test_parallel_emits_running_for_all_before_dispatch` | AC2 | 前 3 条进度全为 running，且 sequence 早于任何 `_FakeSubAgentRunner.run` 调用（`_Harness.calls` 记录时读 bus 长度） |
| `test_parallel_emits_failed_for_single_member_and_merging` | AC2 | 失败成员 failed、其余 done；`merging/running` 在最后一个成员终态之后、`_FakeLLM.stream` 调用之前；`merging/done` 最后 |
| `test_parallel_all_failed_skips_merging` | AC2 | 三条 failed，零条 merging |
| `test_routing_emits_routing_then_only_selected_member` | AC3 | `routing/running` → `routing/done(agent_id=选中)` → `member/running` → `member/done`，未选中成员零条 |
| `test_routing_decision_failure_emits_routing_failed` | AC3 | `fail_structured=True` 时有 `routing/failed`、零条 member |
| `test_member_spec_carries_agent_id` | AC4/AC5 | `_FakeSubAgentRunner` 记录 `spec.agent_id == config.agent_id` |

### 6.3 `tests/test_stream_assembler.py` / 新 `tests/test_events_actor.py`
- `subagent_actor("explore", run_id="run-1").to_dict_compact()` 不含 `agent_id`（AC4 零变化）。
- `subagent_actor("wf-x", run_id="run-2", agent_id=7)` 的 full 与 compact 都含 `"agent_id": 7`。
- `StreamEvent.workflow_progress(...).to_dict()` 的 `progress` 键与 §1.5 示例一致，
  actor 为 compact 形态。

### 6.4 `tests/test_subagent_spec.py`
内置 spec `agent_id is None`；传 `agent_id=7` 时可读回。

### 6.5 runtime/subagent.py 透传
`tests/test_logging_chain.py:91-109` 已 monkeypatch `SubAgentRunner`，不覆盖真实 `run()`；
本任务补一条针对 `runtime/subagent.py:135-137` 的用例：替换 `run_repo` 与 `bus` 为记录替身，
跑真实 `SubAgentRunner.run`（`_run_react_loop` 打桩），断言 `run_repo.create` 收到
`agent_id=spec.agent_id`、`turn_start` 事件 actor 含 `agent_id`。

### 6.6 欠账（需真实 PostgreSQL）
迁移往返（AC6）与 `subagent_runs.agent_id` 真实落库（AC5 的数据库侧）按项目规约需
真实集成测试，本任务在 implement 中以 `alembic upgrade/downgrade` 命令验证并如实报告。

## 7. 兼容性与回滚

### 7.1 前端容错前提
本仓库无前端工程。新事件依赖前端对未知 `type` 容错（忽略而非崩溃）；
`actor.agent_id` 是新增键，旧前端不读即无影响。契约变更通过 code-spec 与 README 告知。

### 7.2 事件形状零变化面
- 主循环（`ORCHESTRATOR_ACTOR`）、teammate、内置子代理三类 actor 的 `agent_id` 恒 None，
  序列化不输出键，逐字节不变。
- `format_sse`（`runtime/agent.py:1851-1853`）与 `StreamBus`（`core/event_bus.py`）零改动。
- `session_messages` / `session_snapshots` 不落进度事件，中立形状协议不受影响。

### 7.3 回滚点
| 层 | 回滚方式 |
|---|---|
| 事件契约（S1） | 删枚举值、dataclass、工厂与 `progress` 字段；无外部依赖 |
| Actor / Spec / Repository 透传（S2） | 删字段与关键字参数；调用点全为默认值 |
| 数据库（S3） | `alembic downgrade -1` 回到 `0022_create_workflows` |
| 编排器（S4） | 删 `_emit_progress` / `_run_member_with_progress` 及其调用 |

各层可独立回退；S4 依赖 S1，S3 依赖 S2 的 Repository 参数。

## 8. 取舍记录

- **StreamEvent 而非 RuntimeEvent**：`RuntimeEvent` 的定位是「不属于某个 actor 的会话级
  控制事件」（`core/events.py:368-374`，如 `session_ready` / `task_snapshot`）；编排阶段
  是编排者这个 actor 的行为，且落在其 `turn_start` / `turn_end` 之间，用 `StreamEvent`
  能复用 actor 与 sequence 语义，前端也能把它与编排者的最终文本块归到同一 turn。
- **compact 也带 agent_id**：见 §2.2。增量事件是前端渲染节点内实时输出的唯一来源，
  只在 full 形态带等于要求前端自建 run_id 映射，与需求相悖。
- **不发路由理由**：路由走 `complete_structured(_RoutingChoice)`（`runtime/workflow.py:402-406`），
  结构化输出只有 `agent_id` 一个字段，本就没有理由文本；若要理由需改 schema 增加 LLM
  输出长度与不确定性，且理由属模型自述，可能泄露候选描述细节，范围外。
- **member_name 用 Agent 原名而非 `wf-` 运行标识**：见 §1.2。
- **配置期失败不发 `member/failed`**：`failed` 只与已发 `running` 配对，避免前端出现
  「从未 running 的节点直接 failed」的状态机异常；整体失败由 `error` 事件承担。
- **Parallel 全败不发 merging**：没有进入汇总阶段就不上报该阶段，与实际执行路径一致。
- **进度事件不落库**：历史回放属范围外；若将来需要，`subagent_runs.agent_id` 已可支撑
  按 session 反查各节点终态，无需再存进度事件本身。
- **不给 `SubAgentRunRecord` 加 relationship**：无读路径，避免无用的懒加载面。
