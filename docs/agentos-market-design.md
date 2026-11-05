# Agent 市场与 Skill 市场设计

用户可以把自己工作区里的 Agent、Skill 提交上架。平台管理员审核通过后，全平台都能浏览；
安装就是把上架时的快照复制进自己的工作区。官方预置内容放在代码仓库里，启动时同步进市场。

参考了 HugAgentOS 的开源版实现，并避开了它的几个问题：
- 上架申请和市场条目是同一行，导致上架后无法更新。
- Agent 快照里存的是作者私有 Skill 的 ID，别人安装时依赖会丢光。
- 安装不在一个事务里，中途失败会留下孤儿数据。
- 下架只是从列表里隐藏，详情页和安装接口照常可用。

## 0. 已拍板的决策（2026-11-04）

1. 上架需要平台管理员审核。
2. 平台管理员是**整个平台第一个注册的用户**。不按工作区算：每个人注册时都会成为自己个人工作区的
   owner，按工作区算就人人都是管理员了。
3. Agent 绑定的 Skill，内容随 Agent 一起存进快照，安装时不依赖市场里的其他条目。
4. 升级是原地升级：直接覆盖，不检测本地改动，也不询问。
5. 安装时遇到同名就拒绝，不提供改名参数。
6. 官方预置内容放在代码仓库里，启动时同步进市场，不经过审核。
7. 本期不做：
   - 评分、收藏、评论；
   - 按组织或角色限定可见范围；
   - 站内通知；
   - 回滚到旧版本；
   - 批量操作；
   - Agent 导入导出（另案搁置）；
   - 修复 `/users` 接口越权。

## 1. 平台管理员

- `users` 表新增列 `is_platform_admin`：bool，NOT NULL，默认 false，列注释为「是否平台管理员（平台首个注册用户）」。
- **注册时判定**：
  - 在注册事务里先取一个固定键的 `pg_advisory_xact_lock`，再检查 `users` 表是否为空（软删除的行也算），为空就把新用户设为平台管理员。
  - 加锁是为了防止两个人同时注册时都被判成第一个用户。
  - 这条规则属于 user 域，写成 UserService 的公开方法，由 auth 的注册流程调用。现有注册流程直接使用 UserRepository，这是历史遗留问题，本次不重构。
- **迁移回填**：把 id 最小且未软删除的用户设为平台管理员；没有用户就不设。
- **字段只读**：不提供修改这个字段的接口，现有的用户创建、更新请求也不能接收它。在「当前用户」的响应里增加只读字段 `is_platform_admin`，前端据此决定是否显示审核页。
- **权限依赖**：`api/deps.py` 新增 `get_current_platform_admin`，要求用户有效且 `is_platform_admin=true`，否则返回 403「需要平台管理员权限」。这个依赖不看工作区。

## 2. 表结构（market 域，迁移 0028）

所有表都继承 Base 的审计字段和软删除字段。表和列都写中文注释，外键写明 `ondelete`。

### market_listings 市场条目

| 列 | 说明 |
|---|---|
| kind | `agent` / `skill`，用 CHECK 约束 |
| is_official | 是否官方预置 |
| official_key | 官方预置的目录名，社区条目为空；`(kind, official_key)` 建部分唯一索引，条件为 official_key 非空 |
| publisher_workspace_id | 发布条目的工作区，官方条目为空；外键 workspaces，ondelete CASCADE |
| source_agent_id / source_skill_id | 源对象；外键 ondelete SET NULL；各自建部分唯一索引，条件为非空，保证同一个源只对应一个条目 |
| status | `unlisted`：还没有审核通过的版本；`listed`：已上架；`delisted`：作者下架；`removed`：平台管理员下架，或官方预置已从代码中删除。用 CHECK 约束 |
| status_reason | 下架原因，可空 |
| current_version_number | 当前版本号，即最新一个审核通过的版本；没有通过的版本时为空 |
| name / summary / category | 当前版本的展示字段冗余在这里，审核通过时从版本复制过来，方便列表查询和搜索 |
| created_by_user_id | 外键 users，官方条目为空 |

### market_versions 版本

| 列 | 说明 |
|---|---|
| listing_id | 外键，指向 market_listings，ondelete CASCADE |
| version_number | 版本号，从 1 开始递增；`(listing_id, version_number)` 唯一 |
| status | 用 CHECK 约束，取值有四种：`pending` 待审、`approved` 通过、`rejected` 驳回、`withdrawn` 撤回。另建部分唯一索引 `(listing_id) WHERE status='pending'`，保证每个条目最多只有一个待审版本 |
| category | 提交时选择的分类 |
| release_note | 版本说明，可空 |
| snapshot | 快照，JSONB 类型，创建后不可变 |
| snapshot_hash | 快照的 sha256。计算方式：先把快照规范化为 JSON（`sort_keys`、紧凑分隔符、`ensure_ascii=False`），再取哈希 |
| submitted_by_user_id | 提交人；官方条目为空 |
| reviewed_by_user_id / reviewed_at / review_comment | 审核人、审核时间、审核意见。驳回时必须填写理由 |

### market_installs 安装记录

| 列 | 说明 |
|---|---|
| listing_id | 外键，指向 market_listings，ondelete CASCADE |
| workspace_id | 外键，指向 workspaces，ondelete CASCADE |
| installed_version_number | 已安装的版本号 |
| target_agent_id / target_skill_id | 安装出来的对象，外键，ondelete SET NULL |
| managed_skill_ids | JSONB 列表，记录安装 Agent 时新建或恢复的 Skill。升级时只覆盖这些 Skill |
| installed_by_user_id | 安装人 |

- `(listing_id, workspace_id)` 上建部分唯一索引，条件为 `is_deleted=false`。
- Agent 和 Skill 都是软删除，软删除不会触发外键的 SET NULL。所以判断一条安装是否仍然有效，要请 agent 域或 skill 域确认目标对象还在。
- 如果目标对象已被删除，这条安装记录视为未安装；重新安装时复用这一行。

## 3. 快照格式

Skill 快照与 skill 域的 SkillBundle 字段一致：

```json
{"name": "data-cleaning", "content": "<SKILL.md 原文>", "extra_files": {...}, "skill_hash": "..."}
```

Skill 条目的快照：

```json
{"skill": {<Skill 快照>}}
```

Agent 条目的快照：

```json
{
  "agent": {"name": "...", "description": "...", "system_prompt": "...", "tool_keys": ["Read", "Grep"]},
  "skills": [{<Skill 快照>}, ...]
}
```

- `tool_keys` 取自 Agent 绑定的工具，使用 tool_key。运行时会自动追加 Skill、SkillResource、SkillRun 三个工具，但它们不在绑定表里，所以不会出现在这里。
- `skills` 包含 Agent 绑定的全部 Skill，已禁用的也算。
- 快照不带 model_name、is_enabled、各类 ID、工作区和时间戳。作者用的模型在别的工作区多半不存在，安装后使用工作区的默认模型。
- 快照序列化后不得超过 32MB，超过返回 400。上限写成代码常量，不加配置项。
- **凭据文件拦截**：Skill 的 extra_files 里，只要有文件名命中下面任意一个，就返回 400，并在错误里写明文件路径：
  - `.env`、`.env.*`
  - `secrets.json`、`credentials.json`
  - `*.pem`
  - `id_rsa`、`id_ed25519`
- 快照在提交那一刻生成。审核人看到的就是最终上架的内容，作者之后再改源对象，不影响已经提交的版本。

## 4. 分类

分类是写在代码里的常量，接口 `GET /api/market/categories` 返回 key 和中文名：

| key | 中文名 |
|---|---|
| general | 通用 |
| coding | 编程开发 |
| data | 数据分析 |
| writing | 写作创作 |
| office | 办公效率 |
| education | 学习教育 |
| other | 其他 |

## 5. 社区上架流程

社区接口要求调用者是**当前工作区**的 owner 或 admin，口径与 Agent、Skill 的写权限一致。当前工作区取自 JWT。

### 提交新条目或新版本：`POST /api/market/submissions`

请求体：`kind`、`source_id`、`category`、`release_note`（可选）。

1. 源对象必须属于当前工作区，且未被删除。
2. 生成快照，然后依次校验快照大小、凭据文件和分类。
3. 锁定这个源对象对应的条目；条目不存在就新建一个，状态为 `unlisted`。
4. 条目下已有 `pending` 版本时，返回 409。
5. 快照哈希与当前已通过的版本相同时，返回 409「内容与当前上架版本相同」。
6. 新建一个 `pending` 版本，版本号取该条目现有最大版本号加 1。
7. 并发提交时可能撞上部分唯一索引（新建条目时撞源对象索引，或撞「每个条目只能有一个待审版本」的索引），这时同样返回 409，不能报 500。
8. 条目状态为 `removed` 时也允许提交新版本，审核通过后重新上架。

### 撤回待审版本：`POST /api/market/versions/{version_id}/withdraw`

只能撤回 `pending` 状态的版本，撤回后状态变为 `withdrawn`。

### 下架：`POST /api/market/listings/{listing_id}/delist`

- 只能对 `listed` 的条目操作，下架后状态变为 `delisted`。
- 官方条目不适用这个接口。

### 重新上架：`POST /api/market/listings/{listing_id}/relist`

- 只能从 `delisted` 恢复为 `listed`。
- `removed` 状态的条目不能用这个接口恢复，只能提交新版本，审核通过后重新上架。
- 官方条目不适用这个接口。

### 我的上架：`GET /api/market/mine`

返回当前工作区的全部条目，每个条目附带全部版本，包括审核意见和驳回理由。

## 6. 平台管理员审核

审核接口都使用 `get_current_platform_admin`，不看工作区。

- **待审列表**：`GET /api/market/admin/versions?status=pending&limit&offset`。返回版本的快照全文、所属条目，以及发布工作区的名称。
- **通过**：`POST /api/market/admin/versions/{version_id}/approve`
  1. 锁住条目这一行，防止同一条目的两个版本同时审核。
  2. 版本必须是 `pending`。
  3. 版本状态改为 `approved`，写入审核人和审核时间。
  4. 用该版本更新条目的 `current_version_number`、`name`、`summary`、`category`。
  5. 条目状态改为 `listed`，原状态是 `unlisted`、`delisted` 还是 `removed` 都一样。
  - `summary` 的来源：Agent 取 description，Skill 取 frontmatter 里的 description。
- **驳回**：`POST /api/market/admin/versions/{version_id}/reject`。请求体里的 `reason` 必填，版本状态改为 `rejected`。
- **强制下架**：`POST /api/market/admin/listings/{listing_id}/remove`
  - 请求体里的 `reason` 必填，条目状态改为 `removed`，原因写入 `status_reason`。
  - 官方条目返回 409「官方内容请从代码中移除」。

## 7. 浏览

- 浏览接口要求用户已登录、且是当前工作区的有效成员。
- **分页列表**：`GET /api/market/listings?kind&category&keyword&limit&offset`
  - 只返回 `listed` 状态的条目。
  - `limit` 默认 20，取值范围 1–100。
  - 按 `is_official desc, updated_at desc, id desc` 排序。
  - `keyword` 对 name 和 summary 做 ilike 匹配。
  - 每个列表项包含以下字段：
    - id、kind、name、summary、category、is_official、current_version_number；
    - publisher_name：官方条目显示「官方」，社区条目显示发布工作区的展示名，展示名通过 WorkspaceService 获取；
    - install_count：未软删除的安装记录数；
    - installed：当前工作区是否有有效安装；
    - installed_version_number；
    - has_update：已安装、且已安装版本低于当前版本时为 true。
- **详情**：`GET /api/market/listings/{listing_id}`
  - `listed` 状态的条目所有人可见；不是 `listed` 的，只有发布工作区的成员和平台管理员能看到。
  - 返回当前版本的完整快照，包括系统提示词和 Skill 全文。用户应该在安装前看清 Skill 里的脚本。
  - 同时返回当前工作区的**安装预览**：
    - 没装过：安装时会发生什么，包括 Agent 是否撞名、每个工具是绑定还是丢弃（及原因）、每个 Skill 是新建、恢复、复用还是冲突；
    - 已装过：升级时会发生什么。
  - 预览和实际安装、升级必须用同一套判定代码（预览只是不写库），保证预览看到的就是实际会发生的。

## 8. 安装：`POST /api/market/listings/{listing_id}/install`

- 调用者必须是当前工作区的 owner 或 admin，条目必须处于 `listed` 状态。
- 当前工作区已有这个条目的安装记录、且安装出来的对象还在时，返回 409「已安装，请使用升级」。
- 整个安装复用请求事务（get_db），任何一步失败都整体回滚。Service 和 Repository 都不能自行 commit。

**Skill 条目**
- 本工作区已有同名、未删除的 Skill：返回 409，拒绝安装。
- 有同名但已软删除的 Skill：原地恢复，并写入快照内容。沿用 skill 域现有的「同名软删记录原地复活」逻辑。
- 都没有：新建。

**Agent 条目**，按下面的顺序处理：
1. 本工作区已有同名、未删除的 Agent：返回 409。
2. 工具：按 tool_key 找到工具 ID。找不到、已停用、或 user_agent 档位不允许绑定的工具都丢弃，并在报告里写明原因。判定口径要与现有绑定校验一致。
3. Skill：
   - 没有同名的：新建；
   - 有同名但已软删除的：恢复；
   - 有同名未删除的，且 skill_hash 相同：直接复用；
   - 有同名未删除的，但内容不同：返回 409，拒绝整个安装，并指明是哪个 Skill 冲突。
4. 调用现有的 `AgentService.create_agent` 创建 Agent，`model_name` 为空，`is_enabled` 为 true。这样能复用它的全部校验。
5. 写入安装记录。`managed_skill_ids` 只记录这次新建或恢复的 Skill。

**返回值**：安装记录加一份报告。报告里有：
- 工具：哪些已绑定，哪些被丢弃、原因是什么；
- Skill：每个 Skill 的名称和处理结果（新建 / 恢复 / 复用）。

## 9. 升级：`POST /api/market/listings/{listing_id}/upgrade`

- 调用者必须是当前工作区的 owner 或 admin，条目必须处于 `listed` 状态。
- 当前工作区必须有有效的安装记录，否则返回 409。
- 已安装的版本号必须小于当前版本，否则返回 409「已是最新版本」。
- 升级在一个事务里完成，返回格式与安装相同的报告。
- 升级直接覆盖，不检测本地改动。

**Skill 条目**：用新快照覆盖已安装 Skill 的内容。Skill 名称创建后不能修改，所以同一个条目的 Skill 名称不会变。

**Agent 条目**
- 覆盖 description、system_prompt、工具绑定和 Skill 绑定。
- 保留本地的 name、is_enabled 和 model_name。名称是工作区内的唯一标识，跟着改可能撞名；启用状态和模型属于本地设置。
- 工具重新解析，不可用的丢弃。
- Skill 的处理：
  - 在 `managed_skill_ids` 里且仍存在的：直接用新内容覆盖；
  - 其余的：按安装规则处理（新建、恢复，或内容相同时复用）；内容不同则返回 409，拒绝整个升级；
  - 新版本已经不再包含的托管 Skill：只解绑，不删除，因为可能还有其他 Agent 在用；同时把它从 `managed_skill_ids` 里移除。
- 托管 Skill 被覆盖后，绑定它的其他 Agent 也会受影响。这是原地升级的预期行为，报告里要列出被覆盖的 Skill。

**最后**：更新 `installed_version_number` 和 `managed_skill_ids`。

已安装的 Agent 或 Skill 用现有接口删除，就相当于卸载，不另设卸载接口。

## 10. 官方预置

**目录结构**

```
market/presets/
├── skills/<skill-name>/SKILL.md     # 加上其他资源文件；目录名必须等于 SKILL.md 里的 name
└── agents/<key>/AGENT.md
```

**AGENT.md 的写法**：开头是一段 YAML frontmatter，字段有 `name`、`description`、`category`、`tools`、`skills`；frontmatter 之后的正文就是系统提示词。
- `tools`：tool_key 列表。
- `skills`：Skill 名称列表，引用 `presets/skills` 下的目录。

**Skill 预置的分类**：写在 SKILL.md frontmatter 的 `metadata.market_category` 里，不写时为 `other`。Skill 目录必须经过 skill 域现有的解析和校验，比如在内存里打成 zip 再交给 `parse_skill_zip`。

**同步时机**：放在 `main.py` 的 lifespan 里，排在内置工具目录同步之后，因为同步时要校验 tool_key。
- 同步使用独立的 session，自己 commit。
- 失败时回滚，并中止启动。这和工具目录同步的处理方式一致：预置内容是代码的一部分，出错就应该在启动时暴露。

**同步逻辑**：先取 `pg_advisory_xact_lock`，防止多个进程同时启动时互相冲突，再逐个处理：

| 情况 | 处理 |
|---|---|
| 代码里有，库里没有 | 新建条目（`is_official`、`official_key`，状态 `listed`），并新建版本 1（`approved`，审核人为空） |
| 库里已有，快照哈希不同 | 新增一个 `approved` 版本，并更新当前版本号和展示字段 |
| 库里已有，哈希相同 | 内容不动。如果条目是 `removed`（之前从代码里删过），恢复为 `listed` |
| 库里有，代码里已删除 | 状态改为 `removed`，已经安装出去的副本不受影响 |

Agent 预置的 `tools` 必须都在工具目录里，`skills` 必须都在 `presets/skills` 里。不满足时启动失败，错误里要指明是哪个预置的哪个字段。

**首批内容**：一个示例 Skill 和一个示例 Agent，用来演示格式、跑通同步。用户可以替换或删除。

## 11. 代码落点

| 位置 | 改动 |
|---|---|
| `market/`（新域） | 包含 `models.py`、`schemas.py`、`repository.py`、`service.py`、`api.py`、`presets.py`（读取预置目录并同步），以及存放预置内容的 `presets/` 目录 |
| `user/`、`auth/` | 新增 `is_platform_admin` 列和「是否第一个注册用户」的判定；当前用户的响应里加上这个字段 |
| `api/deps.py` | 新增依赖 `get_current_platform_admin` |
| `agent/service.py` | 新增四个公开方法：生成 Agent 快照、判断同名、按 ID 批量判断是否存在、用于升级的覆盖（尽量复用现有的更新和绑定替换方法） |
| `skill/service.py` | 新增公开方法：生成 Skill 快照；按快照判定新建、恢复、复用还是冲突，并执行；按快照覆盖内容；按 ID 批量判断是否存在 |
| `tools/service.py` | 新增 tool_key 与工具 ID 的双向转换，同时给出不可用原因，判定口径与 `require_bindable_tool_ids` 一致 |
| `workspace/service.py` | 新增按 ID 批量获取展示名 |
| 注册入口 | `database/registry.py` 登记三张新表；`api/router.py` 挂载 `/market` |
| 迁移 `0028` | 包括 `users.is_platform_admin`、回填、三张新表，并写显式的 downgrade |

**约束**
- 跨域只走 Service 调 Service：market 域不能直接用别的域的 Repository，也不能自己拼别的域的表的 SQL。
- 不加配置项，不加依赖（pyyaml 已经有了）。

## 12. 测试要求

- **纯函数单元测试**：
  - 快照生成和哈希；
  - 凭据文件拦截；
  - 快照大小上限；
  - 状态流转中的非法操作被拒；
  - 预置目录解析，包括格式错误、引用不存在的 tool 或 Skill。
- **真实 PostgreSQL 集成测试**：
  - 首个注册用户成为平台管理员，第二个用户不是；
  - 跨工作区安装 Agent，覆盖 Skill 新建、恢复、复用、冲突四种情况；
  - 安装中途失败时整体回滚；
  - 同名 Agent 和同名 Skill 被拒；
  - 升级：
    - 覆盖托管 Skill；
    - 解绑新版本里已移除的 Skill；
    - 保留本地的名称和启用状态；
  - 两个版本同时审核时只有一个生效；
  - 已有待审版本时再次提交被拒；
  - 下架后，详情、安装和升级都被拒；
  - 普通用户调用审核接口返回 403；
  - 预置同步的四种情况。
- **测试改动范围**：可以新增测试文件，但不能修改已有测试。

## 13. 编码规范（用户要求，原文）

```
核心原则:易读、易懂、易维护,优先考虑可读性,而不是追求代码简短
  1.减少封装:不要过度拆分函数，只有职责独立、确实有复用价值的逻辑才单独封装
  2.减少修饰和包装:避免不必要的装饰器、类、继承等，只保留确实有必要的设计
  3.减少嵌套:优先用提前返回，减少多层if、try、with嵌套，让逻辑保持扁平
  4.优先用直观语法:多用普通的if/else、for等，少用复杂推导式、三元表达式
  5.避免过度压缩代码:宁可多写几行，也要把执行步骤和逻辑表达清楚
  6.注释用中文:重点解释代码目的、设计原因，不要重复代码本身
  7.命名清晰:变量、函数名尽量直观，不用反复跳转就能理解作用
  8.保持原有功能:不随意删减功能、改变业务逻辑、修改接口
  9.复杂机制优先保证正确性:涉及异步、并发、数据安全时不破坏原有行为
  10.控制优化范围:不引入不必要的第三方依赖，不增加多余抽象层
  最终让新人能顺着代码从上到下看懂逻辑！
```
