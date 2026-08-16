# Database Guidelines

> AgentOS 数据库规范：SQLAlchemy 2 Async + PostgreSQL + Alembic

---

## Overview

AgentOS 使用 **SQLAlchemy 2 Async** + **PostgreSQL** + **Alembic** 管理数据库：

- **ORM**: SQLAlchemy 2 异步 API，使用 `Mapped[T]` + `mapped_column`，不使用 v1 API
- **数据库**: PostgreSQL（通过 psycopg3 异步驱动连接）
- **迁移**: Alembic 管理所有表结构变更，提供显式 `upgrade()` 和 `downgrade()`
- **事务管理**: 普通 HTTP 请求由 `get_db()` 统一管理事务生命周期，Repository 层不自行 commit
- **软删除**: 所有 ORM 实体继承 `Base`，自动获得 `is_deleted` + `deleted_at` 字段和全局软删除读过滤

**关键文件**:
- `/Users/xianglei/Documents/develop/code/python/AgentOS/database/base.py` — Base 类定义（审计字段 + 软删除）
- `/Users/xianglei/Documents/develop/code/python/AgentOS/database/engine.py` — 异步引擎 + 会话工厂 + `get_db()` 事务边界
- `/Users/xianglei/Documents/develop/code/python/AgentOS/database/registry.py` — ORM 实体注册入口（**新增实体必须登记**）

---

## ORM 实体定义

### 1. 使用 SQLAlchemy 2 现代语法

所有列使用 `Mapped[T]` + `mapped_column()`，不使用 v1 的 `Column()` API：

```python
from sqlalchemy import String, Integer, ForeignKey
from sqlalchemy.orm import Mapped, mapped_column
from database.base import Base

class TaskRecord(Base):
    __tablename__ = "tasks"
    __table_args__ = {"comment": "会话任务记录"}

    id: Mapped[int] = mapped_column(Integer, primary_key=True, comment="任务ID")
    session_id: Mapped[int] = mapped_column(
        ForeignKey("sessions.id", ondelete="CASCADE"),
        index=True,
        comment="所属会话ID",
    )
    subject: Mapped[str] = mapped_column(String(200), comment="任务主题")
    status: Mapped[str] = mapped_column(String(32), default="pending", index=True, comment="任务状态")
```

**参考**: `/Users/xianglei/Documents/develop/code/python/AgentOS/task/models.py`

### 2. 继承 Base 获得审计字段和软删除

所有实体继承 `Base`，自动获得以下字段：

- `created_at: Mapped[datetime]` — 创建时间（服务端默认 `now()`）
- `updated_at: Mapped[datetime]` — 更新时间（服务端默认 `now()`，onupdate 自动更新）
- `is_deleted: Mapped[bool]` — 软删除标记（默认 `False`，有索引）
- `deleted_at: Mapped[datetime | None]` — 软删除时间（空表示未删除）

`database/engine.py` 中的全局 ORM 事件监听器自动过滤 `is_deleted = True` 的行，所有查询默认只返回有效数据。

**不要随意物理删除数据**，Repository 的 `delete()` 方法应设置 `is_deleted = True`。

**参考**: `/Users/xianglei/Documents/develop/code/python/AgentOS/database/base.py` 第 16-44 行

### 3. 表和列必须有中文 comment

所有表使用 `__table_args__` 添加 `comment`，所有列使用 `comment` 参数：

```python
class UserRecord(Base):
    __tablename__ = "users"
    __table_args__ = {"comment": "用户表"}

    id: Mapped[int] = mapped_column(primary_key=True, comment="用户ID")
    email: Mapped[str] = mapped_column(String(255), unique=True, index=True, comment="邮箱(唯一登录标识)")
```

**参考**: `/Users/xianglei/Documents/develop/code/python/AgentOS/user/models.py`

### 4. 外键必须明确 ondelete 行为

所有外键必须显式指定 `ondelete` 参数（通常是 `CASCADE` 或 `SET NULL`）：

```python
session_id: Mapped[int] = mapped_column(
    ForeignKey("sessions.id", ondelete="CASCADE"),
    index=True,
    comment="所属会话ID",
)
```

- `CASCADE`: 父记录删除时级联删除子记录（如会话删除时删除消息）
- `SET NULL`: 父记录删除时子记录外键置空（需列允许 `nullable=True`）

**参考**: `/Users/xianglei/Documents/develop/code/python/AgentOS/task/models.py` 第 17-20 行

### 5. JSON 可变字段使用 MutableDict/MutableList

JSON 类型字段必须使用 `MutableDict.as_mutable()` 或 `MutableList.as_mutable()` + `default=dict/list`，**禁止**使用可变字面量作为默认值：

```python
from sqlalchemy.ext.mutable import MutableDict, MutableList
from database.base import json_type

# ✅ 正确
preferences: Mapped[dict] = mapped_column(
    MutableDict.as_mutable(json_type()),
    default=dict,
    comment="用户偏好设置",
)

blocked_by: Mapped[list[Any]] = mapped_column(
    MutableList.as_mutable(json_type()),
    default=list,
    comment="阻塞任务列表",
)

# ❌ 错误：可变字面量会在所有实例间共享
metadata: Mapped[dict] = mapped_column(json_type(), default={})
```

**参考**: `/Users/xianglei/Documents/develop/code/python/AgentOS/user/models.py` 第 59-63 行，`/Users/xianglei/Documents/develop/code/python/AgentOS/task/models.py` 第 25-29 行

### 6. 常用查询字段建立索引

在以下情况建立索引：

- 外键字段（`index=True`）
- 常用过滤条件字段（如 `status`, `is_deleted`, `suspended`）
- 唯一约束字段（`unique=True` 自动创建索引）
- 多列联合索引（通过 `__table_args__` 中的 `Index` 声明）

```python
from sqlalchemy import Index

class UserRecord(Base):
    __tablename__ = "users"
    __table_args__ = (
        Index("ix_users_auth_provider_provider_user_id", "auth_provider", "provider_user_id"),
        {"comment": "用户表"},
    )
    
    email: Mapped[str] = mapped_column(String(255), unique=True, index=True, comment="邮箱")
    suspended: Mapped[bool] = mapped_column(Boolean, default=False, index=True, comment="是否暂停")
```

**参考**: `/Users/xianglei/Documents/develop/code/python/AgentOS/user/models.py` 第 17-22 行

---

## ORM 实体注册

### 新增实体必须登记到 registry.py

**所有新增的 ORM 实体必须在 `database/registry.py` 中显式导入**，否则会导致：

1. **Alembic autogenerate 误判为删表**：registry 未登记的实体不在 `Base.metadata` 中，`alembic revision --autogenerate` 会生成 `drop_table` 操作
2. **跨包字符串式 relationship 解析失败**：如 `UserRecord.sessions` 指向 `"SessionRecord"`，SQLAlchemy 需要在映射配置时找到目标类，漏登记会报 `InvalidRequestError`

实体分散在各 feature 包（`user/`, `session/`, `task/` 等），导入单个模型不会连带注册其余实体。`database/registry.py` 是唯一汇总入口，`database/engine.py` 已通过副作用导入 registry，保证"能拿到 session 就一定已注册全部映射"。

**示例**（`/Users/xianglei/Documents/develop/code/python/AgentOS/database/registry.py`）：

```python
"""ORM 模型注册入口：导入所有实体，确保 Base.metadata 与映射关系完整。"""

from agent.models import AgentRecord, AgentSkillRecord, AgentToolRecord  # noqa: F401
from task.models import TaskRecord  # noqa: F401
from user.models import UserRecord  # noqa: F401
from session.models import SessionRecord, SessionMessage  # noqa: F401
# ... 所有实体必须在此导入
```

**新增实体后必须同步更新此文件**。

---

## Repository 层模式

### 1. Repository 持有 AsyncSession，不自行 commit

Repository 构造函数接收 `AsyncSession`，写操作使用 `add()` + `flush()` + `refresh()`，**绝不自行 `commit()`**：

```python
from sqlalchemy.ext.asyncio import AsyncSession
from user.models import UserRecord

class UserRepository:
    def __init__(self, db: AsyncSession):
        self.db = db

    async def create(self, username: str, email: str, password: str) -> UserRecord:
        user = UserRecord(username=username, email=email, password=password)
        self.db.add(user)
        await self.db.flush()  # 刷入数据库获取 ID，但不提交事务
        await self.db.refresh(user)  # 刷新实例获取服务端默认值
        return user

    async def update(self, user: UserRecord) -> UserRecord:
        await self.db.flush()
        await self.db.refresh(user)
        return user

    async def delete(self, user: UserRecord) -> None:
        """软删除用户"""
        user.is_deleted = True
        await self.db.flush()
```

**参考**: `/Users/xianglei/Documents/develop/code/python/AgentOS/user/repository.py`

### 2. 事务由 get_db() 统一管理

普通 HTTP 请求的事务生命周期由 `database/engine.py` 的 `get_db()` 依赖统一管理：

- 请求处理成功 → `await session.commit()`
- 任何异常 → `await session.rollback()`

这样跨 Service 的多步写入（如注册时创建 user + workspace + member）要么全部成功，要么全部回滚，不留孤儿数据。

```python
async def get_db() -> AsyncGenerator[AsyncSession]:
    """请求级事务边界:一次 HTTP 请求 = 一个事务。"""
    async with AsyncSessionLocal() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise
```

**参考**: `/Users/xianglei/Documents/develop/code/python/AgentOS/database/engine.py` 第 60-77 行

### 3. 流式/后台任务可分步 commit，需注释原因

**只有**流式 Agent 执行或后台运行任务为了增量持久化才能分步 `commit()`，并且必须注释生命周期原因：

```python
# 流式响应需要在每个步骤后 commit，确保前端能实时看到进度，
# 即使后续步骤失败也不会回滚已完成的部分。
async for chunk in agent_runtime.stream_execute():
    # ... 更新状态
    await session.commit()  # 增量持久化
    yield chunk
```

这类场景下，后续的 `get_db()` 收尾 commit 已无未提交变更，是安全的空操作。

**普通 HTTP 请求中的 Repository/Service 绝不允许 commit**。

---

## Query Patterns

### 1. 使用 SQLAlchemy 2 异步查询 API

所有查询使用 `select()` + `await db.scalars()` 或 `await db.execute()`：

```python
from sqlalchemy import select, desc
from session.models import SessionRecord

# 单条查询
stmt = select(SessionRecord).where(SessionRecord.id == session_id)
session = (await self.db.scalars(stmt)).first()

# 列表查询
stmt = (
    select(SessionRecord)
    .where(SessionRecord.user_id == user_id)
    .order_by(desc(SessionRecord.last_active_at))
    .limit(20)
)
sessions = list(await self.db.scalars(stmt))

# 主键查询
user = await self.db.get(UserRecord, user_id)
```

**参考**: `/Users/xianglei/Documents/develop/code/python/AgentOS/user/repository.py` 第 39-58 行

### 2. 批量操作

批量查询使用 `in_()` 过滤：

```python
async def list_by_ids(self, user_ids: list[int]) -> list[UserRecord]:
    if not user_ids:
        return []
    stmt = select(UserRecord).where(UserRecord.id.in_(user_ids)).order_by(UserRecord.id)
    return list(await self.db.scalars(stmt))
```

批量更新使用 `update()` 语句或循环修改实体后统一 flush。

---

## Migrations

### 1. 所有表结构变更必须通过 Alembic 迁移

新增、删除、修改表或列时，必须提供显式的 Alembic `upgrade()` 和 `downgrade()` 迁移：

```bash
# 生成迁移文件（autogenerate 作为起点，需手动检查和完善）
alembic revision --autogenerate -m "add_user_avatar_column"

# 执行迁移
alembic upgrade head

# 回滚迁移
alembic downgrade -1
```

**迁移文件示例**（`/Users/xianglei/Documents/develop/code/python/AgentOS/alembic/versions/0001_create_agentos_base_tables.py`）：

```python
"""创建 AgentOS 第一阶段基础表

Revision ID: 0001_create_agentos_base_tables
Revises:
Create Date: 2026-06-20
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "0001_create_agentos_base_tables"
down_revision = None

def upgrade() -> None:
    op.create_table(
        "sessions",
        sa.Column("id", sa.Integer(), primary_key=True, comment="会话ID"),
        sa.Column("title", sa.String(length=200), nullable=False, comment="会话标题"),
        # ...
        comment="会话主记录",
    )
    op.create_index("ix_sessions_status", "sessions", ["status"])

def downgrade() -> None:
    op.drop_index("ix_sessions_status", "sessions")
    op.drop_table("sessions")
```

### 2. 已进入迁移历史的文件不得删改

**已经提交到版本库的 Alembic 迁移文件不得直接删除或篡改**（会导致迁移链断裂和多环境不一致）。

数据库结构变更应通过**新的迁移**完成：

- 删除表/列 → 创建新迁移执行 `op.drop_table()` / `op.drop_column()`
- 修改列类型 → 创建新迁移执行 `op.alter_column()`
- 重命名表/列 → 创建新迁移执行 `op.rename_table()` / `op.alter_column(new_column_name=...)`

### 3. 迁移前检查 registry.py 是否完整

执行 `alembic revision --autogenerate` 前，确认 `database/registry.py` 已导入所有实体，否则会误判删表。

---

## Naming Conventions

### 表名和列名

- **表名**: 复数小写 + 下划线分隔（如 `users`, `session_messages`, `team_members`）
- **列名**: 小写 + 下划线分隔（如 `user_id`, `created_at`, `is_deleted`）
- **外键列**: `<关联表单数>_id`（如 `session_id`, `workspace_id`）
- **索引名**: `ix_<表名>_<列名>`（如 `ix_users_email`, `ix_sessions_status`）
- **外键约束名**: SQLAlchemy 自动生成，无需手动指定

### ORM 类名

- **实体类**: `<实体名>Record`（如 `UserRecord`, `SessionRecord`, `TaskRecord`）
- **Repository 类**: `<实体名>Repository`（如 `UserRepository`, `SessionRepository`）

---

## Common Mistakes

### 1. 忘记在 registry.py 登记新实体

**症状**: `alembic revision --autogenerate` 生成 `drop_table` 操作，或跨包 relationship 报 `InvalidRequestError`。

**解决**: 在 `/Users/xianglei/Documents/develop/code/python/AgentOS/database/registry.py` 添加导入：

```python
from your_feature.models import YourNewRecord  # noqa: F401
```

### 2. Repository 层自行 commit

**症状**: 事务边界混乱，部分操作成功部分失败，无法整体回滚。

**解决**: Repository 只使用 `add()` + `flush()` + `refresh()`，由 `get_db()` 统一 commit。

### 3. JSON 字段使用可变字面量默认值

**症状**: 多个实例共享同一个 dict/list 对象，修改一个实例影响其他实例。

**解决**: 使用 `MutableDict.as_mutable(json_type())` + `default=dict`。

### 4. 外键未指定 ondelete

**症状**: 删除父记录时数据库报外键约束错误，或子记录变成孤儿数据。

**解决**: 所有外键明确指定 `ondelete="CASCADE"` 或 `ondelete="SET NULL"`。

### 5. 直接删除 Alembic 迁移文件

**症状**: 迁移链断裂，`alembic upgrade` 报错，多环境数据库结构不一致。

**解决**: 通过新迁移执行 `op.drop_table()` 等操作，已提交的迁移文件不得删改。

### 6. 跨域直接使用别的域的 Repository

**症状**: 违反分层架构，业务规则分散，事务边界不清晰。

**解决**: feature 包之间只允许 Service 调 Service，本域 Service 才能访问本域 Repository。操作哪张表就归哪个域，需要读写别域数据时在数据所属域的 Service 上补公开方法。

---

## 总结

AgentOS 数据库规范的核心原则：

1. **使用 SQLAlchemy 2 异步 API**：`Mapped[T]` + `mapped_column`，不使用 v1 API
2. **新增实体必须登记 registry.py**：漏登记会导致 autogenerate 误判和 relationship 失败
3. **Repository 不自行 commit**：由 `get_db()` 统一管理事务生命周期
4. **表结构变更走 Alembic 迁移**：已提交的迁移文件不得删改
5. **表/列有中文 comment，外键明确 ondelete，JSON 字段用 Mutable 包装**
6. **默认继承 Base 的软删除机制**，不随意物理删除
7. **跨域调用走 Service 层**，不跨域直接使用 Repository
