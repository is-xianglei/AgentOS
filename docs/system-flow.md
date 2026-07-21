# AgentOS 系统功能流程与数据关系

## 一、用户注册与登录流程

### 1.1 用户注册流程

```
用户注册
  ↓
POST /auth/register
  ↓
创建用户记录 (users 表)
  ↓
自动创建个人工作区 (workspaces 表)
  ├─ name: {username}_personal
  ├─ slug: {username}-personal
  ├─ display_name: {full_name}的个人空间
  └─ workspace_type: personal
  ↓
自动加入工作区成员 (workspace_members 表)
  ├─ workspace_id: 新创建的工作区 ID
  ├─ user_id: 用户 ID
  └─ joined_at: 当前时间
  ↓
生成包含 workspace_id 的 JWT Token
  ↓
返回: 用户信息 + workspace_id + access_token + refresh_token
```

### 1.2 用户登录流程

```
用户登录
  ↓
POST /auth/login
  ↓
验证邮箱和密码
  ↓
查询用户加入的工作区列表
  ↓
选择默认工作区（优先个人工作区）
  ↓
生成包含 workspace_id 的 JWT Token
  ↓
返回: 用户信息 + workspace_id + access_token + refresh_token
```

## 二、核心数据关系

### 2.1 数据库表关系

```
users (用户表)
  │
  ├──> workspace_members (成员关系表)
  │      └──> workspaces (工作区表)
  │
  └──> sessions (会话表)
         ├─ user_id (创建者)
         ├─ workspace_id (所属工作区)
         └──> session_messages (消息表)
```

### 2.2 详细字段关系

#### users 表
- id (主键)
- username (唯一)
- email (唯一)
- password (哈希)
- full_name
- avatar_url
- is_deleted
- suspended
- ...

#### workspaces 表
- id (主键)
- name (唯一) - 内部标识
- slug (唯一) - URL 友好标识
- display_name - 显示名称
- workspace_type - personal/team/enterprise
- logo_url
- is_deleted
- ...

#### workspace_members 表
- id (主键)
- workspace_id (外键 → workspaces.id)
- user_id (外键 → users.id)
- invited_by (外键 → users.id)
- invited_at - 邀请时间
- joined_at - 加入时间（NULL = 待接受）
- is_deleted
- ...

#### sessions 表
- id (主键)
- user_id (外键 → users.id) - 创建者
- workspace_id (外键 → workspaces.id) - 所属工作区
- title
- status
- visibility - private/team/workspace/public
- shared_with - 共享用户 ID 列表
- ...

## 三、JWT Token 结构

### 3.1 Access Token Payload

```json
{
  "sub": "用户ID",
  "email": "用户邮箱",
  "username": "用户名",
  "workspace_id": 当前工作区ID,
  "type": "access",
  "exp": 过期时间,
  "iat": 签发时间
}
```

### 3.2 Refresh Token Payload

```json
{
  "sub": "用户ID",
  "type": "refresh",
  "exp": 过期时间,
  "iat": 签发时间
}
```

## 四、完整业务流程

### 4.1 新用户首次使用

```
1. 注册账号
   POST /auth/register
   {
     "email": "user@example.com",
     "username": "testuser",
     "password": "password123",
     "full_name": "测试用户"
   }
   ↓
   返回: {
     "user": {...},
     "workspace_id": 1,  // 自动创建的个人工作区
     "access_token": "eyJ...",
     "refresh_token": "eyJ..."
   }

2. 创建会话并发送消息
   POST /sessions/messages
   Headers: Authorization: Bearer {access_token}
   {
     "session_id": null,  // 首次为 null
     "content": "你好"
   }
   ↓
   系统自动:
   - 从 token 提取 user_id 和 workspace_id
   - 创建新会话记录
   - 设置 session.user_id = token.user_id
   - 设置 session.workspace_id = token.workspace_id
   - 保存消息并执行 Agent

3. 查看会话列表
   GET /sessions
   Headers: Authorization: Bearer {access_token}
   ↓
   返回当前用户在当前工作区的所有会话
```

### 4.2 多工作区切换

```
1. 创建或加入其他工作区
   POST /workspaces
   或
   POST /workspaces/{id}/members/invite (被邀请)
   POST /workspaces/{id}/members/accept (接受邀请)

2. 切换工作区
   POST /auth/switch-workspace
   Headers: Authorization: Bearer {access_token}
   {
     "workspace_id": 2
   }
   ↓
   返回新的 token，包含新的 workspace_id

3. 在新工作区创建会话
   POST /sessions/messages
   Headers: Authorization: Bearer {新access_token}
   ↓
   会话数据隔离在新工作区下
```

### 4.3 工作区成员协作

```
1. 工作区管理员邀请成员
   POST /workspaces/{workspace_id}/members/invite
   {
     "email": "member@example.com"
   }
   ↓
   创建待接受的成员记录 (joined_at = NULL)

2. 被邀请者接受邀请
   POST /workspaces/{workspace_id}/members/accept
   ↓
   更新 joined_at = 当前时间

3. 成员切换到该工作区
   POST /auth/switch-workspace
   {
     "workspace_id": ...
   }

4. 成员查看工作区的会话（根据 visibility）
   - visibility = "private": 仅创建者可见
   - visibility = "workspace": 工作区成员可见
   - visibility = "team": 特定团队可见
   - visibility = "public": 所有人可见
```

## 五、API 依赖注入链路

### 5.1 Session 相关接口

```python
@router.post("/messages")
async def send_message(
    payload: SessionSendMessageRequest,
    db: AsyncSession = Depends(get_db),
    current_user: UserRecord = Depends(get_current_active_user),
    workspace_id: int | None = Depends(get_current_workspace_id),
):
    # current_user: 从 token 解析出的用户对象
    # workspace_id: 从 token 解析出的工作区 ID
    
    runtime = AgentRuntime(
        db, 
        user_id=current_user.id, 
        workspace_id=workspace_id
    )
    ...
```

### 5.2 依赖函数说明

- `get_db()`: 获取数据库会话
- `get_current_user()`: 验证 token 并返回用户对象
- `get_current_active_user()`: 验证用户状态（未删除、未禁用）
- `get_current_workspace_id()`: 从 token 提取 workspace_id

## 六、数据隔离与权限

### 6.1 数据隔离层级

```
系统级
  └─ 工作区级 (workspace_id)
       └─ 用户级 (user_id + visibility)
            └─ 会话级 (session_id)
```

### 6.2 会话访问权限规则

```python
def check_access(session: SessionRecord, user_id: int) -> bool:
    """
    访问规则：
    1. 会话的创建者
    2. 会话在 shared_with 列表中的用户
    3. 会话可见性为 workspace 且用户是该工作区成员
    4. 会话可见性为 public
    5. 兼容旧数据：user_id 为 None 的会话允许所有人访问
    """
```

## 七、实现要点总结

### 7.1 已实现

✅ 注册时自动创建个人工作区
✅ 注册时自动加入工作区成员表
✅ 登录时自动选择默认工作区
✅ Token 包含 workspace_id
✅ Session 创建时自动关联 user_id 和 workspace_id
✅ 从 Token 提取 workspace_id 的依赖函数
✅ 工作区切换接口
✅ 工作区成员邀请与管理

### 7.2 待完善

⚠️ 工作区成员权限验证（已预留接口，未实现）
⚠️ 会话 visibility=workspace 时的成员权限检查
⚠️ 邀请邮件发送功能
⚠️ 工作区配额与限制
⚠️ 审计日志

### 7.3 兼容性处理

- Token 中 workspace_id 可为 null（兼容旧数据）
- Session 中 user_id 为 null 时允许所有人访问（兼容旧数据）
- 登录时若用户没有工作区，生成不含 workspace_id 的 token
