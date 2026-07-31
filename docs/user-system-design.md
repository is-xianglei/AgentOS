# AgentOS 企业级用户系统设计方案

## 一、概述

本文档设计 AgentOS 的企业级用户系统，覆盖用户认证、授权、工作区架构、资源隔离、计费追踪等核心模块。该系统基于现有的 FastAPI + PostgreSQL + SQLAlchemy 技术栈，与当前的 Session、Team、Permission 模块无缝集成。

### 设计原则

1. **多租户隔离**：支持 B2B SaaS 和私有部署两种模式
2. **RBAC + ABAC 混合**：角色权限 + 属性权限，适配复杂企业场景
3. **渐进式实现**：按 MVP → 企业版 → 高级特性分阶段交付
4. **安全优先**：密码加密、JWT 双 Token、敏感操作二次验证
5. **可观测性**：资源使用可计量

---

## 二、系统架构

### 2.1 核心模块

```
用户系统
├── 认证模块 (Authentication)
│   ├── 用户注册/登录
│   ├── JWT Token 管理
│   └── OAuth2/SAML/LDAP 集成
├── 授权模块 (Authorization)
│   ├── RBAC 角色权限
│   ├── ABAC 属性权限
│   ├── 资源权限控制
│   └── API 限流与配额
├── 工作区模块 (Workspace)
│   ├── 工作区管理（个人/团队/企业）
│   ├── 成员管理
│   └── 邀请与审批
└── 计费模块 (Billing)
    ├── 使用量统计
    ├── 配额管理
    ├── 订阅计划
    └── 成本分摊
```

### 2.2 与现有模块的关系

```
User (新增)
  ↓ 1:N
Workspace (新增)
  ↓ 1:N
Session (已有) → 新增 user_id, workspace_id 字段
  ↓ 1:N
Message / Team / Task / ToolCall (已有)
```

---

## 三、数据模型设计

### 3.1 用户表 (users)

```python
class User(Base):
    __tablename__ = "users"
    __table_args__ = {"comment": "用户表"}

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    email: Mapped[str] = mapped_column(String(255), unique=True, index=True, comment="邮箱(唯一登录标识)")
    username: Mapped[str] = mapped_column(String(64), unique=True, index=True, comment="用户名")
    password: Mapped[str] = mapped_column(String(255), comment="密码哈希(MD5)")
    full_name: Mapped[str | None] = mapped_column(String(128), nullable=True, comment="真实姓名")
    avatar_url: Mapped[str | None] = mapped_column(String(512), nullable=True, comment="头像URL")
    phone: Mapped[str | None] = mapped_column(String(32), nullable=True, comment="手机号")
    
    # 状态字段
    email_verified: Mapped[bool] = mapped_column(default=False, comment="邮箱是否验证")
    suspended: Mapped[bool] = mapped_column(default=False, index=True, comment="是否被暂停使用")
    
    # OAuth 字段
    auth_provider: Mapped[str] = mapped_column(String(32), default="local", comment="认证提供商: local/google/github/saml")
    provider_user_id: Mapped[str | None] = mapped_column(String(255), nullable=True, comment="第三方用户ID")
    
    # 偏好设置
    preferences: Mapped[dict] = mapped_column(json_type(), default=dict, comment="用户偏好设置")
    
    # 时间戳（继承自 Base: created_at, updated_at, is_deleted, deleted_at）
    last_login_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True, comment="最后登录时间")
    
    # 关联关系
    workspace_memberships: Mapped[list["WorkspaceMember"]] = relationship(back_populates="user")
    sessions: Mapped[list["SessionRecord"]] = relationship(back_populates="user")
```

**索引策略**：
- `email` 唯一索引（登录查询）
- `username` 唯一索引（用户名查询）
- `suspended` 普通索引（筛选被暂停用户）
- `is_deleted` 普通索引（继承自 Base，筛选有效用户）
- `(auth_provider, provider_user_id)` 复合索引（OAuth 登录）

### 3.2 工作区表 (workspaces)

```python
class Workspace(Base):
    __tablename__ = "workspaces"
    __table_args__ = {"comment": "工作区表（个人/团队/企业）"}

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(255), unique=True, index=True, comment="工作区名称")
    slug: Mapped[str] = mapped_column(String(64), unique=True, index=True, comment="工作区标识(URL友好)")
    display_name: Mapped[str] = mapped_column(String(255), comment="显示名称")
    logo_url: Mapped[str | None] = mapped_column(String(512), nullable=True)
    
    # 类型与状态
    workspace_type: Mapped[str] = mapped_column(String(32), default="personal", comment="类型: personal/team/enterprise")
    suspended: Mapped[bool] = mapped_column(default=False, index=True, comment="是否被暂停使用")
    
    # 企业信息
    industry: Mapped[str | None] = mapped_column(String(64), nullable=True, comment="所属行业")
    company_size: Mapped[str | None] = mapped_column(String(32), nullable=True, comment="公司规模")
    billing_email: Mapped[str | None] = mapped_column(String(255), nullable=True)
    
    # 配额与计费
    plan: Mapped[str] = mapped_column(String(32), default="free", comment="订阅计划: free/pro/enterprise")
    quotas: Mapped[dict] = mapped_column(json_type(), default=dict, comment="配额配置 {sessions_per_month: 100}")
    
    # 设置
    settings: Mapped[dict] = mapped_column(json_type(), default=dict, comment="工作区设置")
    
    # 时间戳（继承自 Base: created_at, updated_at, is_deleted, deleted_at）
    
    # 关联关系
    members: Mapped[list["WorkspaceMember"]] = relationship(back_populates="workspace")
    sessions: Mapped[list["SessionRecord"]] = relationship(back_populates="workspace")
```

### 3.3 工作区成员表 (workspace_members)

```python
class WorkspaceMember(Base):
    __tablename__ = "workspace_members"
    __table_args__ = (
        Index("ix_workspace_members_workspace_user", "workspace_id", "user_id", unique=True),
        {"comment": "工作区成员关系表"}
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    workspace_id: Mapped[int] = mapped_column(ForeignKey("workspaces.id", ondelete="CASCADE"), index=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    
    # 邀请与加入
    invited_by: Mapped[int | None] = mapped_column(ForeignKey("users.id"), nullable=True, comment="邀请人用户ID")
    invited_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True, comment="邀请时间")
    joined_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True, comment="加入时间(接受邀请的时间)")
    
    # 时间戳（继承自 Base: created_at, updated_at, is_deleted, deleted_at）
    # 注：is_deleted=true 表示成员已被移除
    
    # 关联关系
    workspace: Mapped["Workspace"] = relationship(back_populates="members")
    user: Mapped["User"] = relationship(back_populates="workspace_memberships")
```

### 3.4 角色表 (roles)

> **⚠️ 此表暂不实现** - 当前版本不包含自定义角色功能

```python
class Role(Base):
    __tablename__ = "roles"
    __table_args__ = (
        Index("ix_roles_workspace_name", "workspace_id", "name", unique=True),
        {"comment": "自定义角色表"}
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    workspace_id: Mapped[int | None] = mapped_column(
        ForeignKey("workspaces.id", ondelete="CASCADE"), 
        nullable=True, 
        index=True,
        comment="所属工作区ID(NULL表示系统预设角色)"
    )
    name: Mapped[str] = mapped_column(String(64), comment="角色名称")
    display_name: Mapped[str] = mapped_column(String(128), comment="显示名称")
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    
    # 权限定义
    permissions: Mapped[list[str]] = mapped_column(json_type(), default=list, comment="权限列表")
    is_system: Mapped[bool] = mapped_column(default=False, comment="是否系统角色(不可删除)")
    
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())
```

### 3.5 会话表扩展 (sessions - 修改现有表)

```python
# 在现有 SessionRecord 基础上新增字段
class SessionRecord(Base):
    # ... 现有字段保持不变 ...
    
    # 新增用户关联
    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), 
        index=True, 
        comment="创建用户ID"
    )
    workspace_id: Mapped[int] = mapped_column(
        ForeignKey("workspaces.id", ondelete="CASCADE"), 
        index=True, 
        comment="所属工作区ID"
    )
    
    # 新增共享与权限
    visibility: Mapped[str] = mapped_column(
        String(32), 
        default="private", 
        index=True, 
        comment="可见性: private/team/workspace/public"
    )
    shared_with: Mapped[list[int]] = mapped_column(
        json_type(), 
        default=list, 
        comment="共享用户ID列表"
    )
    
    # 关联关系
    user: Mapped["User"] = relationship(back_populates="sessions")
    workspace: Mapped["Workspace"] = relationship(back_populates="sessions")
```

### 3.6 使用量统计表 (usage_records)

```python
class UsageRecord(Base):
    __tablename__ = "usage_records"
    __table_args__ = (
        Index("ix_usage_workspace_date", "workspace_id", "date"),
        Index("ix_usage_user_date", "user_id", "date"),
        {"comment": "使用量统计表"}
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    workspace_id: Mapped[int] = mapped_column(ForeignKey("workspaces.id", ondelete="CASCADE"), index=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    
    # 时间维度
    date: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True, comment="统计日期")
    hour: Mapped[int | None] = mapped_column(Integer, nullable=True, comment="小时(0-23)")
    
    # 计量维度
    metric_type: Mapped[str] = mapped_column(String(64), comment="指标类型: token_usage/session_count/tool_calls")
    metric_value: Mapped[int] = mapped_column(Integer, default=0, comment="指标数值")
    
    # 关联资源
    resource_type: Mapped[str | None] = mapped_column(String(64), nullable=True)
    resource_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    
    # 成本
    cost: Mapped[float] = mapped_column(default=0.0, comment="成本(美元)")
    
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
```

---

## 四、认证系统设计

### 4.1 认证流程

#### 本地认证流程

```
1. 用户注册
   POST /api/auth/register
   {
     "email": "user@example.com",
     "username": "johndoe",
     "password": "SecurePass123!",
     "full_name": "John Doe"
   }
   ↓
   - 验证邮箱格式与唯一性
   - MD5 加密密码
   - 创建用户记录 (suspended=false, email_verified=false)
   - 发送验证邮件
   - 返回 access_token + refresh_token

2. 用户登录
   POST /api/auth/login
   {
     "email": "user@example.com",
     "password": "SecurePass123!"
   }
   ↓
   - 验证邮箱密码
   - 检查用户状态 (suspended/deleted 拒绝登录)
   - 生成 JWT access_token (15min) + refresh_token (7days)
   - 记录 last_login_at
   - 返回 tokens + user info

3. Token 刷新
   POST /api/auth/refresh
   {
     "refresh_token": "xxx"
   }
   ↓
   - 验证 refresh_token 签名与过期时间
   - 检查 token 是否在黑名单
   - 生成新的 access_token
   - 返回新 token
```

#### OAuth2 集成流程

```
1. 发起 OAuth 登录
   GET /api/auth/oauth/{provider}/authorize
   ↓
   - 重定向到 Google/GitHub/SAML 授权页
   - state 参数防 CSRF

2. OAuth 回调
   GET /api/auth/oauth/{provider}/callback?code=xxx&state=xxx
   ↓
   - 验证 state
   - 用 code 换取 access_token
   - 获取用户信息 (email, name, avatar)
   - 查找或创建用户 (auth_provider=google, provider_user_id=xxx)
   - 生成系统 JWT tokens
   - 返回 tokens
```

### 4.2 JWT Token 结构

```python
# Access Token Payload
{
  "sub": "123",  # user_id
  "email": "user@example.com",
  "username": "johndoe",
  "workspace_id": 456,  # 当前组织ID
  "role": "member",  # 当前组织角色
  "permissions": ["session:read", "session:write"],
  "type": "access",
  "exp": 1234567890,  # 15分钟后过期
  "iat": 1234567000,
  "jti": "unique-token-id"  # 用于撤销
}

# Refresh Token Payload
{
  "sub": "123",
  "type": "refresh",
  "exp": 1235000000,  # 7天后过期
  "jti": "unique-refresh-id"
}
```

### 4.3 密码策略

- **强度要求**：
  - 最少 8 字符
  - 包含大小写字母、数字
  - 可选特殊字符要求（企业版）
  
- **加密算法**：MD5

- **密码重置流程**：
  1. 用户提交邮箱
  2. 生成重置 token (有效期 1 小时)
  3. 发送重置链接邮件
  4. 用户点击链接，提交新密码
  5. 验证 token，更新密码哈希

---

## 五、授权系统设计

> **⚠️ 注意：权限和角色功能暂不实现**  
> 本章节内容（第五章）作为未来规划保留，当前版本不包含 RBAC/ABAC 权限系统。

### 5.1 权限模型（RBAC + ABAC）

#### 系统预设角色

| 角色 | 权限范围 | 典型操作 |
|------|---------|---------|
| **owner** | 组织所有权限 | 删除组织、修改计费、管理成员 |
| **admin** | 管理权限 | 邀请成员、创建角色、管理组织设置 |
| **member** | 标准权限 | 创建会话、使用工具、查看团队会话 |
| **guest** | 只读权限 | 查看共享会话、无法创建/修改 |

#### 权限粒度设计

```python
# 权限格式: {resource}:{action}
PERMISSIONS = [
    # 会话权限
    "session:create",
    "session:read",
    "session:update",
    "session:delete",
    "session:share",
    
    # 工具权限
    "tool:execute",
    "tool:manage",  # 配置工具权限规则
    
    # 团队权限
    "team:create",
    "team:read",
    "team:manage",
    
    # 组织权限
    "org:read",
    "org:update",
    "org:billing",
    "org:members:invite",
    "org:members:remove",
    "org:roles:manage",
    "org:settings:manage",
]
```

#### 资源所有权检查

```python
# 伪代码
async def check_permission(user: User, action: str, resource: Resource) -> bool:
    # 1. 检查用户是否属于资源所在工作区
    if resource.workspace_id not in user.workspace_ids:
        return False
    
    # 2. 获取用户在该工作区的角色
    member = await get_workspace_member(resource.workspace_id, user.id)
    role_permissions = ROLE_PERMISSIONS[member.role]
    
    # 3. 检查角色权限
    if action in role_permissions or f"{resource.type}:*" in role_permissions:
        return True
    
    # 4. 检查额外权限
    if action in member.permissions:
        return True
    
    # 5. 检查资源所有权（ABAC）
    if resource.user_id == user.id and action in ["read", "update", "delete"]:
        return True
    
    # 6. 检查共享权限
    if resource.visibility == "workspace":
        return action == "read"
    if user.id in resource.shared_with:
        return action == "read"
    
    return False
```

### 5.2 API 权限装饰器

```python
# services/auth/permission.py
from functools import wraps
from fastapi import Depends, HTTPException

async def require_permission(permission: str):
    """权限检查依赖"""
    async def checker(current_user: User = Depends(get_current_user)):
        if not await has_permission(current_user, permission):
            raise HTTPException(status_code=403, detail="Permission denied")
        return current_user
    return checker

# 使用示例
@router.post("/sessions")
async def create_session(
    payload: SessionCreate,
    user: User = Depends(require_permission("session:create"))
):
    return await session_service.create(user, payload)
```

### 5.3 资源级权限检查

```python
async def check_resource_access(
    user: User,
    resource_type: str,
    resource_id: int,
    action: str
) -> bool:
    """检查用户对特定资源的访问权限"""
    
    # 加载资源
    resource = await load_resource(resource_type, resource_id)
    if not resource:
        return False
    
    # Owner 检查
    if resource.user_id == user.id:
        return action in ["read", "update", "delete"]
    
    # 工作区成员检查
    member = await get_workspace_member(resource.workspace_id, user.id)
    if not member:
        return False
    
    # 角色权限检查
    required_perm = f"{resource_type}:{action}"
    if required_perm in get_role_permissions(member.role):
        return True
    
    # 共享权限检查
    if action == "read":
        if resource.visibility == "workspace":
            return True
        if user.id in resource.shared_with:
            return True
    
    return False
```

---

## 六、工作区管理设计

### 6.1 工作区生命周期

```
1. 创建工作区
   POST /api/workspaces
   {
     "name": "Acme Corp",
     "workspace_type": "team"
   }
   ↓
   - 创建 Workspace 记录
   - 将创建者加入为 owner
   - 初始化默认配额

2. 邀请成员
   POST /api/workspaces/{workspace_id}/members/invite
   {
     "email": "newuser@example.com",
     "role": "member"
   }
   ↓
   - 查找用户（不存在则创建待激活用户）
   - 创建 WorkspaceMember (joined_at 为空表示待接受邀请)
   - 发送邀请邮件（带 token）
   - 用户点击链接后设置 joined_at

3. 移除成员
   DELETE /api/workspaces/{workspace_id}/members/{user_id}
   ↓
   - 检查操作者权限 (admin/owner)
   - 软删除 WorkspaceMember 记录（设置 is_deleted=true）
   - 可选：保留用户创建的资源 或 转移给其他成员
```

### 6.2 工作区切换

```python
# 用户可能属于多个工作区
GET /api/users/me/workspaces
→ [
  {"id": 1, "name": "个人工作区", "role": "owner", "workspace_type": "personal"},
  {"id": 2, "name": "Acme Corp", "role": "member", "workspace_type": "team"}
]

# 切换当前工作区（重新签发 token）
POST /api/auth/switch-workspace
{"workspace_id": 2}
↓
- 验证用户是该工作区成员
- 生成新的 access_token（workspace_id=2, role=member）
- 返回新 token
```

### 6.3 配额管理

```python
# 工作区配额示例
{
  "sessions_per_month": 100,       # 每月会话数
  "tokens_per_month": 1000000,     # 每月 token 数
  "max_team_members": 5,           # 最大成员数
  "max_concurrent_sessions": 10,   # 最大并发会话
  "tool_access": ["echo", "Weather"],  # 可用工具白名单
  "storage_gb": 10                 # 存储空间
}

# 配额检查中间件
async def check_quota(workspace_id: int, metric: str, delta: int = 1):
    usage = await get_monthly_usage(workspace_id, metric)
    quota = await get_workspace_quota(workspace_id, metric)
    
    if usage + delta > quota:
        raise QuotaExceededError(
            f"{metric} quota exceeded: {usage}/{quota}"
        )
    
    await increment_usage(workspace_id, metric, delta)
```

---

## 七、计费与统计

### 7.1 使用量追踪

```python
# 在 LLM 调用后记录使用量
async def track_usage(
    workspace_id: int,
    user_id: int,
    session_id: int,
    tokens: int,
    cost: float
):
    await UsageRecord.create(
        workspace_id=workspace_id,
        user_id=user_id,
        date=datetime.now(),
        hour=datetime.now().hour,
        metric_type="token_usage",
        metric_value=tokens,
        resource_type="session",
        resource_id=str(session_id),
        cost=cost
    )
```

### 7.2 订阅计划

| 计划 | 价格 | 会话数 | Token | 成员数 | 特性 |
|------|------|--------|-------|--------|------|
| **Free** | $0 | 10/月 | 10K/月 | 1 | 基础工具 |
| **Pro** | $29/月 | 100/月 | 500K/月 | 5 | 所有工具 + 优先级支持 |
| **Team** | $99/月 | 500/月 | 2M/月 | 20 | + 自定义工具 + SSO |
| **Enterprise** | 定制 | 无限 | 无限 | 无限 | + 私有部署 + SLA |

### 7.3 成本分摊

```python
# 按用户统计成本
GET /api/workspaces/{workspace_id}/usage/by-user?start=2024-07&end=2024-07
→ [
  {"user_id": 1, "username": "alice", "tokens": 50000, "cost": 0.5},
  {"user_id": 2, "username": "bob", "tokens": 30000, "cost": 0.3}
]

# 按会话统计成本
GET /api/workspaces/{workspace_id}/usage/by-session?start=2024-07&end=2024-07
→ [
  {"session_id": 123, "title": "数据分析", "tokens": 20000, "cost": 0.2},
  ...
]
```

---

## 八、API 设计

### 8.1 认证相关 API

```
POST   /api/auth/register              注册
POST   /api/auth/login                 登录
POST   /api/auth/logout                登出
POST   /api/auth/refresh               刷新 Token
POST   /api/auth/forgot-password       发起密码重置
POST   /api/auth/reset-password        确认密码重置
POST   /api/auth/verify-email          验证邮箱

# OAuth
GET    /api/auth/oauth/{provider}/authorize    发起 OAuth
GET    /api/auth/oauth/{provider}/callback     OAuth 回调
```

### 8.2 用户相关 API

```
GET    /api/users/me                   当前用户信息
PATCH  /api/users/me                   更新用户信息
DELETE /api/users/me                   删除账号
POST   /api/users/me/change-password   修改密码
GET    /api/users/me/workspaces        用户加入的工作区列表
GET    /api/users/me/sessions          用户的会话列表
```

### 8.3 工作区相关 API

```
POST   /api/workspaces              创建工作区
GET    /api/workspaces              工作区列表
GET    /api/workspaces/{id}         工作区详情
PATCH  /api/workspaces/{id}         更新工作区
DELETE /api/workspaces/{id}         删除工作区

# 成员管理
GET    /api/workspaces/{id}/members            成员列表
POST   /api/workspaces/{id}/members/invite     邀请成员
DELETE /api/workspaces/{id}/members/{user_id}  移除成员
PATCH  /api/workspaces/{id}/members/{user_id}  更新成员角色

# 角色管理
GET    /api/workspaces/{id}/roles              角色列表
POST   /api/workspaces/{id}/roles              创建角色
PATCH  /api/workspaces/{id}/roles/{role_id}    更新角色
DELETE /api/workspaces/{id}/roles/{role_id}    删除角色

# 配额与使用量
GET    /api/workspaces/{id}/quotas             配额信息
GET    /api/workspaces/{id}/usage              使用量统计
GET    /api/workspaces/{id}/usage/by-user      按用户统计
GET    /api/workspaces/{id}/usage/by-session   按会话统计
```

---

## 九、实施路线图

### Phase 1: MVP（2-3 周）

**目标**：单用户系统，基础认证

- [ ] User 表与 SQLAlchemy 模型
- [ ] 用户注册/登录 API（邮箱+密码）
- [ ] JWT Token 生成与验证
- [ ] 密码加密（MD5）
- [ ] Session 表添加 user_id 字段
- [ ] API 依赖注入：`get_current_user`
- [ ] 基础权限检查：会话所有权验证

**交付物**：
- 用户可以注册、登录
- 创建的会话归属于用户
- 只能访问自己的会话

### Phase 2: 工作区与多租户（2-3 周）

**目标**：支持团队协作

- [ ] Workspace 表与模型
- [ ] WorkspaceMember 关系表
- [ ] 工作区 CRUD API
- [ ] 成员邀请/管理 API
- [ ] Session 表添加 workspace_id
- [ ] 工作区切换功能
- [ ] 基于工作区的资源隔离

**交付物**：
- 用户可以创建工作区
- 邀请成员加入工作区
- 工作区内会话共享
- 按工作区隔离数据

### Phase 3: 权限系统（2 周）

> **⚠️ 此阶段暂不实施** - 权限系统推迟到后续版本

**目标**：RBAC 权限系统

- [ ] Role 表与预设角色
- [ ] 权限检查装饰器
- [ ] 资源级权限验证

**交付物**：
- owner/admin/member 角色生效
- 权限拒绝返回 403

### Phase 4: 计费与配额（1-2 周）

**目标**：使用量统计与限制

- [ ] UsageRecord 表与统计逻辑
- [ ] 配额检查中间件
- [ ] Token 使用量追踪
- [ ] 使用量统计 API
- [ ] 配额超限提示

**交付物**：
- 按工作区统计 Token 使用量
- 达到配额后阻止新请求
- 使用量报表 API

### Phase 5: 企业特性（2-3 周）

> **⚠️ 此阶段暂不实施** - 企业特性推迟到后续版本

**目标**：企业级功能

- [ ] OAuth2 集成（Google/GitHub）
- [ ] SAML SSO 集成
- [ ] 自定义角色

**交付物**：
- 支持 Google/GitHub 登录
- 企业 SSO 集成

### Phase 6: 优化与监控（持续）

**目标**：性能与可观测性

- [ ] Redis 缓存（用户信息、权限）
- [ ] 数据库查询优化
- [ ] Prometheus 指标导出
- [ ] 异常告警
- [ ] 性能监控

---

## 十、安全考虑

### 10.1 密码安全

- ✅ MD5 加密
- ✅ 密码强度验证
- ✅ 密码重置 Token 有效期 1 小时
- ✅ 登录失败限流（同一 IP 5 次/分钟）

### 10.2 Token 安全

- ✅ Access Token 短生命周期（15 分钟）
- ✅ Refresh Token 长生命周期（7 天）
- ✅ Token 撤销机制（Redis 黑名单）
- ✅ JWT 签名验证（HS256 或 RS256）
- ✅ jti 唯一标识防重放

### 10.3 API 安全

- ✅ HTTPS Only（生产环境）
- ✅ CORS 配置（白名单域名）
- ✅ Rate Limiting（按 IP 和用户）
- ✅ SQL 注入防护（SQLAlchemy ORM）
- ✅ XSS 防护（输入验证）
- ✅ CSRF Token（状态变更操作）

### 10.4 数据安全

- ✅ 数据库连接加密（SSL）
- ✅ 定期备份
- ✅ 软删除（用户/组织）

### 10.5 合规性

- ✅ GDPR 数据删除（Right to be forgotten）
- ✅ 密码策略可配置
- ✅ 会话超时可配置

---

## 十一、数据库迁移

### Alembic 迁移脚本顺序

```bash
# 1. 创建用户表
alembic revision -m "create_users_table"

# 2. 创建工作区表
alembic revision -m "create_workspaces_table"

# 3. 创建工作区成员表
alembic revision -m "create_workspace_members_table"

# 4. 修改会话表（添加 user_id, workspace_id）
alembic revision -m "add_user_workspace_to_sessions"

# 5. 创建角色表
alembic revision -m "create_roles_table"

# 6. 创建使用量表
alembic revision -m "create_usage_records_table"
```

### 数据迁移注意事项

**现有会话数据处理**：
1. 创建默认用户（system user）
2. 创建默认工作区（default workspace）
3. 将现有会话关联到默认用户和工作区
4. 后续允许用户认领会话

---

## 十二、配置管理

### 环境变量

```bash
# 数据库
AGENTOS_DATABASE_URL=postgresql://user:pass@localhost/agentos

# JWT 配置
JWT_SECRET_KEY=your-secret-key-change-in-production
JWT_ALGORITHM=HS256
JWT_ACCESS_TOKEN_EXPIRE_MINUTES=15
JWT_REFRESH_TOKEN_EXPIRE_DAYS=7

# OAuth 配置
GOOGLE_CLIENT_ID=xxx
GOOGLE_CLIENT_SECRET=xxx
GITHUB_CLIENT_ID=xxx
GITHUB_CLIENT_SECRET=xxx

# 邮件服务
SMTP_HOST=smtp.gmail.com
SMTP_PORT=587
SMTP_USERNAME=noreply@example.com
SMTP_PASSWORD=xxx
SMTP_FROM=noreply@example.com

# Redis（Token 黑名单、缓存）
REDIS_URL=redis://localhost:6379/0

# 安全配置
PASSWORD_MIN_LENGTH=8
LOGIN_RATE_LIMIT=5  # 每分钟
API_RATE_LIMIT=100  # 每分钟

# 计费配置
FREE_PLAN_SESSIONS=10
FREE_PLAN_TOKENS=10000
PRO_PLAN_SESSIONS=100
PRO_PLAN_TOKENS=500000
```

---

## 十三、测试策略

### 单元测试

```python
# tests/test_auth.py
async def test_register_user():
    response = await client.post("/api/auth/register", json={
        "email": "test@example.com",
        "username": "testuser",
        "password": "SecurePass123!"
    })
    assert response.status_code == 201
    assert "access_token" in response.json()

async def test_login_invalid_password():
    response = await client.post("/api/auth/login", json={
        "email": "test@example.com",
        "password": "WrongPassword"
    })
    assert response.status_code == 401

async def test_permission_denied():
    # 用户 A 尝试访问用户 B 的会话
    response = await client.get(
        "/api/sessions/123",
        headers={"Authorization": f"Bearer {user_a_token}"}
    )
    assert response.status_code == 403
```

### 集成测试

```python
# tests/test_workspace_flow.py
async def test_workspace_workflow():
    # 1. 创建工作区
    workspace = await create_workspace(owner_user)
    
    # 2. 邀请成员
    invite = await invite_member(workspace.id, "member@example.com", role="member")
    
    # 3. 成员接受邀请
    await accept_invite(invite.token)
    
    # 4. 成员创建会话
    session = await create_session(member_user, workspace.id)
    
    # 5. Owner 可以查看成员会话
    response = await get_session(owner_user, session.id)
    assert response.status_code == 200
    
    # 6. Guest 用户无法查看
    response = await get_session(guest_user, session.id)
    assert response.status_code == 403
```

### 性能测试

```python
# tests/test_performance.py
async def test_login_performance():
    """登录接口响应时间 < 200ms"""
    start = time.time()
    await client.post("/api/auth/login", json={"email": "...", "password": "..."})
    elapsed = time.time() - start
    assert elapsed < 0.2

async def test_permission_check_performance():
    """权限检查 < 50ms"""
    start = time.time()
    await check_permission(user, "session:read", session)
    elapsed = time.time() - start
    assert elapsed < 0.05
```

---

## 十四、监控指标

### 关键指标

```python
# Prometheus 指标定义
auth_login_total = Counter("auth_login_total", "Total login attempts", ["status"])
auth_login_duration = Histogram("auth_login_duration_seconds", "Login duration")
permission_check_total = Counter("permission_check_total", "Permission checks", ["result"])
session_create_total = Counter("session_create_total", "Sessions created")
workspace_member_count = Gauge("workspace_member_count", "Workspace member count", ["workspace_id"])
quota_usage_ratio = Gauge("quota_usage_ratio", "Quota usage ratio", ["workspace_id", "metric"])

# 告警规则
- auth_login_failure_rate > 10% (last 5min)
- permission_check_latency_p95 > 100ms
- quota_usage_ratio > 0.9
- active_users_drop > 20% (last 1hour)
```

---

## 十五、常见问题

### Q1: 用户可以属于多个工作区吗？
**A**: 是的，通过 `workspace_members` 表支持多对多关系。用户在不同工作区有不同角色。

### Q2: 如何处理工作区 Owner 离职？
**A**: 
- Owner 删除账号前必须转移所有权给其他成员
- 系统强制工作区至少有一个 Owner
- 企业版支持自动继任规则

### Q3: 如何防止配额滥用？
**A**:
- 每个请求检查配额剩余量
- 达到 80% 发送警告邮件
- 达到 100% 拒绝新请求（返回 429）
- 企业版支持自动升级

---

## 十六、参考资料

### 技术栈文档
- [FastAPI Security](https://fastapi.tiangolo.com/tutorial/security/)
- [SQLAlchemy 2.0 Documentation](https://docs.sqlalchemy.org/en/20/)
- [PyJWT](https://pyjwt.readthedocs.io/)
- [python-jose](https://python-jose.readthedocs.io/)
- [hashlib (MD5)](https://docs.python.org/3/library/hashlib.html)

### 安全标准
- [OWASP Top 10](https://owasp.org/www-project-top-ten/)
- [OWASP Authentication Cheat Sheet](https://cheatsheetseries.owasp.org/cheatsheets/Authentication_Cheat_Sheet.html)
- [OAuth 2.0 RFC 6749](https://datatracker.ietf.org/doc/html/rfc6749)
- [JWT Best Practices RFC 8725](https://datatracker.ietf.org/doc/html/rfc8725)

### 合规性
- [GDPR Overview](https://gdpr.eu/)

---

## 附录 A: 完整代码示例

### A.1 用户认证服务

```python
# services/auth_service.py
from datetime import datetime, timedelta
from typing import Optional
import hashlib
import jwt
from fastapi import HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession

from models.user import User
from core.config import settings

class AuthService:
    def __init__(self, db: AsyncSession):
        self.db = db
    
    async def register(
        self, 
        email: str, 
        username: str, 
        password: str,
        full_name: Optional[str] = None
    ) -> dict:
        """用户注册"""
        # 检查邮箱唯一性
        existing = await self.db.execute(
            select(User).where(User.email == email)
        )
        if existing.scalar_one_or_none():
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Email already registered"
            )
        
        # 加密密码
        password_hash = hashlib.md5(password.encode('utf-8')).hexdigest()
        
        # 创建用户
        user = User(
            email=email,
            username=username,
            password=password_hash,
            full_name=full_name,
            suspended=False
        )
        self.db.add(user)
        await self.db.commit()
        await self.db.refresh(user)
        
        # 生成 Token
        tokens = self._generate_tokens(user)
        
        # TODO: 发送验证邮件
        
        return {
            "user": user,
            "access_token": tokens["access_token"],
            "refresh_token": tokens["refresh_token"]
        }
    
    async def login(self, email: str, password: str) -> dict:
        """用户登录"""
        # 查找用户
        result = await self.db.execute(
            select(User).where(User.email == email)
        )
        user = result.scalar_one_or_none()
        
        if not user:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Invalid credentials"
            )
        
        # 检查状态
        if user.is_deleted:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Account has been deleted"
            )
        
        if user.suspended:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Account is suspended"
            )
        
        # 验证密码
        password_hash = hashlib.md5(password.encode('utf-8')).hexdigest()
        if password_hash != user.password:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Invalid credentials"
            )
        
        # 更新登录时间
        user.last_login_at = datetime.utcnow()
        await self.db.commit()
        
        # 生成 Token
        tokens = self._generate_tokens(user)
        
        return {
            "user": user,
            "access_token": tokens["access_token"],
            "refresh_token": tokens["refresh_token"]
        }
    
    def _generate_tokens(self, user: User, workspace_id: Optional[int] = None) -> dict:
        """生成 JWT Tokens"""
        # Access Token
        access_payload = {
            "sub": str(user.id),
            "email": user.email,
            "username": user.username,
            "type": "access",
            "exp": datetime.utcnow() + timedelta(minutes=settings.jwt_access_token_expire_minutes),
            "iat": datetime.utcnow()
        }
        if workspace_id:
            access_payload["workspace_id"] = workspace_id
        
        access_token = jwt.encode(
            access_payload,
            settings.jwt_secret_key,
            algorithm=settings.jwt_algorithm
        )
        
        # Refresh Token
        refresh_payload = {
            "sub": str(user.id),
            "type": "refresh",
            "exp": datetime.utcnow() + timedelta(days=settings.jwt_refresh_token_expire_days),
            "iat": datetime.utcnow()
        }
        
        refresh_token = jwt.encode(
            refresh_payload,
            settings.jwt_secret_key,
            algorithm=settings.jwt_algorithm
        )
        
        return {
            "access_token": access_token,
            "refresh_token": refresh_token
        }
    
    async def verify_token(self, token: str) -> User:
        """验证 Token"""
        try:
            payload = jwt.decode(
                token,
                settings.jwt_secret_key,
                algorithms=[settings.jwt_algorithm]
            )
            
            if payload.get("type") != "access":
                raise HTTPException(
                    status_code=status.HTTP_401_UNAUTHORIZED,
                    detail="Invalid token type"
                )
            
            user_id = int(payload.get("sub"))
            result = await self.db.execute(
                select(User).where(User.id == user_id)
            )
            user = result.scalar_one_or_none()
            
            if not user or user.is_deleted or user.suspended:
                raise HTTPException(
                    status_code=status.HTTP_401_UNAUTHORIZED,
                    detail="User not found or inactive"
                )
            
            return user
            
        except jwt.ExpiredSignatureError:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Token expired"
            )
        except jwt.InvalidTokenError:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Invalid token"
            )
```

### A.2 权限检查装饰器

```python
# api/deps.py
from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from sqlalchemy.ext.asyncio import AsyncSession

from database.engine import get_db
from services.auth_service import AuthService
from models.user import User

security = HTTPBearer()

async def get_current_user(
    credentials: HTTPAuthorizationCredentials = Depends(security),
    db: AsyncSession = Depends(get_db)
) -> User:
    """获取当前登录用户"""
    token = credentials.credentials
    auth_service = AuthService(db)
    return await auth_service.verify_token(token)

async def get_current_active_user(
    current_user: User = Depends(get_current_user)
) -> User:
    """获取当前活跃用户"""
    if current_user.is_deleted:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="User account has been deleted"
        )
    if current_user.suspended:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="User account is suspended"
        )
    return current_user
```

### A.3 工作区管理 API

```python
# api/workspaces.py
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from api.deps import get_current_active_user
from database.engine import get_db
from models.user import User
from schemas.workspace import WorkspaceCreate, WorkspaceResponse
from services.workspace_service import WorkspaceService

router = APIRouter(prefix="/workspaces", tags=["workspaces"])

@router.post("", response_model=WorkspaceResponse, status_code=201)
async def create_workspace(
    payload: WorkspaceCreate,
    current_user: User = Depends(get_current_active_user),
    db: AsyncSession = Depends(get_db)
):
    """创建工作区"""
    service = WorkspaceService(db)
    return await service.create(current_user, payload)

@router.get("", response_model=list[WorkspaceResponse])
async def list_workspaces(
    current_user: User = Depends(get_current_active_user),
    db: AsyncSession = Depends(get_db)
):
    """获取用户加入的工作区列表"""
    service = WorkspaceService(db)
    return await service.list_user_workspaces(current_user.id)

@router.get("/{workspace_id}", response_model=WorkspaceResponse)
async def get_workspace(
    workspace_id: int,
    current_user: User = Depends(get_current_active_user),
    db: AsyncSession = Depends(get_db)
):
    """获取工作区详情"""
    service = WorkspaceService(db)
    workspace = await service.get(workspace_id)
    
    # 检查用户是否是该工作区成员
    if not await service.is_member(workspace_id, current_user.id):
        raise HTTPException(status_code=403, detail="Not a member of this workspace")
    
    return workspace
```

---

## 结语

本设计方案提供了 AgentOS 企业级用户系统的完整蓝图，涵盖认证、授权、组织管理、计费、审计等核心模块。建议按照 Phase 1-6 的路线图渐进式实施，优先交付 MVP，再逐步增强企业特性。

在实施过程中，请注意：
1. **安全优先**：密码加密、Token 管理、API 限流一个都不能少
2. **性能优化**：合理使用索引、Redis 缓存、数据库连接池
3. **可测试性**：编写充分的单元测试和集成测试
4. **可观测性**：日志、指标、链路追踪要完整
5. **文档同步**：API 文档、开发文档、用户文档保持更新

