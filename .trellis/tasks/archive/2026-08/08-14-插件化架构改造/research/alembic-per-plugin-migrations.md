# 调查报告：Alembic 每插件独立迁移链的可行性

> 调查日期：2026-08-14 · Alembic 官方文档版本 1.19.1（项目依赖 `alembic>=1.17.2`，pyproject.toml）
> 文档来源：Context7（/websites/alembic_sqlalchemy，trustScore 9.7）+ alembic.sqlalchemy.org 原文核对

## 结论速览

| 问题 | 一句话结论 |
|---|---|
| Q1 能否 N 条互不干扰的迁移链并存 | **能**，且有两条官方路线；推荐"程序化多环境"（每插件独立 version 表），代价是放弃 `depends_on` 跨链依赖 |
| Q2 插件卸载能否只 downgrade 自己 | **能**，官方直接支持；多环境路线下天然隔离，多 base 路线用 `<branch>@base` |
| Q3 autogenerate 会不会误删别人的表 | **默认一定会**，官方明确要求用 `include_name`/`include_object` 钩子防护，这是硬性前提 |
| Q4 退路 | 独立 PG schema / JSONB 宿主表 / 自带 SQL 脚本，均可行但各有明确代价 |

**推荐方案**：程序化多环境（cookbook "Run Multiple Alembic Environments" 模式的代码化版本）——
每插件自带 `migrations/` 目录 + 独立 MetaData + 独立 `alembic_version_<plugin>` 版本表，
运行时用 `alembic.config.Config` 程序化构造配置逐插件执行 upgrade/downgrade，不改全局 alembic.ini。
失败模式见「推荐方案」一节，全部可通过工程约束封堵。

---

## Q1：Alembic 能否支持 N 条互不干扰的迁移链并存？

**答：能。官方提供两条正式路线，适用场景不同。**

### 路线 A：多 base（Multiple Bases）——共享一张 alembic_version 表

官方文档 branches.html「Working with Multiple Bases」原文要点：

> "The multiple base feature is intended to allow for multiple Alembic versioning lineages
> **which share the same alembic_version table**. This is so that individual revisions within
> the lineages can have cross-dependencies on each other. For the simpler case where one
> project has multiple, **completely independent** revision lineages that refer to
> **separate alembic_version tables**, see the example in
> Run Multiple Alembic Environments from one .ini file."
> —— https://alembic.sqlalchemy.org/en/latest/branches.html

即官方自己划定了边界：**链之间需要 `depends_on` 交叉依赖 → 用多 base；完全独立 → 用多环境。**

多 base 做法（branches.html）：
1. `alembic.ini` 配 `version_locations = %(here)s/model/networking:%(here)s/alembic/versions`，
   `path_separator = os`（本仓库 alembic.ini:4 已有此项）；
2. 建新链：`alembic revision -m "..." --head=base --branch-label=networking --version-path=model/networking`；
3. 之后该链的新 revision 用 `--head=networking@head`，目录自动选对。

多 base 的代价（全部来自官方文档）：

- **`alembic upgrade head` 从此永久失效**："FAILED: Multiple head revisions are present for
  given argument 'head'; please specify a specific target revision, '<branchname>@head' ...
  or 'heads' for all heads"。所有升级命令必须改为 `upgrade heads` 或逐链 `<label>@head`。
- **alembic_version 表变成多行**（详见 Q2 证据），任何假设"该表只有一行"的工具/脚本会坏。
- 所有链共用一个 env.py 与一个 `target_metadata`，autogenerate 时必须能"看见"全部插件的
  MetaData，装卸插件会改变 autogenerate 的世界观（引出 Q3 的误删风险）。
- 新建链首个 revision 必须显式 `--version-path`，且 `script.py.mako` 必须含
  `branch_labels = ${repr(branch_labels)}` 占位（否则报官方文档中列出的 FAILED 错误；
  本仓库 mako 模板需核对）。

### 路线 B：多环境（Run Multiple Alembic Environments）——每链独立 alembic_version 表

官方 cookbook 原文要点：

> "projects had a need to maintain more than one Alembic version history in a single project,
> where these version histories are **completely independent** of each other and **each refer
> to their own alembic_version table**... A simple approach was added to support this, the
> `--name` flag on the commandline."
> —— https://alembic.sqlalchemy.org/en/latest/cookbook.html#run-multiple-alembic-environments-from-one-ini-file

做法：alembic.ini 里 `[DEFAULT]` 放共享配置，每个环境一个 named section
（`[schema1]` `[schema2]`…各自 `script_location`），命令行 `alembic --name schema2 upgrade head`。
每个环境有**自己的 env.py、自己的版本目录、自己的版本表**（env.py 里
`context.configure(version_table="alembic_version_<name>")`，api/runtime.html 确认
`version_table` 参数默认 `'alembic_version'`、可自由改名，另有 `version_table_schema` 可指定 schema）。

官方同页明确："The above approach can be automated by creating a custom front-end to the
Alembic commandline as well."——即程序化驱动是官方认可的方向。api/config.html 给出无 ini 的
程序化配置示例：`Config()` + `set_main_option("script_location", ...)`。

多环境的代价：
- **一次命令只能跑一个环境**，插件多时需要宿主循环调用（程序化正好解决）；
- **链之间零依赖表达能力**：不能用 `depends_on` 声明"插件 A 的表依赖核心的 users 表已存在"，
  依赖顺序只能由宿主的插件加载顺序保证；
- named section 方式**不适用于 pyproject.toml 配置**（官方注明 "it does not apply to
  pyproject.toml configuration"）；本仓库用 alembic.ini（alembic.ini:1-5），不受影响。

### 仓库现状（改造起点）

- 单链 21 个 revision，起点 `0001_create_agentos_base_tables`（down_revision=None），
  唯一 head 为 `0020_agent_mvp_comments`；全部文件 `branch_labels=None`、`depends_on=None`，
  **纯线性、无分支**。注意链中有一处命名断层：`d5245f5576f8_create_users_table.py` 插在
  `0007_create_skills` 与 `0008_create_workspaces` 之间（0008 的
  down_revision='d5245f5576f8'，alembic/versions/0008_create_workspaces_table.py:12）。
- `alembic.ini:2-4`：`script_location = alembic`、`path_separator = os`、**未配置
  version_locations**（当前只有默认 `alembic/versions` 一个目录）。
- `alembic/env.py:25`：`target_metadata = Base.metadata`，单一 metadata；
  `env.py:7` 副作用导入 `database.registry` 汇总全部实体。
- `alembic/env.py:32,45`：已用 `include_name=include_alembic_name` 钩子排除自维护的
  `alembic_version_history` 表（database/migration_history.py:20-27）——证明本仓库已有
  include_name 过滤先例，扩展为插件表过滤是同一机制。
- `alembic/env.py:46`：`on_version_apply=record_version_apply` 往
  `alembic_version_history` 表写审计记录（database/migration_history.py:30-66），
  其 `resulting_heads` 字段（migration_history.py:63）天然支持多 head，无需改。
- `database/registry.py:7-37`：全部 14 个域、31 个实体的静态 import 汇总；
  docstring（registry.py:1-5）明言"遗漏任一模块会导致自动迁移误判为删表"——
  这正是 Q3 问题在单链下的既有体现。
- `database/engine.py:14`：副作用导入 registry，保证映射完整。
- `main.py:24-26`：lifespan 里 `settings.create_tables` 为真时 `Base.metadata.create_all`；
  `core/config.py:59` 默认 False（仅开发用）。**create_all 只认 Base.metadata**，
  插件若用独立 MetaData，此逻辑覆盖不到，需插件自跑迁移。

### Q1 结论

N 条互不干扰的链**可以并存**，官方两条路线都是正式文档特性而非 hack。
完全独立装卸的插件场景与官方对"多环境"路线的定义逐字吻合
（"completely independent revision lineages"）。代价汇总：多 base 牺牲命令兼容性
（head→heads）并共享版本表；多环境牺牲跨链 `depends_on`，换来彻底隔离。

---

## Q2：插件卸载时如何只 downgrade 自己那条链？

**答：两条路线都有官方支持的做法，多环境路线隔离性更强。**

### 多 base 路线：`alembic downgrade <label>@base`

官方 branches.html「Running with Multiple Bases」原文：

> "We may now run upgrades or downgrades **freely, among individual branches**...
> `$ alembic upgrade networking@head`"

降级用同一语法反向：`alembic downgrade networking@base` 把 networking 链完全退干净，
不触碰其他链。alembic_version 表按官方展示的 SQL 语义维护多行（branches.html 原文 SQL）：

```sql
-- 每多一个未合并分支就 INSERT 一行
INSERT INTO alembic_version (version_num) VALUES ('ae1027a6acf')
-- 合并时 DELETE 一行 + UPDATE 一行
DELETE FROM alembic_version WHERE version_num = 'ae1027a6acf'
UPDATE alembic_version SET version_num='53fffde5ad5' WHERE version_num = '27c6a30d7c24'
```

即 **alembic_version 是"每个当前 head 一行"的多行表**（回答任务中的"单表多行？"——是，
单表多行，官方设计如此）。`MigrationContext.get_current_heads()`（api/runtime.html）
返回 tuple，官方注明 "when multiple unmerged branches exist within the target database,
the returned tuple will contain a value for each head"。downgrade 关闭分支时官方描述：
"If multiple heads exist, Alembic will close out branches from the alembic_version table
until only one remains"——即降到 base 时删除该链对应的行。

多 base 卸载的坑：如果任何 revision 用了 `depends_on` 指向别的链
（branches.html「Branch Dependencies」），或出现过 merge revision
（down_revision 为元组），该链就不再"纯净"，`<label>@base` 降级会受依赖约束牵制。
**插件场景必须禁止跨链 depends_on 与跨链 merge**，否则隔离承诺失效。

### 多环境路线：每插件独立版本表，卸载 = 对自己环境跑 downgrade base

每插件 env.py 里 `context.configure(version_table="alembic_version_plugin_<name>")`，
卸载时程序化执行该插件环境的 `command.downgrade(cfg, "base")`：
- 只读写自己的版本表，**物理上不可能碰到别的插件的版本状态**；
- 降级脚本只含本插件的表，天然不动别人；
- 卸载后可 `DROP TABLE alembic_version_plugin_<name>` 彻底清场（Alembic 不管这张空表）。

### Q2 结论

官方支持。多环境路线的隔离是物理隔离（不同版本表），多 base 是逻辑隔离（同表多行 + 标签寻址）。
卸载插件这种"整链清除"操作，多环境更稳。

---

## Q3：autogenerate 会不会把别的插件的表误判为「要删表」？

**答：默认一定会。这是 autogenerate 的基本行为，不是 bug；官方提供两个钩子防护，属必配项。**

### 官方证据

autogenerate.html「Omitting Table Names from the Autogenerate Process」：

> "Use `include_name` to ensure only tables present in the target MetaData are considered.
> **This prevents autogenerate from attempting to drop extraneous tables** from the database."

```python
def include_name(name, type_, parent_names):
    if type_ == "table":
        return name in target_metadata.tables
    else:
        return True
```

cookbook.html「Don't emit DROP TABLE...」给出等价的 `include_object` 版本：

```python
def include_object(object, name, type_, reflected, compare_to):
    # 数据库里反射到、但本地 metadata 没有对应物的表 → 不许进入比较，自然不会生成 drop
    if type_ == "table" and reflected and compare_to is None:
        return False
    return True
```

两者语义差异（api/runtime.html）：`include_name` 只看名字、在反射早期生效、可减少反射开销；
`include_object` 拿到完整对象与 `reflected`/`compare_to` 上下文，判断力更强。可叠加使用。

### 多 MetaData 的官方行为

autogenerate.html「Autogenerating Multiple MetaData collections」：

> "target_metadata may be defined as **a sequence of these collections**...
> `target_metadata = [Model1Base.metadata, Model2Base.metadata]`
> Each MetaData collection **must have unique table keys**."

即 env.py 可以 `target_metadata = [core_metadata, plugin_a_metadata, ...]`，Alembic 会把
列表并集当作"期望世界"。两个硬约束：
1. **表名（table key）全局唯一**，重名直接报错——插件表必须强制前缀（如 `plugin_<name>_*`）
   或落独立 schema；
2. 列表是"当时可见的插件集合"：某插件被卸载后其 metadata 不再进列表，若其表还在库里，
   autogenerate 立即视为 extraneous → 生成 DROP。**这就是插件场景误删的主通道。**

### 本仓库的既有印证

`database/registry.py:1-5` docstring 明言"遗漏任一模块会导致自动迁移误判为删表"——
单链单 metadata 下该问题已被项目自己确认过；`alembic/env.py:32,45` 已用 `include_name`
排除 `alembic_version_history` 表（database/migration_history.py:20-27），机制现成。

### Q3 结论与防护清单

会误删，且触发条件在插件场景必然出现（生成迁移时另一插件未安装/未导入）。防护是硬性前提：
1. 每插件表强制命名前缀（或独立 schema），使"归属"可由名字判定；
2. env.py 的 include_name/include_object 按前缀白名单过滤：本插件的 autogenerate 只看
   本插件前缀的表 + 忽略一切 reflected-only 表（cookbook 配方直接可用）；
3. 多环境路线下每插件 env.py 只装本插件 metadata，配合前缀过滤后，别的插件表在比较世界里
   根本不可见；
4. 人审仍是官方立场：autogenerate 产物 "please adjust!"，插件迁移脚本入库前必须人工审查
   有无越权 DROP。

---

## Q4：如果 Alembic 做不到，退路是什么？

Alembic 做得到（Q1-Q3 已证），以下退路按"限制插件能力换简单性"排序，均为真实可选项：

### 退路 1：插件不准建表，只准用宿主提供的 JSONB 附属表

宿主提供 `plugin_data(plugin_name, key, value JSONB, ...)` 一张通用表，插件读写 JSONB。
- 代价：失去外键/唯一约束/列级索引（JSONB GIN 索引只能救部分查询）；复杂插件
  （如自带工作流状态机的）表达力不足；所有插件数据挤一张表，量大后互相拖累。
- 收益：零迁移问题，卸载 = `DELETE WHERE plugin_name=...`，与本仓库软删除机制
  （database/engine.py:35-57 的全局过滤）天然兼容。
- 适用：轻量配置型插件；可与推荐方案并存（简单插件走 JSONB，重插件走独立链）。

### 退路 2：每插件独立 PostgreSQL schema

插件表建在 `plugin_<name>` schema 下，卸载 = `DROP SCHEMA ... CASCADE`。
- Alembic 侧配套齐全：`version_table_schema` 可把版本表也放进插件 schema
  （api/runtime.html）；autogenerate 需 `include_schemas=True` + include_name 按
  schema 白名单过滤（autogenerate.html 有 schema_qualified_table_name 完整示例）。
- 代价：本仓库当前全部表在默认 schema，ORM 需给插件实体加 `__table_args__ = {"schema": ...}`；
  跨 schema 外键可行但 `DROP SCHEMA CASCADE` 会连带删掉外部依赖对象，需禁止核心表
  外键指向插件 schema；Supabase pooler 下 search_path 行为需实测。
- 收益：卸载最干净（一条 DDL 清场），归属由 schema 物理表达，不靠命名纪律。
- 注意：这不是"绕开 Alembic"的退路，而是推荐方案的加固变体，可叠加。

### 退路 3：插件自带建表 SQL，不走 Alembic

插件带 `install.sql`/`uninstall.sql`（或按版本号排序的 SQL 目录 + 自记版本表）。
- 代价：自造迁移引擎——版本记录、断点续跑、事务边界、downgrade 顺序全要自己写并自己踩坑；
  与主链的 `alembic_version_history` 审计（0018_add_alembic_version_history.py）脱节；
  违背仓库"提供显式 Alembic upgrade/downgrade 迁移"的既有约定（CLAUDE.md 架构第 7 条）。
- 收益：插件作者不需要懂 Alembic；宿主零配置。
- 结论：除非插件生态面向完全外部的第三方作者，否则不值得。

---

## 推荐方案（明确、无条件）

**采用"程序化多环境"：每插件一条完全独立的迁移链 + 独立版本表，宿主统一驱动。**

结构与机制：
1. 核心链保持现状不动（`alembic/versions/` 21 个 revision、`alembic_version` 表、
   env.py 单 Base.metadata），继续用现有 CLI 流程。
2. 每插件包内自带：`migrations/` 版本目录、独立 `PluginBase(DeclarativeBase)` 或独立
   `MetaData`、表名强制 `plugin_<name>_` 前缀。
3. 宿主插件管理器程序化构造配置（官方 api/config.html 模式）：
   `Config()` + `set_main_option("script_location", <插件migrations路径>)`，
   共享一个通用 env.py 模板，`context.configure(version_table=f"alembic_version_plugin_{name}",
   target_metadata=<该插件metadata>, include_name=<按前缀白名单>)`。
4. 安装/启动：对每个已启用插件依序 `command.upgrade(cfg, "head")`（每链单 head，
   `upgrade head` 在环境内永远合法，不受多 base 的 heads 问题影响）。
   卸载：`command.downgrade(cfg, "base")` + 删除自己的版本表。
5. autogenerate 供插件开发用：同一套程序化 Config 跑 `command.revision(autogenerate=True)`，
   include_name 白名单保证只看见自己前缀的表。
6. `main.py:24-26` 的 `create_tables` 开发捷径只覆盖核心 Base.metadata，插件表一律走
   自己的迁移链（或在 lifespan 里对启用插件循环 upgrade）。

选它而不选多 base 的理由：插件"装卸不影响别人"的需求与官方对两条路线的分界原文完全对应
（多 base 为"cross-dependencies"设计并共享版本表；多环境为"completely independent"设计、
各自版本表）。插件恰恰要禁止跨链依赖，多 base 的唯一优势（depends_on）正是插件场景要
禁用的东西，而它的成本（upgrade head 失效、共享版本表多行、共享 target_metadata）全要照付。

### 推荐方案的失败模式（如实列出）

1. **跨插件外键无迁移期保障**：插件 A 的表若外键指向核心表，A 的迁移必须在核心链之后跑；
   宿主若乱序执行则建表失败。缓解：宿主固定"先核心后插件"的执行顺序，插件声明
   `requires_core_revision >= X` 由宿主在 upgrade 前校验（Alembic 本身不校验跨环境依赖，
   这是该路线让渡掉的能力）。**插件之间的外键应直接禁止**。
2. **表名前缀纪律失守**：某插件不守前缀建了 `users_ext`，另一插件 autogenerate 的白名单
   之外出现无主表，或与核心表重名冲突（多 MetaData 的 unique table keys 约束报错）。
   缓解：插件加载时校验其 metadata 中所有表名前缀，不合规拒绝加载。
3. **半程失败的卸载**：downgrade base 中途抛错（如插件表被外部对象依赖），版本表残留
   中间状态。PostgreSQL 事务性 DDL 使单个 revision 原子，但多 revision 的降级不是整体
   原子的；重跑 downgrade 可续，但"卸载"语义要求宿主把该插件标记为"卸载失败待处理"而非
   直接移除代码，否则迁移脚本一丢，残表永远无法用 Alembic 清理。
4. **孤儿表**：插件代码先被删、库没 downgrade——独立版本表 `alembic_version_plugin_x`
   和数据表全部变孤儿。核心链的 autogenerate 有 include_name/include_object 防护后
   不会去删它们（这是防护的代价：不删=永久残留），需要运维工具列出"版本表存在但插件
   不存在"的孤儿并人工处置。
5. **alembic_version_history 审计缺口**：现有 on_version_apply 审计
   （alembic/env.py:46，database/migration_history.py:30-66）只挂在核心 env.py；
   插件环境的通用 env.py 模板若不挂同一钩子，插件迁移无审计记录。缓解：模板统一挂钩子，
   history 表加 plugin_name 列或复用 revision 前缀区分。
6. **create_tables 开发模式漂移**：开发者靠 `AGENTOS_CREATE_TABLES=true`（main.py:24-26）
   建核心表，插件表没人建，开发环境行为与生产（纯迁移）不一致。缓解：lifespan 里对启用
   插件统一跑 upgrade，开发/生产同路径。

---

## 引用清单

官方文档（Alembic 1.19.1，经 Context7 /websites/alembic_sqlalchemy 查询并原文核对）：
- branches.html —— Working with Branches / Working with Multiple Bases /
  Setting up Multiple Version Directories / Running with Multiple Bases /
  Branch Dependencies；alembic_version 多行 SQL 语义；upgrade head 多头 FAILED 提示
- cookbook.html —— Run Multiple Alembic Environments from one .ini file（独立版本表 +
  --name + 可程序化前端）；Don't emit DROP TABLE（include_object 配方）
- autogenerate.html —— Autogenerating Multiple MetaData collections（列表 + unique
  table keys）；Omitting Table Names（include_name 防误删原文）
- api/runtime.html —— EnvironmentContext.configure 的 version_table /
  version_table_schema / include_name / include_object / on_version_apply；
  MigrationContext.get_current_heads 多头元组
- api/config.html —— Config 程序化构造（set_main_option）

仓库证据：
- alembic.ini:2-4（script_location=alembic、path_separator=os、无 version_locations）
- alembic/env.py:7,25,32,45-46（registry 副作用导入、单 Base.metadata、include_name、
  on_version_apply）
- database/registry.py:1-37（31 实体静态汇总；docstring 自证"遗漏即误判删表"）
- database/engine.py:14（副作用导入 registry）、35-57（全局软删除过滤）
- database/migration_history.py:20-27（include_alembic_name）、30-66（record_version_apply）
- alembic/versions/ —— 21 文件全部 branch_labels=None/depends_on=None，纯线性；
  head=0020_agent_mvp_comments；d5245f5576f8 插于 0007→0008 之间
  （0008_create_workspaces_table.py:12）
- main.py:24-26（lifespan create_all 仅覆盖 Base.metadata）、core/config.py:59
  （create_tables 默认 False）
- pyproject.toml（alembic>=1.17.2）
