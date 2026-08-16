# 目录结构规范

> AgentOS 后端代码的组织方式。

---

## 概述

AgentOS 采用**按业务域（Feature）组织代码**的方式，而非按技术层划分目录。

**核心原则**：
- 每个业务域是一个顶层包，包含该域的全部实现（API、Schema、Service、Repository、ORM Model）
- 分层（API → Service → Repository → ORM Model）是**依赖约定**，不是目录结构
- 禁止创建 `models/`、`services/`、`repositories/`、`schemas/` 这类按技术层划分的目录

**分层职责**：
1. **API 层**：路由、依赖注入、认证/权限入口、Schema 转换、响应包装
2. **Service 层**：业务校验、跨模块编排、事务边界控制
3. **Repository 层**：SQL 查询、持久化操作（持有 AsyncSession，通常使用 add/flush/refresh，不自行 commit）
4. **ORM Model 层**：数据库表映射、关系定义

**跨域调用规则**：
- feature 包之间只允许 Service 调 Service
- 不得跨域直接使用别的域的 Repository 或自行拼写别域表的 SQL
- 判断归属看表的所有者：操作哪张表就归哪个域

---

## 顶层目录布局

```
AgentOS/
├── user/                  # 用户域
├── task/                  # 任务域
├── team/                  # 团队/协作域
├── memory/                # 记忆域
├── core/                  # 核心基础设施（配置、异常、中间件、事件总线）
├── database/              # 数据库引擎、连接、ORM 注册表、Alembic 迁移
├── tools/                 # 工具系统（BaseTool、ToolRegistry、builtin 工具）
├── tests/                 # 测试用例
├── alembic/               # Alembic 迁移历史
├── main.py                # FastAPI 应用入口
├── pyproject.toml         # uv 依赖管理
├── .env                   # 本地环境配置（不提交）
├── .env.example           # 配置模板（提交到仓库）
└── CLAUDE.md              # 项目约定权威文档
```

---

## 业务域（Feature）包组织

每个业务域是一个顶层包，标准文件组织如下：

```
domain_name/
├── __init__.py          # 包导出（通常导出 Service、主要 Schema）
├── models.py            # ORM Model 定义
├── schemas.py           # Pydantic Schema（Request/Response/DTO）
├── repository.py        # Repository 实现（数据访问层）
├── service.py           # Service 实现（业务逻辑层）
└── api.py               # FastAPI 路由定义
```

**文件职责说明**：

- **models.py**：SQLAlchemy ORM Model，继承 `Base`，使用 `Mapped[T]` + `mapped_column`，包含表名、列定义、索引、外键、relationship
- **schemas.py**：Pydantic Schema，用于 API 请求/响应、数据传输，命名后缀：`*Request`、`*Response`、`*Record`
- **repository.py**：数据访问层，持有 `AsyncSession`，负责 SQL 查询和持久化，写操作使用 `add`/`flush`/`refresh`，不自行 `commit`
- **service.py**：业务逻辑层，处理业务校验、跨模块编排、事务边界控制；跨域调用只能调用其他域的 Service
- **api.py**：FastAPI 路由，负责依赖注入、认证/权限、Schema 转换、响应包装，使用 `ApiResponse[T]` 和 `ok(data, request)`

**复杂域的扩展**：
- 当某个域的逻辑复杂度增加时，可以引入子包或额外模块（如 `memory/jobs/`、`memory/recall/`、`team/isolation.py`）
- 但标准的 5 个文件必须存在于顶层，子包用于拆分辅助逻辑，不改变核心分层结构

---

## 真实示例

### 用户域（user/）

```
user/
├── __init__.py
├── models.py            # UserRecord ORM Model
├── schemas.py           # UserRequest, UserResponse, LoginRequest, LoginResponse
├── repository.py        # UserRepository
├── service.py           # UserService（注册、登录、用户管理）
└── api.py               # /api/v1/users 路由
```

路径：`/Users/xianglei/Documents/develop/code/python/AgentOS/user/`

### 任务域（task/）

```
task/
├── __init__.py
├── models.py            # TaskRecord ORM Model
├── schemas.py           # TaskRequest, TaskResponse
├── repository.py        # TaskRepository
├── service.py           # TaskService（任务创建、状态管理、认领）
└── api.py               # /api/v1/tasks 路由
```

路径：`/Users/xianglei/Documents/develop/code/python/AgentOS/task/`

### 记忆域（memory/）

```
memory/
├── __init__.py
├── models.py            # MemoryRecord, MemoryChunk ORM Models
├── schemas.py           # MemoryRequest, MemoryResponse, MemorySearchRequest
├── repository.py        # MemoryRepository
├── service.py           # MemoryService（记忆存储、检索、召回）
├── api.py               # /api/v1/memory 路由
├── rollout_service.py   # 辅助服务（记忆展开逻辑）
├── jobs/                # 子包：后台任务
└── recall/              # 子包：召回策略
```

路径：`/Users/xianglei/Documents/develop/code/python/AgentOS/memory/`

**说明**：memory 域引入了子包 `jobs/` 和 `recall/` 来拆分辅助逻辑，但核心 5 文件仍在顶层。

### 团队域（team/）

```
team/
├── __init__.py
├── models.py            # TeamRecord, TeamMemberRecord ORM Models
├── schemas.py           # TeamRequest, TeamResponse
├── repository.py        # TeamRepository
├── service.py           # TeamService（团队管理、成员管理）
├── api.py               # /api/v1/teams 路由
├── isolation.py         # 隔离策略实现
└── subagent_run_repository.py  # 子 Agent 运行记录 Repository
```

路径：`/Users/xianglei/Documents/develop/code/python/AgentOS/team/`

**说明**：team 域有额外的 Repository（`subagent_run_repository.py`）和辅助模块（`isolation.py`），但不改变核心分层结构。

---

## 命名约定

### 文件和目录

- **文件名**：使用 `snake_case`（如 `user_service.py`、`task_repository.py`）
- **目录名**：使用 `snake_case`（如 `memory/`, `user/`）
- **标准文件名**：`models.py`、`schemas.py`、`repository.py`、`service.py`、`api.py`
- **包导出**：`__init__.py` 导出该域的 Service 和主要 Schema

### 类

- **类名**：使用 `PascalCase`
- **ORM Model**：后缀 `Record`（如 `UserRecord`、`TaskRecord`）
- **Repository**：后缀 `Repository`（如 `UserRepository`、`TaskRepository`）
- **Service**：后缀 `Service`（如 `UserService`、`TaskService`）
- **Schema**：后缀 `Request`、`Response`、`Record`（如 `UserRequest`、`UserResponse`）
- **Tool**：后缀 `Tool`（如 `FileReadTool`、`WebSearchTool`）
- **工具输入**：后缀 `Input`（如 `FileReadInput`、`WebSearchInput`）

### 函数和变量

- **函数名**：使用 `snake_case`（如 `create_user`、`get_task_by_id`）
- **变量名**：使用 `snake_case`（如 `user_id`、`task_status`）
- **私有函数/变量**：前缀 `_`（如 `_validate_email`、`_internal_cache`）
- **常量**：使用 `UPPER_SNAKE_CASE`（如 `MAX_RETRY_COUNT`、`DEFAULT_TIMEOUT`）

### 类型注解

- 使用现代 Python 类型写法：
  - `T | None`（不用 `Optional[T]`）
  - `list[T]`（不用 `List[T]`）
  - `dict[str, Any]`（不用 `Dict[str, Any]`）
- SQLAlchemy 使用 `Mapped[T]` + `mapped_column`
- Pydantic 使用 `model_validate`、`model_dump`、`Field`

---

## 反模式（禁止的做法）

### ❌ 按技术层划分目录

**错误示例**：
```
# 禁止这样组织
AgentOS/
├── models/
│   ├── user.py
│   ├── task.py
│   └── memory.py
├── services/
│   ├── user_service.py
│   ├── task_service.py
│   └── memory_service.py
├── repositories/
│   ├── user_repository.py
│   ├── task_repository.py
│   └── memory_repository.py
└── schemas/
    ├── user_schema.py
    ├── task_schema.py
    └── memory_schema.py
```

**原因**：按技术层划分会导致：
- 同一业务逻辑分散在多个目录
- 跨域调用边界模糊
- 难以追踪业务流程
- 代码耦合度高

### ❌ 跨域直接访问 Repository

**错误示例**：
```python
# team/service.py 中直接使用 task/repository.py（禁止）
from task.repository import TaskRepository

class TeamService:
    def __init__(self, db: AsyncSession):
        self.task_repo = TaskRepository(db)  # ❌ 跨域直接访问 Repository
    
    async def claim_task(self, task_id: int):
        task = await self.task_repo.get_by_id(task_id)  # ❌ 违反分层约定
```

**正确做法**：
```python
# team/service.py 调用 task/service.py
from task.service import TaskService

class TeamService:
    def __init__(self, db: AsyncSession):
        self.task_service = TaskService(db)  # ✅ 跨域调用 Service
    
    async def claim_task(self, task_id: int):
        task = await self.task_service.get_task(task_id)  # ✅ 符合分层约定
```

### ❌ Repository 自行 commit

**错误示例**：
```python
# user/repository.py（禁止）
class UserRepository:
    async def create_user(self, user: UserRecord) -> UserRecord:
        self.db.add(user)
        await self.db.commit()  # ❌ Repository 不应自行 commit
        await self.db.refresh(user)
        return user
```

**正确做法**：
```python
# user/repository.py（推荐）
class UserRepository:
    async def create_user(self, user: UserRecord) -> UserRecord:
        self.db.add(user)
        await self.db.flush()    # ✅ 只 flush，不 commit
        await self.db.refresh(user)
        return user

# user/service.py 中由 get_db 统一管理事务
```

**例外情况**：流式 Agent 或后台任务需要增量持久化时可以分步 commit，但必须注释说明原因。

---

## 新增业务域的步骤

当需要添加新的业务域时，按以下步骤操作：

1. **创建顶层包**：在项目根目录创建 `domain_name/` 目录
2. **创建标准文件**：创建 `__init__.py`、`models.py`、`schemas.py`、`repository.py`、`service.py`、`api.py`
3. **定义 ORM Model**：在 `models.py` 中定义表结构，继承 `Base`
4. **注册 ORM**：在 `database/registry.py` 中导入并注册新 Model
5. **创建 Alembic 迁移**：运行 `alembic revision --autogenerate -m "add domain_name table"`
6. **实现 Repository**：在 `repository.py` 中实现数据访问逻辑
7. **实现 Service**：在 `service.py` 中实现业务逻辑
8. **定义 Schema**：在 `schemas.py` 中定义 Request/Response Schema
9. **实现 API**：在 `api.py` 中定义路由，挂载到主应用
10. **编写测试**：在 `tests/` 中添加测试用例

---

## 核心基础设施包

### core/

核心基础设施，不属于任何业务域，提供跨域能力。

```
core/
├── config.py            # Settings 配置类（从 .env 加载）
├── exceptions.py        # AgentException 等统一异常定义
├── middleware.py        # FastAPI 中间件
├── response.py          # ApiResponse[T] 统一响应包装
└── events.py            # 事件总线（EventBus）
```

路径：`/Users/xianglei/Documents/develop/code/python/AgentOS/core/`

### database/

数据库引擎、连接管理、ORM 注册表、Alembic 迁移。

```
database/
├── engine.py            # AsyncEngine、get_db 依赖注入
├── registry.py          # ORM Model 注册表（汇总所有域的 Model）
└── base.py              # Base、审计字段、软删除机制
```

路径：`/Users/xianglei/Documents/develop/code/python/AgentOS/database/`

**重要**：新增 ORM Model 后必须在 `registry.py` 中导入注册，否则 Alembic autogenerate 会误判为删表。

### tools/

工具系统，提供 Agent 可调用的工具能力。

```
tools/
├── base.py              # BaseTool 抽象基类、ToolContext
├── registry.py          # ToolRegistry 工具注册表
└── builtin/             # 内置工具实现
    ├── __init__.py      # 导出所有内置工具
    ├── file_read.py     # 文件读取工具
    ├── web_search.py    # 网络搜索工具
    └── ...
```

路径：`/Users/xianglei/Documents/develop/code/python/AgentOS/tools/`

**新增工具步骤**：
1. 继承 `BaseTool`
2. 定义 `Input` Schema（Pydantic BaseModel）
3. 实现 `async run(input: Input, context: ToolContext)`
4. 在 `builtin/__init__.py` 中导出
5. 在 `ToolRegistry` 中注册

---

## 配置文件

### .env 和 .env.example

- **`.env`**：本地实际配置，包含真实的数据库连接、密码、Token（不提交到版本库）
- **`.env.example`**：配置模板，只包含配置键和安全示例值（提交到版本库）

**同步规则**：
- `.env`、`.env.example` 与 `core/config.py` 中的 `Settings` 配置类必须始终保持同步
- 新增、删除、重命名配置项时，必须同时更新三处
- 环境变量使用 `AGENTOS_` 前缀

**示例**：

```bash
# .env（本地实际配置）
AGENTOS_DATABASE_URL=postgresql+asyncpg://user:password@localhost:5432/agentos
AGENTOS_REDIS_URL=redis://:password@localhost:6379/0
AGENTOS_ANTHROPIC_API_KEY=sk-ant-xxx

# .env.example（配置模板）
AGENTOS_DATABASE_URL=postgresql+asyncpg://user:password@localhost:5432/agentos
AGENTOS_REDIS_URL=redis://:password@localhost:6379/0
AGENTOS_ANTHROPIC_API_KEY=your-api-key-here
```

```python
# core/config.py
from pydantic_settings import BaseSettings

class Settings(BaseSettings):
    database_url: str
    redis_url: str
    anthropic_api_key: str
    
    class Config:
        env_prefix = "AGENTOS_"
        env_file = ".env"
```

### pyproject.toml

项目依赖管理文件，使用 `uv` 作为包管理工具。

**主要依赖**：
- FastAPI
- Pydantic v2
- SQLAlchemy 2 Async
- PostgreSQL (asyncpg)
- Alembic
- Anthropic SDK
- pytest

---

## 导入规范

### 导入顺序

按以下顺序分组，组间留空行：

1. **标准库导入**
2. **第三方库导入**
3. **项目内绝对导入**

**示例**：

```python
# 标准库
import logging
from datetime import datetime
from typing import Any

# 第三方库
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession
from pydantic import BaseModel, Field

# 项目内绝对导入
from core.response import ApiResponse, ok
from core.exceptions import AgentException
from database.engine import get_db
from user.service import UserService
from user.schemas import UserRequest, UserResponse
```

### 跨域导入规则

- **ORM Model**：可以跨域导入（外键、relationship 需要）
- **Service**：可以跨域调用其他域的 Service
- **Repository**：只能在本域 Service 中使用，不得跨域调用
- **Schema**：可以跨域导入，但优先返回本域 Schema

---

## 总结

AgentOS 的目录结构遵循以下核心原则：

1. **按业务域组织**：每个域是一个顶层包，包含全部实现
2. **分层是依赖约定**：API → Service → Repository → ORM Model
3. **跨域调用规则**：Service 调 Service，不跨域调 Repository
4. **禁止按技术层划分**：不创建 models/、services/、repositories/ 目录
5. **配置同步**：`.env`、`.env.example`、`core/config.py` 必须保持一致
6. **ORM 注册**：新增 Model 必须在 `database/registry.py` 中注册

**参考文档**：
- 项目约定权威文档：`/Users/xianglei/Documents/develop/code/python/AgentOS/CLAUDE.md`
- 真实示例：`user/`、`task/`、`memory/`、`team/` 域
