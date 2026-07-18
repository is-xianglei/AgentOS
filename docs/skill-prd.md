# AgentOS Skill 系统 PRD(产品与实现规格)

## 0. 本文档怎么用

本文面向**没有任何项目上下文的实现者(人或 LLM)**。读完即可从零实现 AgentOS 的
Skill(技能)系统,无需再问设计问题。文档给出:背景、设计决策与依据、项目现有约定、
逐字段的数据模型、完整的迁移脚本、每个模块的签名与行为、接口契约、安全要求、验证清单、
以及严格的实施顺序。

约定:
- 所有代码路径均相对仓库根 `/`(本项目已扁平化,**导入一律无 `app.` 前缀**,
  如 `from tools.base import BaseTool`、`from core import config`)。
- 文中"必须 / MUST"为硬性要求,"建议 / SHOULD"为推荐但可权衡。
- 代码示例是目标状态(target state):即使某文件已存在半成品,也以本文为准覆盖实现。
- 文档用简体中文;代码标识符用英文。

---

## 1. 背景与目标

**Skill(技能)** 是一个可被上传、复用的"能力包",对齐 Anthropic 的 Agent Skills / SKILL.md
规范。一个 skill = 一段带 YAML frontmatter 的 Markdown 指令正文(`SKILL.md`)+ 可选的附属
资源目录(`references/` 参考文档、`scripts/` 脚本、`assets/` 模板/图片)。

AgentOS 是一个 **web 版多会话 Agent 运行时**(服务端 Python/FastAPI + PostgreSQL)。与官方
"本地目录"形态不同,本项目的 skill 由用户通过 web 接口**上传**,因此需要落库与对象存储。

**本期目标(v1):**
1. 提供 skill 的上传(打包 zip)、校验、列表、详情、删除接口。
2. 让运行时的主代理能**发现**已上传的 skill(name + description 注入系统提示)。
3. 让模型能**按需加载** skill 正文与其引用的资源文件。
4. 资源文件存对象存储(MinIO),正文与元数据的索引存 PostgreSQL。
5. 让模型能**执行** skill `scripts/` 下的脚本(进程内子进程 + 软限制,见 §3.5)。

**本期不做(明确排除):**
- **不做强隔离沙箱**(但**支持执行**):`scripts/` 脚本本期**可执行**,采用**进程内子进程 +
  软限制**(超时、临时工作目录物化、解释器白名单),不引入容器 / OS 级沙箱。执行经统一的
  `Executor` 抽象封装,将来叠加容器 / nsjail 等强隔离时**零返工**(见 §3.5)。
  ⚠️ 进程内执行 = 在服务主机上运行任意代码,**等同信任上传者**,仅限**单机 / 内网可信部署**;
  多租户 / 生产环境须先接入沙箱再开放执行。
- 不做多用户/多租户隔离(项目当前无用户体系),skill 全局共享。
- 不做 skill 版本历史表(同名覆盖更新)。

---

## 2. 术语表

| 术语 | 含义 |
|---|---|
| skill | 一个能力包,含 `SKILL.md` + 可选资源目录 |
| SKILL.md | skill 的入口文件:YAML frontmatter + Markdown 正文 |
| frontmatter | SKILL.md 顶部两个 `---` 之间的 YAML 元数据 |
| 正文 / body | SKILL.md 中 frontmatter 之后的 Markdown 内容 |
| 资源 / resource | skill 的附属文件,即 `references/`、`scripts/`、`assets/` 下的文件 |
| bundle | 一个 skill 的全部文件(SKILL.md + 资源),打包为 zip 上传 |
| 命名空间前缀 | 对象存储中每个 skill 以其 name 为前缀:`{name}/...`(桶 `agentos-skills` 内) |
| 发现 / discovery | 把 enabled skill 的 name+description 注入系统提示,让模型知道有哪些能力 |
| 渐进式披露 | 三层加载:① name+description 常驻 → ② 正文按需 → ③ 资源按需 |
| catalog | 供发现用的 skill 清单文本(name + description) |

---

## 3. 设计决策与依据(评审重点)

以下决策已确认,实现时**不要擅自更改**;每条附依据,便于评审。

### 3.1 内容存 MinIO,DB 只存索引与元数据(不存正文 body)

- **MinIO = 内容唯一真源**:整个 bundle 原样存,包含 `{name}/SKILL.md` 及所有资源。
- **DB = 索引 + 管理元数据**:name、description、frontmatter(解析后)、version、
  content_hash、status、scope。**不存 body 列**,也不存可从 frontmatter 取值的 allowed_tools、
  可从 MinIO 重建的资源清单、可从 name 派生的 bundle 前缀,理由见 §6.1。
- **依据**:skill 有两种访问模式,频率天差地别——
  - **发现(高频)**:每轮对所有 enabled skill 取 name+description,必须快 → 存 DB。
  - **加载正文(低频)**:仅当模型决定使用某 skill 时读一次 → 从 MinIO 取即可,无需在 DB
    冗余一份。SKILL.md 本就是 bundle 的一个文件,DB 再存一份纯属重复。
  - 且执行时整个 `{name}/` 前缀会被物化到临时工作目录(§3.5),SKILL.md 自然跟随,
    内容只有 MinIO 一个真源,不存在 "DB 的 body 与物化文件不一致" 的风险。
- **一致性代价**:内容与元数据分属两存储,靠"上传时一次性写入 + content_hash/version 校验"
  兜住;此风险对 references/scripts 本就存在,body 挪过去不引入新类型风险。

### 3.2 统一命名空间 `{name}/{relative_path}`(正文零改写)

- 对象存储 key、模型看到的引用前缀、执行时物化路径**三层共用**同一前缀(即 skill 的 `name`)。
- **依据(LibreChat 生产实践)**:SKILL.md 正文里作者按官方规范写相对路径(如
  `references/cli.md`)。host 侧用"当前激活的 skill 名 + 相对路径"补全为
  `{name}/references/cli.md`,**无需改写正文**;执行时(§3.5)把该前缀物化到临时目录后,
  子进程与工具路由解析到同一路径。
- **无额外前缀**:桶 `agentos-skills`(§7.1)已专用于 skill,桶内直接以 `name` 分目录即可,
  不再套 `skills/` 一层——否则得到 `agentos-skills/skills/...`,"skills"重复出现纯属冗余。

### 3.3 发现走"系统提示注入",加载走"工具"

- **发现**:主代理每轮把 enabled skill 的 name+description 注入系统提示(渐进式披露第一层)。
- **加载正文**:`Skill(name)` 工具从 MinIO 取 SKILL.md 正文返回,并附资源清单+基准说明。
- **加载资源**:`SkillResource(skill_name, relative_path)` 工具从 MinIO 取单个资源。
- **执行脚本**:`SkillRun(skill_name, relative_path, args?, stdin?)` 工具把整个 bundle 物化到
  临时工作目录后,以子进程执行 `scripts/` 下脚本,返回退出码 + stdout/stderr(截断)。
- **依据**:官方所有形态都假定 "skill 是可被文件工具遍历、可被 bash 执行的目录";本项目当前
  无文件工具、无 bash,故用一组"专用工具"替代(官方实现指南明确:模型无文件/执行能力时提供
  专用工具)。纯工具方案的发现性弱(模型不主动查就发现不了),故 name+description 用系统提示
  常驻兜住。

### 3.4 单表、全局共享、同名覆盖更新

- 单张 `skills` 表,无 `session_id`/`user_id`,`scope` 字段默认 `"global"` 预留未来隔离。
- 上传同名 skill = 覆盖更新(含软删复活),不建版本历史表。
- **依据**:项目当前无用户/租户体系,唯一隔离维度是 session_id,但 skill 语义上是全局能力;
  `scope` 仿 `permission_rules` 表的双作用域范式预留。

### 3.5 scripts 本期可执行(进程内子进程 + 软限制)

本期**必须完整支持 skill 的执行能力**:模型能通过 `SkillRun` 工具执行 skill `scripts/` 下的
脚本。执行采用**进程内子进程**方案(不引入容器 / OS 级沙箱),辅以一组**软限制**收敛风险。

**执行模型:**
- **物化(materialize)**:执行前把该 skill 的整个 bundle 从 MinIO 拉取,镜像还原到一个
  **一次性临时工作目录**(如 `{tempdir}/{name}/...`,布局同 §3.2 命名空间),使脚本内的
  相对路径(`references/`、`scripts/`、`assets/`)可原样解析。执行完成后**无条件清理**该目录。
- **子进程**:用 `asyncio.create_subprocess_exec`(**不经 shell**,`argv` 数组传参,杜绝 shell
  注入)在临时工作目录内启动脚本;`cwd` 设为该 skill 的物化根。
- **解释器白名单**:按脚本扩展名映射到白名单解释器(`.py`→`python`、`.sh`→`bash`、`.js`→`node`
  等),映射表可配置(见 §7.2);**不在白名单则拒绝执行**,不做"猜测执行"。

**软限制(硬性要求):**
- **超时**:每次执行强制 `timeout`(默认见 §7.2),超时 `kill` 整个进程组并回收。
- **输出截断**:stdout/stderr 各自封顶(默认见 §7.2),超出截断并标注,防止爆内存 / 撑爆上下文。
- **参数白名单化传递**:`args` 以数组形式拼进 `argv`,`stdin` 以字节写入,**均不经 shell**。
- **环境变量最小化**:子进程环境**不继承**主进程敏感变量(如 `ANTHROPIC_API_KEY`、
  `DATABASE_URL`、`MINIO_*`),只下发一个最小白名单(`PATH`、`LANG` 等)。
- **路径约束**:`relative_path` 必须落在 `scripts/` 下,复用 §6.2 的相对路径正则并禁止 `..` 段;
  物化根之外的路径一律拒绝。

**Executor 抽象(为将来强隔离预留,零返工):**
- 执行经统一接口 `Executor.run(workdir, argv, stdin, timeout, env) -> ExecResult` 封装
  (详见后续 §"脚本执行层")。本期实现 `SubprocessExecutor`;将来接入容器 / nsjail 时新增
  `SandboxExecutor` 并在工厂切换,**上层 `SkillRun` 工具与 service 不改**。
- `SkillRun` 工具返回的基准说明中**显式注明**当前执行环境与限制(进程内、超时值、无网络策略
  说明等),让模型据此决策。

**⚠️ 安全边界(必须在文档与代码注释中如实声明):**
- 进程内子进程执行 = **在服务主机上运行上传者提供的任意代码**,软限制只降低误伤 / 失控概率,
  **不构成安全隔离**:恶意脚本仍可读写主机文件、发起网络请求、耗尽资源。因此本方案**等同于
  完全信任 skill 上传者**。
- **适用边界**:仅限**单机 / 内网可信部署**(上传者可信)。**多租户 / 公网生产环境严禁直接启用**,
  须先将 `Executor` 切换为强隔离实现(容器 / nsjail / gVisor)再开放。
- 建议提供全局开关 `SKILL_EXEC_ENABLED`(见 §7.2),默认可开,但部署到不可信环境时须显式关闭。

---

## 4. 项目现有约定(实现前必读)

实现者须严格沿用以下既有约定,保证新代码与项目风格一致。

### 4.1 目录结构与导入

项目已扁平化,包直接位于仓库根:`core/`、`tools/`、`services/`、`schemas/`、`models/`、
`repositories/`、`api/`、`db/`、`alembic/`,`prompts.py` 在仓库根。导入无 `app.` 前缀。

### 4.2 ORM 基类(`db/base.py`)

`Base(DeclarativeBase)` 已为所有子类下发四个审计列,新表**自动继承,不要重复声明**:

```python
class Base(DeclarativeBase):
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), comment="创建时间", sort_order=100)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(),
        comment="更新时间", sort_order=101)
    is_deleted: Mapped[bool] = mapped_column(
        Boolean, server_default=false(), default=False, index=True,
        comment="软删除标记", sort_order=102)
    deleted_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, comment="软删除时间", sort_order=103)
```

- 主键统一 `int` 自增(无 UUID 先例)。
- **软删除全局自动生效**:`db/session.py` 在 ORM SELECT 上挂了 `do_orm_execute` 事件,
  自动追加 `is_deleted = false` 过滤。新表无需任何改动即生效;读已删数据需显式
  `execution_options(include_deleted=True)`。
- 表名用复数蛇形(`skills`),`__table_args__` 带中文 `{"comment": "..."}`;每列写中文 `comment`。

### 4.3 JSONB 列写法(`models/session.py` 提供 `json_type()`)

```python
from sqlalchemy.dialects.postgresql import JSONB
def json_type():
    return JSONB()
```

三种用法:需就地变更追踪的 dict 用 `MutableDict.as_mutable(json_type())`;需追踪的 list 用
`MutableList.as_mutable(json_type())`;不需追踪的裸 JSON 直接 `json_type()`。本表的
frontmatter 用裸 `json_type()`(整体替换写入,不做就地变更)即可。

### 4.4 工具基类(`tools/base.py`)

```python
@dataclass
class ToolContext:
    session_id: int
    db: AsyncSession
    bus: StreamBus | None = None

class BaseTool(ABC):
    name: str
    description: str
    input_model: type[BaseModel]
    def to_param(self) -> ToolParam: ...   # 用 input_model.model_json_schema() 生成
    async def run_with_dict(self, data: dict, ctx: ToolContext) -> str: ...  # 校验后调 run
    @abstractmethod
    async def run(self, args: BaseModel, ctx: ToolContext) -> str: ...
```

- `BaseTool` 是普通 ABC(非 pydantic)。`name`/`description` 是类属性,`input_model` 指向
  独立的 pydantic 模型。`run` 是 **async,返回 `str`**(给 LLM 的文本)。
- 工具经 Anthropic **原生 tool-calling** 暴露(`to_param()`),**不拼进系统提示文本**。
- 结构化返回统一 `json.dumps(..., ensure_ascii=False)`,不 return 裸 dict。

### 4.5 工具注册(`tools/registry.py`)

硬编码工厂 `build_tool_registry() -> ToolRegistry`,内部 `ToolRegistry([EchoTool(), ...])`。
新增工具需两处登记:`tools/builtin/__init__.py` 的导出 + `build_tool_registry()` 列表。
`ToolRegistry` 有 `get(name)`、`to_anthropic_tools()`、`without(*names)`、`only(*names)`。

### 4.6 Repository / Service 事务分工

- Repository 持有 `AsyncSession`,方法内只 `add/flush/refresh`,**不 commit**。
- Service 负责 `commit` 与业务校验(状态用字符串集合校验,不用 DB 原生 enum)。

### 4.7 API 层

- `core/responses.py` 的 `ok(data, request)` 包装;`schemas/common.py` 的 `ApiResponse[T]`。
- 路由 `router = APIRouter()`;DB 用 `db: AsyncSession = Depends(get_db)`(来自 `api/deps.py`)。
- 在 `api/router.py` 用 `api_router.include_router(skills.router, prefix="/skills", tags=["skills"])`
  注册,并在其 `from api import ...` 补 `skills`。

### 4.8 配置(`core/config.py`)

无 pydantic-settings。用 `config.get(key, default)` / `config.get_int(key, default)` 读环境变量
(已 `load_dotenv`)。新增配置在此模块加模块级常量。

---

## 5. SKILL.md 与 bundle 格式规范

### 5.1 SKILL.md 结构

```
---
name: pdf-tools
description: "处理 PDF:提取文本、合并、拆分。当用户需要操作 PDF 文件时使用。"
version: 1.0.0
license: Apache-2.0
allowed-tools: [Read, Bash]
metadata:
  author: zhang
---

# PDF 工具

正文 Markdown……引用资源用相对路径,如 references/cli.md、scripts/extract.py。
```

- 首行必须是 `---`;frontmatter 是合法 YAML;第二个 `---` 之后为正文。
- frontmatter 字段(对齐官方规范):
  - `name`(**必填**):≤64 字符,正则 `^[a-z0-9]+(-[a-z0-9]+)*$`(小写字母/数字/连字符,
    不以连字符开头结尾、不连续连字符);**必须等于 bundle 里 skill 目录名**。
  - `description`(**必填**):非空,≤1024 字符。
  - `version` / `license` / `compatibility`(≤500) / `metadata`(string→string dict) /
    `allowed-tools`(可选):兼容行内列表 `[Read, Write]` 与 YAML 块列表两种写法。
- 未知顶层字段:**宽松处理**——不拒绝,原样保留在 frontmatter JSONB(便于官方字段演进)。

### 5.2 上传 bundle(zip)的目录结构

zip 内**必须有且只有一个顶层目录**,即 skill 目录,目录名 == frontmatter.name:

```
pdf-tools/                 (顶层目录,名字须等于 name)
├── SKILL.md               (必需)
├── references/            (可选)
│   └── cli.md
├── scripts/               (可选)
│   └── extract.py
└── assets/                (可选)
    └── template.docx
```

- 校验须定位 `{顶层目录}/SKILL.md`;找不到则报错。
- 允许 zip 内直接以 `SKILL.md` 在根(无顶层目录)的情形:此时 skill 目录名取 frontmatter.name,
  校验"目录名 == name"这一条对根形态跳过。实现二选一均可,但**必须在文档/错误信息中一致**。
  本 PRD 采用:**要求有顶层目录且名字等于 name**(更贴合官方,校验更强)。

### 5.3 对象存储布局

上传成功后,bundle 内每个文件按下述 key 存入 MinIO(镜像相对结构):

```
{bucket}/pdf-tools/SKILL.md
{bucket}/pdf-tools/references/cli.md
{bucket}/pdf-tools/scripts/extract.py
{bucket}/pdf-tools/assets/template.docx
```

其中 `{bucket}` 为 `config.MINIO_BUCKET`(默认 `agentos-skills`),`pdf-tools` 为 skill name;
对象 key 直接以 name 开头(`pdf-tools/...`),桶内不再套额外前缀。

---

## 6. 数据模型

### 6.1 `skills` 表(`models/skill.py` 的 `SkillRecord`)

| 字段 | 类型 | 约束/默认 | 说明 |
|---|---|---|---|
| id | int | PK 自增 | 主键 |
| name | String(64) | unique, index, 非空 | kebab-case;覆盖更新与工具路由的键 |
| description | Text | 非空 | ≤1024;注入系统提示供发现 |
| frontmatter | JSONB | 非空,default dict | 解析后的**完整** frontmatter 快照 |
| version | String(32) | nullable | frontmatter 声明的版本号 |
| content_hash | String(64) | nullable, index | bundle 内容哈希,幂等上传判重 |
| scope | String(16) | 非空,default "global" | 预留隔离维度,本期恒为 global |
| status | String(16) | 非空,default "enabled" | enabled / disabled;发现只列 enabled |
| created_at/updated_at/is_deleted/deleted_at | — | Base 继承 | 不要重复声明 |

说明:
- `name` 与 `description`/`version` 是从 `frontmatter` 抽出的**反范式冗余**,仅为发现热路径
  加速(每轮只取 name+description,不必反序列化整个 frontmatter);`frontmatter` 保留完整快照
  作真源(重叠是刻意的)。
- **不存 `allowed_tools`(允许工具列表)**:它就是 `frontmatter["allowed-tools"]`,与 frontmatter
  在**同一行**,重建只是一次内存字典取值(连字符键名→下划线,一个 helper 搞定),既非网络也非
  查询。没有任何访问路径会在不加载 frontmatter 时单独读它——发现热路径只用 name+description,
  工具裁剪发生在模型调 `Skill(name)` 之后(那时整行 frontmatter 已在手)。表上也未给它建索引、
  无"查哪些 skill 用工具 X"之类需求。故所谓"裁剪加速"是空的,纯冗余,删。
- **不存 `resources`(资源清单)**:清单可从 MinIO `list_prefix("{name}/")` 实时重建
  (size 取对象元数据,mime 用 `mimetypes.guess_type` 按扩展名重猜)。理由同 §3.1 拒绝存 body——
  资源本就在 MinIO(唯一真源),DB 再存一份纯属可过期的重复;按需重建保证读到的是**当前真实
  状态**,代价是 `Skill` 加载 / 详情接口各多一发 `list_prefix`(§3.1 已归为低频,可接受)。
- **不存 `bundle_prefix`(对象存储前缀)**:桶内 key 直接以 name 开头,前缀就等于 `{name}/`,
  即 skill 自己的 name,无任何额外信息可存。**派生字段不落库**,需要时现拼(§8.2)。

### 6.2 资源相对路径校验规范

bundle 内每个资源文件以**相对 skill 根的路径**标识(如 `references/cli.md`、`scripts/extract.py`),
该路径规范被 §3.5 `SkillRun`、`SkillResource` 工具及重建资源清单共用:

- 正则校验 `^[A-Za-z0-9._\-/]+$`,**禁止 `..` 段**(防目录穿越)。
- 重建清单时,单个资源元素形如
  `{"relative_path": "references/cli.md", "size": 8432, "mime": "text/markdown"}`:
  `size` 取 MinIO 对象元数据字节数;`mime` 由 `mimetypes.guess_type` 按扩展名推断,猜不出用
  `application/octet-stream`。此结构**不落库**,是接口 / 工具的运行时返回形态。

### 6.3 `models/__init__.py` 登记

在现有导入与 `__all__` 中加入 `SkillRecord`:

```python
from models.skill import SkillRecord
# __all__ 追加 "SkillRecord"
```

`alembic/env.py` 靠 `import models` 触发汇总 import,故此登记是新表被 metadata 收录的前提。

### 6.4 Alembic 迁移 `alembic/versions/0007_create_skills.py`

- `revision = "0007_create_skills"`,`down_revision = "0006_add_audit_fields"`,
  `branch_labels = None`,`depends_on = None`。
- 项目**手写迁移,不依赖 autogenerate**:审计四列也须在迁移中逐列写出。
- upgrade 建表 + 建索引(`ix_skills_name` unique、`ix_skills_is_deleted`、`ix_skills_content_hash`);
  downgrade 先 drop_index 再 drop_table。

迁移 upgrade 主体(照抄风格,`postgresql.JSONB()` 用于 JSONB 列):

```python
def upgrade() -> None:
    op.create_table(
        "skills",
        sa.Column("id", sa.Integer(), primary_key=True, comment="skillID"),
        sa.Column("name", sa.String(length=64), nullable=False, comment="skill名称(kebab-case)"),
        sa.Column("description", sa.Text(), nullable=False, comment="skill描述"),
        sa.Column("frontmatter", postgresql.JSONB(), nullable=False, comment="解析后的完整frontmatter"),
        sa.Column("version", sa.String(length=32), nullable=True, comment="版本号"),
        sa.Column("content_hash", sa.String(length=64), nullable=True, comment="内容哈希"),
        sa.Column("scope", sa.String(length=16), nullable=False, server_default="global", comment="作用域"),
        sa.Column("status", sa.String(length=16), nullable=False, server_default="enabled", comment="状态"),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), comment="创建时间"),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), comment="更新时间"),
        sa.Column("is_deleted", sa.Boolean(), server_default=sa.false(), nullable=False, comment="软删除标记"),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True, comment="软删除时间"),
        comment="web端上传的skill(正文与资源存对象存储,此表存索引与元数据)",
    )
    op.create_index("ix_skills_name", "skills", ["name"], unique=True)
    op.create_index("ix_skills_is_deleted", "skills", ["is_deleted"])
    op.create_index("ix_skills_content_hash", "skills", ["content_hash"])
```

---

## 7. 依赖与配置

### 7.1 `pyproject.toml` 新增依赖

在 `[project].dependencies` 加入(项目用 uv,索引为清华源):

```
"python-multipart>=0.0.20",   # FastAPI UploadFile / multipart 表单必需
"pyyaml>=6.0",                # 解析 frontmatter
"minio>=7.2.0",               # 对象存储 SDK(MinIO / S3 兼容)
```

安装:`uv sync`(或 `uv add python-multipart pyyaml minio`)。

### 7.2 `core/config.py` 新增配置

```python
# 对象存储(MinIO / S3 兼容)
MINIO_ENDPOINT = get("MINIO_ENDPOINT")          # 形如 "localhost:9000",不含 scheme
MINIO_ACCESS_KEY = get("MINIO_ACCESS_KEY")
MINIO_SECRET_KEY = get("MINIO_SECRET_KEY")
MINIO_BUCKET = get("MINIO_BUCKET", "agentos-skills")
MINIO_SECURE = (get("MINIO_SECURE", "false") or "").strip().lower() in {"1", "true", "yes"}

# skill 脚本执行(§3.5,进程内子进程 + 软限制)
# ⚠️ 进程内执行=在服务主机上运行上传者任意代码,仅限单机/内网可信部署;不可信环境须置 false
SKILL_EXEC_ENABLED = (get("SKILL_EXEC_ENABLED", "true") or "").strip().lower() in {"1", "true", "yes"}
SKILL_EXEC_TIMEOUT = get_int("SKILL_EXEC_TIMEOUT", 30)              # 单次执行超时(秒)
SKILL_EXEC_MAX_OUTPUT = get_int("SKILL_EXEC_MAX_OUTPUT", 65536)    # stdout/stderr 各自字节上限
# 解释器白名单:扩展名 -> 解释器 argv 前缀(不在表中的扩展名拒绝执行)
SKILL_EXEC_INTERPRETERS = {
    ".py": ["python"],
    ".sh": ["bash"],
    ".js": ["node"],
}
```

`.env` 需提供 `MINIO_ENDPOINT/MINIO_ACCESS_KEY/MINIO_SECRET_KEY`(以及可选 `MINIO_BUCKET`、
`MINIO_SECURE`);执行相关的 `SKILL_EXEC_*` 均有默认值,可选覆盖。

---

## 8. 对象存储抽象层(`core/storage.py`)

提供一层抽象,便于将来替换后端;本期实现 MinIO。

### 8.1 抽象与实现

```python
class ObjectStorage(ABC):
    @abstractmethod
    async def put(self, key: str, data: bytes, content_type: str | None = None) -> None: ...
    @abstractmethod
    async def get(self, key: str) -> bytes: ...
    @abstractmethod
    async def list_prefix(self, prefix: str) -> list[ObjectMeta]: ...   # 列某前缀下全部对象(key+size)
    @abstractmethod
    async def delete_prefix(self, prefix: str) -> None: ...   # 删除某前缀下全部对象
    @abstractmethod
    async def exists(self, key: str) -> bool: ...
```

其中 `ObjectMeta` 为轻量结构(如 `@dataclass class ObjectMeta: key: str; size: int`),仅承载重建
资源清单所需字段。

- `MinIOStorage(ObjectStorage)`:用官方 `minio` SDK。**minio SDK 是同步的**,所有网络调用
  必须用 `await asyncio.to_thread(...)` 包裹,避免阻塞事件循环。
- 构造时读 `config.MINIO_*`;`ensure_bucket()`:若 bucket 不存在则 `make_bucket`。
- `get(key)` 读取对象为 bytes;对象不存在时抛领域错误(见 §12),不要泄漏底层异常。
- `list_prefix(prefix)`:`list_objects(recursive=True)` 收集每个对象的 key 与 size,返回
  `list[ObjectMeta]`。**资源清单不落库,由此方法实时重建**(§6.1);skill 详情 / `Skill` 加载
  时,调用方把 key 去掉 `{name}/` 前缀还原成 `relative_path`,mime 用
  `mimetypes.guess_type` 按扩展名重猜。
- `delete_prefix(prefix)`:`list_objects(recursive=True)` 后逐个 `remove_object`(覆盖更新与
  删除时清理旧文件用)。
- 单例工厂 `get_object_storage() -> ObjectStorage`:进程内缓存一个实例。

### 8.2 key 拼装规则

统一 helper(建议放 `core/storage.py` 或 skill_service):

```python
def skill_object_key(name: str, relative_path: str) -> str:
    return f"{name}/{relative_path}"
# 例:skill_object_key("pdf-tools", "references/cli.md") -> "pdf-tools/references/cli.md"
```

---

## 9. 脚本执行层(`core/skill_executor.py`)

对应 §3.5。本层把"在临时工作目录内执行一条命令"抽象为统一接口,本期实现进程内子进程,
为将来强隔离预留零返工的替换点。

### 9.1 结果与接口

```python
@dataclass(frozen=True)
class ExecResult:
    exit_code: int | None      # 正常退出为退出码;超时被 kill 为 None
    stdout: str                # 已按上限截断(UTF-8, errors="replace")
    stderr: str                # 已按上限截断
    timed_out: bool            # 是否因超时被终止
    truncated: bool            # stdout/stderr 是否发生过截断

class Executor(ABC):
    @abstractmethod
    async def run(
        self, *, workdir: str, argv: list[str], stdin: bytes | None,
        timeout: int, env: dict[str, str],
    ) -> ExecResult: ...
```

### 9.2 `SubprocessExecutor(Executor)` 行为(硬性要求)

- 用 `asyncio.create_subprocess_exec(*argv, cwd=workdir, env=env, stdin/stdout/stderr=PIPE)`,
  **绝不经 shell**(不用 `create_subprocess_shell`、不做字符串拼接),`argv` 为数组。
- **进程组**:`start_new_session=True`(POSIX),超时用 `os.killpg(os.getpgid(pid), SIGKILL)`
  杀整个进程组,防止子进程 fork 出的孙进程逃逸。
- **超时**:`asyncio.wait_for(proc.communicate(input=stdin), timeout=timeout)`;
  捕获 `TimeoutError` → 杀进程组 → `timed_out=True`、`exit_code=None`。
- **输出截断**:stdout/stderr 各自读取上限 `config.SKILL_EXEC_MAX_OUTPUT` 字节,超出截断并置
  `truncated=True`;解码 `decode("utf-8", errors="replace")`。
- **env 最小化**:仅由调用方传入的白名单(见 §11 `SkillRun`),本层原样使用,不追加继承。
- 任何底层异常(解释器不存在等)转领域错误(§12),不外泄 traceback 给模型。

### 9.3 工厂

```python
def get_executor() -> Executor:  # 本期恒返回 SubprocessExecutor 单例
    ...
```

将来接入容器 / nsjail 时新增 `SandboxExecutor(Executor)`,在工厂按配置切换,**上层不改**。

---

## 10. Frontmatter 解析与校验(`core/skill_parser.py`)

无状态的纯函数模块:只解析 SKILL.md 文本、校验合法性,**不碰 DB、不碰对象存储、不扫目录**。
供上传与预检接口复用。

### 10.1 解析

```python
_FRONTMATTER_RE = re.compile(r"^---\s*\n(.*?)\n---\s*\n?(.*)", re.DOTALL)

@dataclass(frozen=True)
class ParsedSkill:
    frontmatter: dict[str, Any]   # yaml.safe_load 结果
    body: str                     # 第二个 --- 之后的正文(strip 前后空白)

def parse_skill_md(text: str) -> ParsedSkill: ...
```

- 用正则切出 frontmatter 与 body;`yaml.safe_load` 解析 YAML。
- 无合法 frontmatter(不以 `---` 开头 / 无第二个 `---` / YAML 非 dict)→ 抛校验错误(§12)。

### 10.2 校验规则(全部 MUST)

`validate_frontmatter(fm: dict) -> None`,任一不满足抛 `SkillValidationError`(§12),错误信息
明确指出违反项:

| 项 | 规则 |
|---|---|
| name 存在 | 缺失 / 空 → 错误 |
| name 格式 | 匹配 `^[a-z0-9]+(-[a-z0-9]+)*$` 且 ≤64 字符 |
| description 存在 | 缺失 / 空(strip 后)→ 错误 |
| description 长度 | ≤1024 字符 |
| compatibility 长度 | 若存在,≤500 字符 |
| metadata 类型 | 若存在,须为 dict 且值均为 str(宽松:非 str 值转 str 亦可,但须一致) |

- 未知顶层字段:**不拒绝**,原样保留(§5.1)。
- `allowed-tools`:接受行内列表与块列表两种;解析后统一为 `list[str]`。提供
  `get_allowed_tools(fm) -> list[str] | None` helper(读 `fm.get("allowed-tools")`,兼容字符串
  空格分隔与列表两种写法)。

### 10.3 name 与目录名一致性

该校验需要 bundle 上下文(目录名),不在本模块做,由 §12 上传流程校验(比对 zip 顶层目录名
与 frontmatter.name)。本模块只负责单文件文本级校验。

---

## 11. Repository 与 Schema

### 11.1 `repositories/skill_repo.py`

`SkillRepository(db: AsyncSession)`,只 `flush`,不 `commit`(§4.6)。方法:

```python
async def get_by_name(name: str, *, include_deleted: bool = False) -> SkillRecord | None
async def list_enabled() -> list[SkillRecord]          # status=="enabled",按 name 排序;供 catalog
async def list_all() -> list[SkillRecord]              # 列表接口用,按 name 排序
async def upsert(*, name, description, frontmatter, version, content_hash) -> SkillRecord
async def soft_delete(name: str) -> bool               # 置 is_deleted=True/deleted_at=now,返回是否命中
```

`upsert` 语义(**覆盖更新 + 软删复活**):

1. `get_by_name(name, include_deleted=True)` 查(含已软删)。
2. 命中:更新 description/frontmatter/version/content_hash,并复活
   (`is_deleted=False`、`deleted_at=None`、`status="enabled"`),`flush` 后返回。
3. 未命中:`SkillRecord(...)` 新建,`add` + `flush` + `refresh` 返回。

- `list_enabled` 只 `select(SkillRecord.name, SkillRecord.description)` 取两列即可(热路径省开销),
  或取整行也可;实现者择一,但注释说明。
- 软删除读过滤全局自动生效(§4.2),故常规查询无需手写 `is_deleted` 条件;`include_deleted=True`
  时须 `execution_options(include_deleted=True)`。

### 11.2 `schemas/skill.py`

`model_config = {"from_attributes": True}`。字段中文 `description`。

```python
class SkillResourceItem(BaseModel):   # 资源清单元素(运行时重建,非 DB)
    relative_path: str
    size: int
    mime: str

class SkillResponse(BaseModel):       # 列表精简:元数据,无正文、无资源
    id: int
    name: str
    description: str
    version: str | None
    status: str
    scope: str
    created_at: datetime
    updated_at: datetime

class SkillDetailResponse(SkillResponse):   # 详情:附 frontmatter + 资源清单 + 正文
    frontmatter: dict[str, Any]
    resources: list[SkillResourceItem]       # 由 service 经 list_prefix 重建填入
    body: str                                # 由 service 从 MinIO 取 SKILL.md 填入

class SkillValidateResult(BaseModel):   # 预检结果
    ok: bool
    name: str | None = None
    description: str | None = None
    errors: list[str] = Field(default_factory=list)
```

- `SkillResponse` 可 `model_validate(orm_obj)` 直接从 ORM 建。
- `SkillDetailResponse` 的 `resources`/`body` 非 DB 字段,由 service 组装后传入
  (`model_validate` + 补字段,或显式构造)。

---

## 12. 领域错误

在 `core/errors.py` 复用现有错误体系(项目已有 `NotFoundError`、`ValidationAppError` 等)。
skill 相关错误统一使用 `AgentException.message("具体错误消息")`,HTTP 状态码固定 500。

| 场景 | 错误消息示例 |
|---|---|
| frontmatter 缺失/非法、校验不通过 | `SKILL.md 缺少合法的 frontmatter` / `name 长度超过 64 字符` |
| zip 结构非法(无顶层目录/无 SKILL.md/多顶层目录) | `bundle 缺少 {top}/SKILL.md` / `bundle 为空或无顶层目录` |
| 目录名与 name 不一致 | `bundle 顶层目录名与 frontmatter.name 不一致` |
| 路径穿越 / 非法 relative_path | `bundle 成员含 .. 段,拒绝` / `资源相对路径含 .. 段` |
| skill 不存在 | `skill 不存在: {name}` |
| 资源对象不存在 | `对象不存在: {key}` / `脚本不存在: {name}/{path}` |
| 执行未启用 / 解释器不在白名单 | `skill 脚本执行已被禁用` / `脚本扩展名不在解释器白名单内` |
| 对象存储底层错误 | `创建Bucket失败` / `写入对象失败` / `读取对象失败` |

工具层(`run` 内)捕获 `AgentException`,转成给 LLM 的**可读文本**(不抛给框架),风格同现有工具
(如 `f"错误:..."`)。API 层让全局异常处理器转 `ApiResponse.error`(项目已有中间件)。

---

## 13. SkillService(`services/skill_service.py`)

业务编排层,负责 `commit`、跨存储协调、校验组合。构造 `SkillService(db, storage=None, executor=None)`
(storage/executor 缺省走各自单例工厂,便于测试注入)。

### 13.1 `upload_bundle(file_bytes: bytes) -> SkillRecord`

上传主流程(全部 MUST 按序):

1. **读 zip**:`zipfile.ZipFile(io.BytesIO(file_bytes))`;非法 zip → `SKILL_BUNDLE_INVALID`。
2. **定位顶层目录**:枚举成员,要求恰好一个顶层目录名 `top`;否则 `SKILL_BUNDLE_INVALID`。
3. **zip-slip 防护**:遍历每个成员名,拒绝绝对路径、`..` 段、越出 `top/` 的路径 →
   `SKILL_PATH_INVALID`。
4. **定位并解析 SKILL.md**:读 `top/SKILL.md`;缺失 → `SKILL_BUNDLE_INVALID`。
   `parse_skill_md` + `validate_frontmatter`(§10)→ 得 `name`/`description`/`frontmatter`/`version`。
5. **一致性**:`top == name` 否则 `SKILL_NAME_MISMATCH`。
6. **算 content_hash**:对 bundle 内全部文件内容(按路径排序后拼接或逐文件 hash 汇总)算
   sha256。若 `get_by_name(name)` 存在且 `content_hash` 相同 → **幂等短路**,直接返回现有记录
   (不重复写 MinIO、不改 DB)。
7. **写对象存储**:先 `delete_prefix(f"{name}/")` 清旧文件(覆盖更新去除已删资源),再逐个
   `put(skill_object_key(name, rel), data, content_type=mime)`。`rel` 为去掉 `top/` 前缀的相对路径,
   须过 §6.2 校验。
8. **落库**:`repo.upsert(...)` → `await db.commit()`。
9. 返回 `SkillRecord`。

> 失败处理:第 7 步之后若第 8 步失败,MinIO 可能已写入而 DB 未落——本期接受此弱一致(下次
> 同名上传会 `delete_prefix` 覆盖)。实现者可选:先落库后写存储的顺序取舍,但须在注释说明。
> 推荐"先写存储再落库",因为落库失败可重试上传,而存储孤儿文件由下次覆盖清理。

### 13.2 `validate(file_bytes: bytes) -> SkillValidateResult`

预检:执行 §13.1 的第 1–5 步(不写存储、不落库),收集错误。全过 → `ok=True` 带 name/description;
否则 `ok=False` 带 errors 列表。用于 `POST /skills/validate`。

### 13.3 读取类

```python
async def get_catalog() -> str
async def list_skills() -> list[SkillRecord]
async def get_detail(name: str) -> SkillDetailResponse    # 组装 frontmatter+resources+body
async def load_body(name: str) -> str                     # 从 MinIO 取 SKILL.md 正文
async def list_resources(name: str) -> list[SkillResourceItem]   # list_prefix 重建,排除 SKILL.md 交由调用方决定
async def read_resource(name: str, relative_path: str) -> tuple[bytes, str]   # (内容, mime)
```

- `get_catalog()`:`repo.list_enabled()` → 渲染多行文本,每行 `- {name}: {description}`;
  空则返回 `"(当前没有可用的 skill)"`。供 §14 注入系统提示。
- `get_detail(name)`:`get_by_name` 缺失 → `SKILL_NOT_FOUND`;`body = load_body`、
  `resources = list_resources`(**含或不含 SKILL.md 由接口语义定,详情接口建议含**),组装
  `SkillDetailResponse`。
- `read_resource(name, rel)`:校验 rel(§6.2)→ `storage.get(skill_object_key(name, rel))`;
  对象不存在 → `SKILL_RESOURCE_NOT_FOUND`;mime 由扩展名推断。
- `load_body(name)`:`storage.get(skill_object_key(name, "SKILL.md"))` 解码 UTF-8;
  须**剥离 frontmatter 只返回正文**(复用 `parse_skill_md`),避免把 YAML 头喂给模型。

### 13.4 `run_script(name, relative_path, args, stdin_text) -> ExecResult`(§3.5)

1. `config.SKILL_EXEC_ENABLED` 为 false → `SKILL_EXEC_DISABLED`。
2. 校验 `relative_path`:过 §6.2 正则、必须在 `scripts/` 下;取扩展名查
   `config.SKILL_EXEC_INTERPRETERS`,不在白名单 → `SKILL_EXEC_INTERPRETER`。
3. **物化**:`tempfile.mkdtemp()` 建一次性工作目录;`list_prefix(f"{name}/")` 拉全部资源,
   逐个 `get` 写入镜像目录(保持相对结构)。**try/finally 确保 `shutil.rmtree` 清理**。
4. **组 argv**:`interpreter + [脚本绝对路径] + args`;`env` 为最小白名单
   (`PATH`/`LANG`,**不含** `ANTHROPIC_API_KEY`/`DATABASE_URL`/`MINIO_*`)。
5. `executor.run(workdir=物化根, argv=..., stdin=stdin_text.encode() if 有 else None,
   timeout=config.SKILL_EXEC_TIMEOUT, env=...)` → 返回 `ExecResult`。

---

## 14. 工具层(`tools/builtin/skill.py`)

三个工具,均继承 `BaseTool`,`run` async 返回 `str`。经 `ctx.db` 构造
`SkillService(ctx.db)`。工具内捕获领域错误转可读文本(§12)。

### 14.1 `SkillTool`(加载正文)

```python
class SkillInput(BaseModel):
    name: str = Field(description="要加载的 skill 名称(取自系统提示中的可用 skill 清单)")

class SkillTool(BaseTool):
    name = "Skill"
    description = "加载指定 skill 的完整说明并注入上下文。当任务匹配某个可用 skill 时,先调用它取回正文再执行。"
    input_model = SkillInput
```

`run` 行为:
- `body = service.load_body(name)`;`resources = service.list_resources(name)`(**排除 SKILL.md**)。
- 返回 = 正文 + 官方 structured wrapping 尾注,形如:

```
<skill name="pdf-tools">
{正文}
</skill>
<skill_resources base="pdf-tools">
此 skill 的相对路径以 pdf-tools/ 为基准。以下资源可用 SkillResource 工具按 relative_path 读取:
- references/cli.md (text/markdown, 8.4KB)
- scripts/extract.py (text/x-python, 1.2KB)
脚本(scripts/)可读取查看;如需运行,使用 SkillRun 工具(当前为进程内执行,受超时与输出上限约束)。
</skill_resources>
```

- skill 不存在 → 返回 `f"错误:未知 skill '{name}'。"`(不抛)。

### 14.2 `SkillResourceTool`(读资源)

```python
class SkillResourceInput(BaseModel):
    skill_name: str = Field(description="skill 名称")
    relative_path: str = Field(description="资源相对路径,如 references/cli.md")

class SkillResourceTool(BaseTool):
    name = "SkillResource"
    description = "读取指定 skill 的某个资源文件(references/scripts/assets),返回其内容。"
    input_model = SkillResourceInput
```

`run`:`data, mime = service.read_resource(skill_name, relative_path)`。
- 文本类 mime(text/*、application/json、常见脚本)→ 直接 `data.decode("utf-8", errors="replace")`。
- 二进制类 → 返回描述(`f"[二进制资源 {relative_path},{mime},{len} 字节,暂不支持内联展示]"`),
  不塞 base64 进上下文。
- 非法路径 / 不存在 → 可读错误文本。

### 14.3 `SkillRunTool`(执行脚本,§3.5)

```python
class SkillRunInput(BaseModel):
    skill_name: str = Field(description="skill 名称")
    relative_path: str = Field(description="脚本相对路径,须在 scripts/ 下")
    args: list[str] | None = Field(default=None, description="传给脚本的命令行参数")
    stdin: str | None = Field(default=None, description="写入脚本标准输入的文本")

class SkillRunTool(BaseTool):
    name = "SkillRun"
    description = "执行指定 skill 的脚本(scripts/ 下),返回退出码与截断后的 stdout/stderr。进程内执行,受超时与输出上限约束。"
    input_model = SkillRunInput
```

`run`:`result = service.run_script(...)` → 组装可读文本(退出码、是否超时、是否截断、
stdout、stderr)。执行未启用 → 返回说明性错误文本。

### 14.4 注册

- `tools/builtin/__init__.py`:导出 `SkillTool`、`SkillResourceTool`、`SkillRunTool`。
- `tools/registry.py` 的 `build_tool_registry()`:追加三者实例(**均无参构造**)。
- 子代理/队友:三工具默认可用(不加入 `SubAgentSpec.disallowed_tools` 黑名单);如需限制
  `SkillRun`,后续再议。

---

## 15. 运行时发现(注入系统提示)

### 15.1 `prompts.py`

`compose_system_prompt` 增加可选参数,把 skill 清单拼在基座之后、会话附加之前:

```python
def compose_system_prompt(session_prompt: str | None, skills_catalog: str | None = None) -> str:
    parts = [LEAD_SYSTEM_PROMPT]
    if skills_catalog and skills_catalog.strip():
        parts.append(
            "## 可用 skills\n"
            "以下 skill 可按需加载(调用 Skill 工具取回完整说明后再执行):\n"
            f"{skills_catalog.strip()}"
        )
    if session_prompt and session_prompt.strip():
        parts.append(session_prompt.strip())
    return "\n\n".join(parts)
```

- 向后兼容:`skills_catalog` 缺省 None 时行为与现状一致。

### 15.2 `services/agent_runtime.py`

主代理运行时注入 catalog:

- 在 `_run_llm_loop` 内,构造 `system_prompt` 处改为:
  ```python
  skills_catalog = await SkillService(self.db).get_catalog()
  system_prompt = compose_system_prompt(session.system_prompt, skills_catalog)
  ```
- **性能**:catalog 每回合都取会打 DB。建议**每次 `run` 缓存一次**(在 `_produce`/`run` 开头取一次
  存实例变量,循环内复用),而非每轮 loop 重取。实现者按此优化,但须保证同一次用户消息处理内
  一致即可。
- **仅主代理(orchestrator)注入**。子代理/队友(`subagent_runner.py`)本期**不注入 catalog**
  (其 system_prompt 走 `spec.system_prompt`),但三个 skill 工具对它们仍可用——即它们能被显式
  指派使用某 skill,只是不主动发现。理由:避免污染子代理上下文;子代理任务通常已明确。

---

## 16. API 层(`api/skills.py`)

`router = APIRouter()`;所有响应 `ok(data, request)` 包装为 `ApiResponse[...]`;
DB 用 `Depends(get_db)`;handler 薄,调 `SkillService`。

| 方法 | 路径 | 入参 | 响应 data | 说明 |
|---|---|---|---|---|
| POST | `/api/skills` | `file: UploadFile`(zip,multipart) | `SkillResponse` | 上传/覆盖更新 |
| POST | `/api/skills/validate` | `file: UploadFile` | `SkillValidateResult` | 预检不落库 |
| GET | `/api/skills` | — | `list[SkillResponse]` | 列表(精简) |
| GET | `/api/skills/{name}` | path name | `SkillDetailResponse` | 详情(含 body+resources) |
| DELETE | `/api/skills/{name}` | path name | `{"deleted": bool}` | 软删除 |

- 上传接口:`async def upload(file: UploadFile, request: Request, db=Depends(get_db))`,
  `data = await file.read()` → `service.upload_bundle(data)` → `ok(SkillResponse.model_validate(rec))`。
  可加大小限制(如 ≤8MB,对齐官方,超出 413/校验错误)。
- `GET /{name}`:不存在 → service 抛 `SKILL_NOT_FOUND`,全局异常处理器转 error 响应。
- **注册**:`api/router.py` 补 `from api import ... skills`,并
  `api_router.include_router(skills.router, prefix="/skills", tags=["skills"])`。

### 16.1 上传接口鉴权与安全提示

- 上传 = 写入可被模型执行的代码(§3.5)。当前项目无用户体系,**上传接口暂无鉴权**。
  须在代码注释与本 PRD 中显式声明:该接口应仅在可信网络暴露,生产/公网须补鉴权
  (与 §3.5 的执行信任边界一致)。
- 文件类型:只接受 zip;非 zip → `SKILL_BUNDLE_INVALID`。

---

## 17. 权限系统交互(`SkillResource` / `SkillRun` 免打扰)

项目已有工具权限审批系统(`services/permission_service.py`,`DANGEROUS_TOOLS` 等,拦截
orchestrator 的部分工具调用需人工审批)。

- **`Skill`、`SkillResource`**:只读能力,**不应触发审批**。确认它们不在 `DANGEROUS_TOOLS`
  或需审批集合内;若权限系统默认对未知工具放行,则无需改动,仅须验证。否则显式 allowlist。
  依据:官方实现指南明确——若每读一个资源都弹审批,skill 流程会被打断。
- **`SkillRun`**:执行脚本,风险等级高。**建议纳入需审批工具集**(与 `Bash` 同级),或至少
  可配置。实现者:确认 `SkillRun` 的审批策略与 `Bash` 一致(§3.5 已声明其等同在主机跑任意代码)。
- 实现前须阅读 `permission_service.py` 确认现有默认策略(放行/拦截/审批),再决定是否需要显式
  登记,避免误判。

---

## 18. 验证清单(实现后必做)

按项目 `verification` 约定,改完跑通再交付。无 DB/MinIO 环境时,分离可离线验证项。

**离线(无需 DB/MinIO):**
- `.venv/bin/python -c "import main"`(加载 `.env` 后)导入无误——证明工具注册、路由挂载不破坏启动。
- `build_tool_registry()` 能构建,含 `Skill`/`SkillResource`/`SkillRun` 三工具,`to_param()` 正常。
- `parse_skill_md` / `validate_frontmatter` 单测:合法样例(hue/langfuse/test 三个现有样例)、
  各类非法输入(缺 name、name 非法、缺 description、超长、无 frontmatter)。
- `upload_bundle` 的 zip 解析与 zip-slip 防护单测:构造含 `../` 成员的恶意 zip,须被拒。
- `SubprocessExecutor` 单测:正常退出、超时被杀(sleep 脚本 + 短 timeout)、输出截断、
  env 不含敏感变量。可用一个 `echo`/`python -c` 脚本在临时目录直接验证,不依赖 MinIO。
- `compose_system_prompt(None, catalog)` 文本拼装正确;缺省 None 向后兼容。

**联机(需 DB + MinIO,视环境):**
- alembic `upgrade head` 建表成功;`downgrade` 回滚成功。
- 端到端:上传 zip → GET 列表/详情 → `Skill`/`SkillResource`/`SkillRun` 工具经 `run_with_dict`
  返回预期 → 覆盖更新(同名再传,资源增减正确)→ 软删 → 同名复活。
- migration 脚本 `import` 校验(`python -c "import alembic.versions.0007_create_skills"` 视文件名调整)。

**清理**:验证产生的临时文件、测试上传的 skill 记录/对象须清理。

---

## 19. 实施顺序(建议分批,每批可独立验证)

依赖自底向上,分批提交便于回滚与评审:

1. **依赖与配置**(§7):`pyproject.toml` + `core/config.py`,`uv sync`。
2. **数据层**(§6、§11.1):`models/skill.py` + `models/__init__.py` 登记 + 迁移
   `0007_create_skills.py` + `repositories/skill_repo.py`。跑 `upgrade head` 验证。
3. **基础设施**(§8、§9、§10):`core/storage.py`、`core/skill_executor.py`、`core/skill_parser.py`。
   各自单测(parser/executor 可离线测)。
4. **服务层**(§11.2、§12、§13):`schemas/skill.py` + `services/skill_service.py`。
5. **工具层**(§14):`tools/builtin/skill.py` + 注册。验证 registry 构建。
6. **运行时发现**(§15):`prompts.py` + `services/agent_runtime.py`。
7. **API 层**(§16):`api/skills.py` + `api/router.py` 注册。
8. **权限确认**(§17)+ **全量验证**(§18)+ 清理。

每批完成后运行对应验证项;`import main` 在 2、5、6、7 批后都应通过。

---

## 20. 涉及文件汇总

**新建:**
- `models/skill.py`
- `alembic/versions/0007_create_skills.py`
- `repositories/skill_repo.py`
- `schemas/skill.py`
- `services/skill_service.py`
- `core/storage.py`
- `core/skill_executor.py`
- `core/skill_parser.py`
- `tools/builtin/skill.py`
- `api/skills.py`
- (测试)`tests/` 下 parser/executor/upload 相关用例

**改动:**
- `pyproject.toml`(依赖)
- `core/config.py`(MinIO + 执行配置)
- `models/__init__.py`(登记 SkillRecord)
- `tools/builtin/__init__.py`(导出三工具)
- `tools/registry.py`(装配三工具)
- `prompts.py`(catalog 注入)
- `services/agent_runtime.py`(取 catalog 传入)
- `api/router.py`(注册路由)

> 注:本会话早期曾按"本地目录扫描"旧思路落过半成品(`tools/skill_registry.py`、
> `tools/builtin/skill.py` 旧版、`core/config.py` 里的 `SKILLS_DIR` 等)。实现本 PRD 时以本文
> 为准:`tools/skill_registry.py` 若与新设计无关应删除或重构为 `core/skill_parser.py`;
> 旧 `SkillTool` 用本文 §14 覆盖;`core/config.py` 的 `SKILLS_DIR` 由 §7.2 的 MinIO 配置取代。

---

## 21. 未决项 / 待评审确认

以下点实现前建议再确认,不影响主体设计:

1. **上传失败的存储/DB 一致性顺序**(§13.1 注):先写存储后落库 vs 反之。PRD 推荐前者,接受
   孤儿文件由下次覆盖清理。
2. **详情接口 resources 是否含 SKILL.md**:PRD 建议详情接口含(完整视图),`Skill` 工具展示给
   模型时排除(正文已单独给)。
3. **SkillRun 是否默认需审批**(§17):PRD 建议与 Bash 同级需审批;若审批链路对工具调用支持不足,
   可先配置开关。
4. **上传大小上限**:PRD 建议 ≤8MB(对齐官方),可调。
5. **二进制资源**:`SkillResource` 对二进制只返回描述不内联(§14.2);若需下载,后续加专门下载接口。
6. **执行 env 白名单的具体项**(§13.4):PRD 给 `PATH`/`LANG`,实现时按解释器实际需要微调
   (如 python 的 `PYTHONPATH` 是否需要),但**严禁**下发 `ANTHROPIC_API_KEY`/`DATABASE_URL`/`MINIO_*`。

---

（PRD 结束）

