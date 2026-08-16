# 质量规范

> AgentOS 后端代码质量标准

---

## 概述

本文档定义 AgentOS 项目的代码质量标准，包括命名、类型注解、格式、测试和日志规范。
所有规范基于项目实际代码风格，不是理想化通用建议。

**技术栈**：Python 3.14、FastAPI、Pydantic v2、SQLAlchemy 2 Async、PostgreSQL、Alembic、pytest

---

## 禁止模式

以下是项目中明确禁止的历史遗留写法，不得在新代码中使用：

### 类型注解

- ❌ **禁止** `Optional[T]`，使用 `T | None`
- ❌ **禁止** 裸 `dict` 或 `list`，使用 `dict[str, Any]`、`list[T]`
- ❌ **禁止** Pydantic v1 API（`parse_obj`、`dict()`），使用 `model_validate`、`model_dump`

### 时间处理

- ❌ **禁止** `datetime.utcnow()`（已废弃），使用 `datetime.now(UTC)`

### 密码与安全

- ❌ **禁止** MD5 密码哈希（仅为历史兼容保留），新功能使用 bcrypt 或 Argon2

### 调试与日志

- ❌ **禁止** `print()` 调试，使用标准 `logging`
- ❌ **禁止** 在异常处理中使用 `print`，使用 `logger.exception()`

### API 响应

- ❌ **禁止** 裸 `HTTPException` 或临时错误结构，统一使用 `AgentException.message("中文消息", details)`
- ❌ **禁止** 不使用 `ApiResponse[T]` 和 `ok(data, request)` 的普通 JSON API（SSE 流式响应除外）

### 数据库操作

- ❌ **禁止** Repository 自行 `commit`，写操作使用 `add/flush/refresh`
- ❌ **禁止** 可变字面量作为默认值（如 `default=[]`），使用 `default=dict` 或 `MutableDict/MutableList`
- ❌ **禁止** 随意物理删除，使用继承自 `Base` 的软删除机制

### 架构边界

- ❌ **禁止** 跨域直接使用别域的 Repository 或自行拼写别域表的 SQL
- ❌ **禁止** API 层包含业务校验与跨模块编排逻辑
- ❌ **禁止** 新建按层划分的目录（`models/`、`services/`、`repositories/`、`schemas/`）

---

## 必需模式

以下是项目中必须遵守的代码规范：

### 命名约定

- **文件、函数、变量**：`snake_case`
- **类**：`PascalCase`
- **常量**：`UPPER_SNAKE_CASE`
- **语义后缀**：`*Record`（ORM 模型）、`*Repository`、`*Service`、`*Request`、`*Response`、`*Tool`、`*Input`

**示例**：
- `user/models.py` → `UserRecord`
- `user/repository.py` → `UserRepository`
- `auth/schemas.py` → `RegisterRequest`、`LoginRequest`、`AuthResponse`

### 类型注解

**公共方法、数据字段和返回值必须补齐类型注解**，使用现代写法：

```python
# ✅ 正确
def get_by_id(self, user_id: int) -> UserRecord | None:
    ...

async def list_all(self, limit: int = 100) -> list[UserRecord]:
    ...

preferences: dict[str, Any] = {}
```

**SQLAlchemy 2**：使用 `Mapped[T]` + `mapped_column`

```python
# 参考：user/models.py
id: Mapped[int] = mapped_column(primary_key=True, comment="用户ID")
email: Mapped[str] = mapped_column(String(255), unique=True, index=True, comment="邮箱")
full_name: Mapped[str | None] = mapped_column(String(128), nullable=True, comment="真实姓名")
preferences: Mapped[dict] = mapped_column(MutableDict.as_mutable(json_type()), default=dict)
```

**Pydantic v2**：使用 `model_validate`、`model_dump`、`Field`，不使用 v1 API

```python
# 参考：auth/schemas.py
class RegisterRequest(BaseModel):
    email: EmailStr = Field(description="邮箱")
    username: str = Field(min_length=3, max_length=64, description="用户名")
    full_name: str | None = Field(default=None, max_length=128, description="全名")

class UserInfo(BaseModel):
    id: int = Field(description="用户ID")
    email: str = Field(description="邮箱")
    model_config = {"from_attributes": True}  # ORM 响应 Schema 必须设置
```

### 代码格式

- **缩进**：4 空格
- **行宽**：100 字符（`pyproject.toml` 已配置 `ruff` line-length）
- **Python 版本**：3.14 语法

**导入分组**：标准库、第三方库、项目内绝对导入，组间留空行

```python
# 参考：auth/service.py
import hashlib
from datetime import UTC, datetime, timedelta

import jwt
from sqlalchemy.ext.asyncio import AsyncSession

from core.config import settings
from core.errors import AgentException
from user.models import UserRecord
from user.repository import UserRepository
```

**长参数、调用和容器**：使用括号分行，保留尾逗号

```python
# 参考：user/repository.py
async def create(
    self,
    username: str,
    email: str,
    password: str,
    full_name: str | None = None,
    phone: str | None = None,
    auth_provider: str = "local",
    provider_user_id: str | None = None,
    preferences: dict | None = None,
) -> UserRecord:
    ...
```

### 注释与文档

**docstring、业务错误、Field 描述、关键注释优先使用简体中文**

```python
# 参考：auth/service.py
class AuthService:
    """认证服务"""

    async def register(
        self,
        email: str,
        username: str,
        password: str,
        full_name: str | None = None,
    ) -> dict:
        """用户注册"""
        # 检查邮箱唯一性
        existing_email = await self.repo.get_by_email(email)
        if existing_email:
            raise AgentException.message("邮箱已被注册")
```

**注释解释契约、原因、事务、并发、兼容性和恢复语义，不逐行复述代码**

```python
# 参考：auth/service.py（第53-55行）
# 不在此 commit:user 的 INSERT 已由 repo.create 内的 flush 落到事务中,
# user.id 可用;整体提交交给请求边界(get_db),与下面的工作区创建同事务,
# 若工作区创建失败则 user 一并回滚,避免孤儿用户。
```

### 异步编程

**所有数据库、LLM、事件流和工具 I/O 使用 async/await**

```python
# 参考：user/repository.py
async def create(self, username: str, email: str, password: str, ...) -> UserRecord:
    user = UserRecord(...)
    self.db.add(user)
    await self.db.flush()
    await self.db.refresh(user)
    return user

async def get_by_email(self, email: str) -> UserRecord | None:
    stmt = select(UserRecord).where(UserRecord.email == email)
    return (await self.db.scalars(stmt)).first()
```

异步流标注 `AsyncIterator`。

### API 响应与错误处理

**普通 JSON API 使用 `ApiResponse[T]` 和 `ok(data, request)`**

```python
# 参考：core/responses.py
def ok(data: Any, request: Request) -> dict[str, Any]:
    return {
        "data": data,
        "error": None,
        "request_id": getattr(request.state, "request_id", None),
    }
```

**业务错误统一使用 `AgentException.message()`**

```python
# 参考：auth/service.py
if existing_email:
    raise AgentException.message("邮箱已被注册")

if not user:
    raise AgentException.message("用户名或密码错误")
```

**包装底层异常时使用 `raise ... from exc` 保留异常链**

```python
# 参考：skill/service.py
try:
    zf = zipfile.ZipFile(io.BytesIO(file_bytes))
except zipfile.BadZipFile as exc:
    raise AgentException.message(f"非法的 zip 文件: {exc}") from exc
```

### 日志规范

**使用标准 `logging`，通过 `extra` 传递结构化上下文**

```python
# 参考：memory/jobs/worker.py
import logging

logger = logging.getLogger(__name__)

logger.info("Memory Worker已启动", extra={"worker_id": self.runner.worker_id})
```

**错误日志优先使用 `logger.exception()`，自动保留完整异常堆栈**

```python
# 参考：memory/recall/service.py
try:
    ...
except Exception:
    logger.exception("Memory选择器失败，切换词法降级")
```

**日志不得输出密码、Token、密钥、完整连接串或其他敏感信息**

### 数据库规范

**Repository 持有 AsyncSession，写操作使用 `add/flush/refresh`，不得自行 `commit`**

```python
# 参考：user/repository.py
async def create(self, username: str, email: str, password: str, ...) -> UserRecord:
    user = UserRecord(...)
    self.db.add(user)
    await self.db.flush()  # 落到事务，获得 user.id
    await self.db.refresh(user)  # 刷新关联属性
    return user

async def update(self, user: UserRecord) -> UserRecord:
    await self.db.flush()
    await self.db.refresh(user)
    return user
```

**普通 HTTP 请求由 `get_db` 统一管理事务：成功 `commit`，异常 `rollback`**

只有流式 Agent 或后台运行为了增量持久化才能分步 `commit`，并应注释生命周期原因。

**ORM 表和列使用中文 `comment`**

```python
# 参考：user/models.py
class UserRecord(Base):
    __tablename__ = "users"
    __table_args__ = (
        Index("ix_users_auth_provider_provider_user_id", "auth_provider", "provider_user_id"),
        {"comment": "用户表"},
    )

    id: Mapped[int] = mapped_column(primary_key=True, comment="用户ID")
    email: Mapped[str] = mapped_column(String(255), unique=True, index=True, comment="邮箱(唯一登录标识)")
```

**JSON 可变字段使用 `MutableDict/MutableList` 和 `default=dict/list`**

```python
# 参考：user/models.py
preferences: Mapped[dict] = mapped_column(
    MutableDict.as_mutable(json_type()),
    default=dict,
    comment="用户偏好设置",
)
```

**新增 ORM 后必须登记到 `database/registry.py`**

实体分散在各 feature 包，registry 是唯一汇总入口，漏登记会导致 `autogenerate` 误判删表。

**提供显式 Alembic `upgrade/downgrade` 迁移**

已进入迁移历史的 Alembic 文件不得直接删除或篡改。

### 架构分层

**代码按 feature 域组织**，每个域一个顶层包，内含 `models.py`、`schemas.py`、`repository.py`、`service.py`、`api.py`

```
auth/
  models.py
  schemas.py
  repository.py
  service.py
  api.py
user/
  models.py
  schemas.py
  repository.py
  service.py
  api.py
```

**分层依赖关系**：API → Service → Repository → ORM Model

- **API 层**：路由、Depends 注入、认证/权限入口、Schema 转换和响应包装
- **Service 层**：业务校验与跨模块编排
- **Repository 层**：SQL 查询和持久化

**跨域调用规则**：

- feature 包之间只允许 Service 调 Service
- 不得跨域直接使用别的域的 Repository 或自行拼写别域表的 SQL
- 本域 Service 才能访问本域 Repository

```python
# 参考：auth/service.py（第58-67行）
# ✅ 正确：auth 域 Service 调用 workspace 域 Service
from workspace.service import WorkspaceService

workspace_service = WorkspaceService(self.db)
workspace: WorkspaceRecord = await workspace_service.create_workspace(
    creator_user_id=user.id,
    name=f"{username}_personal",
    slug=f"{username}-personal",
    display_name=f"{full_name or username}的个人空间",
    workspace_type="personal",
)

# ❌ 错误：auth 域直接使用 workspace 域的 Repository
from workspace.repository import WorkspaceRepository
repo = WorkspaceRepository(self.db)  # 违反分层
```

**判断归属看表的所有者**：操作哪张表就归哪个域，不看谁先需要这个功能。

### 内部协议与值对象

**优先使用类型化 `dataclass`**

```python
# 参考：skill/service.py
from dataclasses import dataclass
from typing import Any

@dataclass(frozen=True)
class _ParsedBundle:
    """bundle 解析结果"""
    root_prefix: str  # skill 根在 zip 内的前缀,如 "foo/" 或 ""(SKILL.md 在根级)
    name: str
    description: str
    frontmatter: dict[str, Any]
    version: str | None
    files: dict[str, bytes]  # 相对 skill 根的路径 -> 文件内容(含 SKILL.md)
```

不可变对象使用 `frozen=True`，通过工厂方法和 `to_dict()` 控制合法状态及序列化。

---

## 测试要求

### 测试组织

- **测试框架**：pytest
- **测试路径**：`tests/test_*.py`（已在 `pyproject.toml` 中配置 `testpaths = ["tests"]`）
- **覆盖范围**：成功路径、失败路径和关键副作用

### 测试分类

**局部 Service/事务测试**：可使用 Fake、monkeypatch 和调用次数断言

**PostgreSQL 特性集成测试**：JSONB、锁、RLS、租户隔离等特性必须使用真实 PostgreSQL

### 验证要求

**修改后必须运行相关测试和完整测试，如实报告**：
- 执行的命令
- 通过项数
- 失败项及原因

不得在未执行时声称验证通过。

---

## 代码审查清单

提交代码前，请自查以下项目：

### 命名与类型
- [ ] 文件、函数、变量使用 `snake_case`；类使用 `PascalCase`；常量使用 `UPPER_SNAKE_CASE`
- [ ] 公共方法、数据字段、返回值补齐类型注解，使用现代写法（`T | None`、`list[T]`、`dict[str, Any]`）
- [ ] SQLAlchemy 使用 `Mapped[T]` + `mapped_column`
- [ ] Pydantic 使用 v2 API（`model_validate`、`model_dump`、`Field`）
- [ ] ORM 响应 Schema 设置 `model_config = {"from_attributes": True}`

### 格式与文档
- [ ] 遵循 4 空格缩进、100 字符行宽
- [ ] 导入按"标准库、第三方库、项目内绝对导入"分组，组间留空行
- [ ] 长参数、调用和容器使用括号分行，并保留尾逗号
- [ ] docstring、业务错误、Field 描述、关键注释使用简体中文
- [ ] 注释解释契约、原因、事务、并发、兼容性和恢复语义，不逐行复述代码

### API 与错误处理
- [ ] 普通 JSON API 使用 `ApiResponse[T]` 和 `ok(data, request)`
- [ ] 业务错误使用 `raise AgentException.message("中文消息", details)`
- [ ] 包装底层异常时使用 `raise ... from exc`
- [ ] 使用标准 `logging`，通过 `extra` 传递结构化上下文
- [ ] 错误日志使用 `logger.exception()`，不使用 `print`

### 数据库
- [ ] Repository 写操作使用 `add/flush/refresh`，不自行 `commit`
- [ ] ORM 表和列使用中文 `comment`
- [ ] JSON 可变字段使用 `MutableDict/MutableList` 和 `default=dict/list`
- [ ] 新增 ORM 登记到 `database/registry.py`
- [ ] 提供显式 Alembic 迁移（`upgrade/downgrade`）

### 架构分层
- [ ] 代码按 feature 域组织（不新建按层划分的目录）
- [ ] API 层保持轻薄，业务校验与跨模块编排放在 Service
- [ ] 跨域只允许 Service 调 Service，不跨域使用 Repository
- [ ] 操作哪张表就归哪个域

### 安全与敏感信息
- [ ] 日志、API 响应、测试快照不包含密码、Token、密钥、数据库连接串
- [ ] 不在代码、文档、提交记录中暴露 Anthropic API Key

### 测试
- [ ] 补充 pytest 测试，覆盖成功路径、失败路径和关键副作用
- [ ] PostgreSQL 特性（JSONB、锁、RLS）使用真实 PostgreSQL 集成测试
- [ ] 运行相关测试和完整测试，如实报告结果

### 禁止模式自查
- [ ] 不使用 `Optional[T]`、裸 `dict/list`、`datetime.utcnow()`、`print`
- [ ] 不使用 Pydantic v1 API、裸 `HTTPException`
- [ ] 不在 Repository 自行 `commit`
- [ ] 不跨域直接使用别域 Repository

---

## 参考文件

以下是本规范引用的真实代码示例：

- **ORM 模型**：`user/models.py`（`UserRecord`、`Mapped[T]`、`mapped_column`、中文 `comment`）
- **Repository**：`user/repository.py`（`add/flush/refresh`、类型注解、异步方法）
- **Service**：`auth/service.py`（跨域调用、事务注释、业务校验）、`skill/service.py`（`dataclass` 值对象、异常包装）
- **Schema**：`auth/schemas.py`（Pydantic v2、`Field`、`model_config`）
- **错误处理**：`core/errors.py`（`AgentException.message`）
- **API 响应**：`core/responses.py`（`ApiResponse[T]`、`ok(data, request)`）
- **日志**：`memory/jobs/worker.py`、`memory/recall/service.py`（`logging`、`logger.exception()`、`extra`）
- **配置**：`pyproject.toml`（pytest、ruff、line-length=100）

---

## 更新说明

本文档基于 AgentOS 项目实际代码风格编写，所有规范和示例均来自真实实现。
如有疑问，优先参考上述参考文件和 `CLAUDE.md` 项目指南。
