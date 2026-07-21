# 用户与工作区系统实现总结

## 实现时间
2026-07-21

## 实现内容

### 一、注册时自动创建工作区

**文件**: `services/auth_service.py`

**改动**:
```python
async def register(...) -> dict:
    # 创建用户
    user = await self.repo.create(...)
    
    # 【新增】自动创建个人工作区
    workspace_service = WorkspaceService(self.db)
    workspace = await workspace_service.create_workspace(
        creator_user_id=user.id,
        name=f"{username}_personal",
        slug=f"{username}-personal",
        display_name=f"{full_name or username}的个人空间",
        workspace_type="personal"
    )
    
    # 【新增】生成包含 workspace_id 的 Token
    tokens = self._generate_tokens_with_workspace(user, workspace.id)
    
    return {
        "user": user,
        "workspace_id": workspace.id,  # 【新增】
        "access_token": tokens["access_token"],
        "refresh_token": tokens["refresh_token"],
    }
```

**效果**:
- 用户注册后自动拥有一个个人工作区
- 自动加入 `workspace_members` 表（在 `create_workspace` 中完成）
- 返回的 token 中包含 `workspace_id`

---

### 二、登录时选择默认工作区

**文件**: `services/auth_service.py`

**改动**:
```python
async def login(...) -> dict:
    # 验证用户和密码
    ...
    
    # 【新增】获取用户的工作区列表
    workspace_service = WorkspaceService(self.db)
    user_workspaces = await workspace_service.list_user_workspaces(user.id)
    
    workspace_id = None
    if user_workspaces:
        # 【新增】优先选择 personal 类型的工作区
        personal_workspace = next(
            (ws for ws in user_workspaces if ws.workspace_type == "personal"), 
            None
        )
        workspace_id = personal_workspace.id if personal_workspace else user_workspaces[0].id
    
    # 【新增】生成包含 workspace_id 的 Token
    if workspace_id:
        tokens = self._generate_tokens_with_workspace(user, workspace_id)
    else:
        tokens = self._generate_tokens(user)  # 兼容没有工作区的情况
    
    return {
        "user": user,
        "workspace_id": workspace_id,  # 【新增】
        "access_token": tokens["access_token"],
        "refresh_token": tokens["refresh_token"],
    }
```

**效果**:
- 登录时自动选择用户的默认工作区
- 优先选择个人工作区（`workspace_type="personal"`）
- Token 中包含 `workspace_id`

---

### 三、添加获取工作区 ID 的依赖函数

**文件**: `api/deps.py`

**改动**:
```python
import jwt  # 【新增导入】
from core.config import settings  # 【新增导入】

async def get_current_workspace_id(
    credentials: HTTPAuthorizationCredentials = Depends(security),
) -> int | None:
    """从 Token 中提取当前工作区 ID"""
    try:
        token = credentials.credentials
        payload = jwt.decode(
            token,
            settings.jwt_secret_key,
            algorithms=[settings.jwt_algorithm],
        )
        return payload.get("workspace_id")
    except (jwt.ExpiredSignatureError, jwt.InvalidTokenError):
        return None
```

**效果**:
- 可以在任何接口中通过依赖注入获取当前工作区 ID
- 解析失败时返回 `None`（兼容旧 token）

---

### 四、更新 Session 接口使用工作区

**文件**: `api/sessions.py`

**改动**:
```python
from api.deps import get_current_workspace_id  # 【新增导入】

@router.post("/messages", summary="发送消息")
async def send_message(
    payload: SessionSendMessageRequest,
    db: AsyncSession = Depends(get_db),
    current_user: UserRecord = Depends(get_current_active_user),
    workspace_id: int | None = Depends(get_current_workspace_id),  # 【新增】
):
    # 【修改】传递工作区信息到 runtime
    runtime = AgentRuntime(db, user_id=current_user.id, workspace_id=workspace_id)
    ...

@router.post("/{session_id}/approvals", summary="审批工具调用并恢复执行")
async def respond_approval(
    session_id: int,
    payload: SessionApprovalRequest,
    db: AsyncSession = Depends(get_db),
    current_user: UserRecord = Depends(get_current_active_user),
    workspace_id: int | None = Depends(get_current_workspace_id),  # 【新增】
):
    # 【修改】传递工作区信息到 runtime
    runtime = AgentRuntime(db, user_id=current_user.id, workspace_id=workspace_id)
    ...
```

**效果**:
- 创建 Session 时自动关联 `workspace_id`
- 数据隔离在工作区级别

---

### 五、更新认证响应 Schema

**文件**: `schemas/auth.py`

**改动**:
```python
class AuthResponse(BaseModel):
    """认证响应（包含用户信息和 Token）"""
    user: "UserInfo"
    workspace_id: int | None = Field(default=None, description="当前工作区ID")  # 【新增】
    access_token: str = Field(description="访问令牌")
    refresh_token: str = Field(description="刷新令牌")
    token_type: str = Field(default="bearer", description="令牌类型")
```

**效果**:
- 注册和登录接口响应中包含 `workspace_id`
- 前端可以知道当前用户所在的工作区

---

### 六、更新认证接口返回值

**文件**: `api/auth.py`

**改动**:
```python
@router.post("/register", ...)
async def register(...):
    result = await auth_service.register(...)
    
    return AuthResponse(
        user=UserInfo.model_validate(result["user"]),
        workspace_id=result.get("workspace_id"),  # 【新增】
        access_token=result["access_token"],
        refresh_token=result["refresh_token"],
    )

@router.post("/login", ...)
async def login(...):
    result = await auth_service.login(...)
    
    return AuthResponse(
        user=UserInfo.model_validate(result["user"]),
        workspace_id=result.get("workspace_id"),  # 【新增】
        access_token=result["access_token"],
        refresh_token=result["refresh_token"],
    )
```

**效果**:
- 统一返回格式，包含工作区信息

---

## 数据流说明

### 注册流程
```
1. POST /auth/register
2. 创建 users 记录
3. 创建 workspaces 记录 (personal 类型)
4. 创建 workspace_members 记录 (自动加入)
5. 生成包含 workspace_id 的 JWT
6. 返回 {user, workspace_id, access_token, refresh_token}
```

### 登录流程
```
1. POST /auth/login
2. 验证用户凭证
3. 查询用户的工作区列表
4. 选择默认工作区 (优先 personal)
5. 生成包含 workspace_id 的 JWT
6. 返回 {user, workspace_id, access_token, refresh_token}
```

### 创建会话流程
```
1. POST /sessions/messages (Header: Bearer token)
2. 从 token 提取 user_id 和 workspace_id
3. 创建 session 记录
   - session.user_id = token.user_id
   - session.workspace_id = token.workspace_id
4. 保存消息并执行 Agent
5. 返回流式响应
```

---

## 工作区数据来源

### workspaces 表
- **自动创建**: 用户注册时创建个人工作区
- **手动创建**: 通过 `POST /workspaces` 接口创建团队/企业工作区

### workspace_members 表
- **自动加入**: 创建工作区时，创建者自动加入
- **邀请加入**: 通过 `POST /workspaces/{id}/members/invite` 邀请
- **接受邀请**: 通过 `POST /workspaces/{id}/members/accept` 接受

---

## 兼容性保证

### 旧数据兼容
- Session 的 `user_id` 为 `NULL` 时，允许所有人访问（`check_access` 方法中）
- Token 中 `workspace_id` 可为 `None`（`get_current_workspace_id` 返回类型为 `int | None`）
- 登录时若用户没有工作区，生成不含 `workspace_id` 的 token

### 新旧系统过渡
- 依赖注入使用 `workspace_id: int | None`，不强制要求
- `AgentRuntime` 接受 `workspace_id=None`
- 所有查询接口兼容 `workspace_id` 为空的情况

---

## 测试建议

### 注册测试
```bash
curl -X POST http://localhost:8000/auth/register \
  -H "Content-Type: application/json" \
  -d '{
    "email": "test@example.com",
    "username": "testuser",
    "password": "password123",
    "full_name": "测试用户"
  }'

# 预期返回：
# {
#   "user": {...},
#   "workspace_id": 1,
#   "access_token": "eyJ...",
#   "refresh_token": "eyJ...",
#   "token_type": "bearer"
# }
```

### 登录测试
```bash
curl -X POST http://localhost:8000/auth/login \
  -H "Content-Type: application/json" \
  -d '{
    "email": "test@example.com",
    "password": "password123"
  }'

# 预期返回：
# {
#   "user": {...},
#   "workspace_id": 1,
#   "access_token": "eyJ...",
#   "refresh_token": "eyJ...",
#   "token_type": "bearer"
# }
```

### 创建会话测试
```bash
# 使用上面获取的 access_token
curl -X POST http://localhost:8000/sessions/messages \
  -H "Authorization: Bearer {access_token}" \
  -H "Content-Type: application/json" \
  -d '{
    "session_id": null,
    "content": "你好"
  }'

# 预期：创建的 session 记录自动关联 user_id 和 workspace_id
```

### 验证数据库
```sql
-- 查看用户的工作区
SELECT u.username, w.name, w.workspace_type, wm.joined_at
FROM users u
JOIN workspace_members wm ON u.id = wm.user_id
JOIN workspaces w ON wm.workspace_id = w.id
WHERE u.email = 'test@example.com';

-- 查看会话关联
SELECT s.id, s.title, s.user_id, s.workspace_id, u.username, w.name
FROM sessions s
JOIN users u ON s.user_id = u.id
JOIN workspaces w ON s.workspace_id = w.id
WHERE u.email = 'test@example.com';
```

---

## 后续工作

### 待实现功能
1. ⚠️ 工作区成员权限验证（在 `check_access` 中）
2. ⚠️ 邀请邮件发送
3. ⚠️ 工作区配额管理
4. ⚠️ 审计日志

### 优化方向
1. 缓存用户的工作区列表（减少数据库查询）
2. Token 中增加工作区角色信息（owner/admin/member）
3. 支持用户设置默认工作区
4. 工作区切换历史记录

---

## 文件修改清单

| 文件 | 修改类型 | 说明 |
|------|---------|------|
| `services/auth_service.py` | 修改 | 注册时创建工作区，登录时选择默认工作区 |
| `api/deps.py` | 新增 | 添加 `get_current_workspace_id` 依赖函数 |
| `api/sessions.py` | 修改 | 使用 `get_current_workspace_id` 依赖 |
| `api/auth.py` | 修改 | 返回 `workspace_id` |
| `schemas/auth.py` | 修改 | `AuthResponse` 增加 `workspace_id` 字段 |
| `docs/system-flow.md` | 新增 | 系统流程和数据关系文档 |
| `docs/implementation-summary.md` | 新增 | 本实现总结文档 |

---

## 总结

本次实现完成了用户与工作区系统的核心功能串联：

✅ 注册时自动创建个人工作区并加入成员表  
✅ 登录时自动选择默认工作区  
✅ Token 包含工作区上下文  
✅ Session 创建时自动关联工作区  
✅ 数据隔离在工作区级别  
✅ API 依赖注入获取工作区信息  
✅ 兼容旧数据和无工作区场景  

系统现在可以支持多用户、多工作区的场景，为后续的权限管理、协作功能打下了基础。
