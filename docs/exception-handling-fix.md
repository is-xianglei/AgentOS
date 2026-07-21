# 异常处理规范统一修复报告

## 问题概述

新增的用户认证相关模块没有遵循项目的异常处理规范，直接使用了 FastAPI 的 `HTTPException`，而项目其他模块统一使用自定义的 `AgentException.message()`。

## 项目异常处理规范

### 正确做法
- **异常类**: `core.errors.AgentException`
- **抛出方式**: `raise AgentException.message("错误消息")`
- **状态码**: 统一为 500
- **响应格式**: 
  ```json
  {
    "data": null,
    "error": {
      "message": "错误消息",
      "details": {}
    },
    "request_id": "..."
  }
  ```

### 错误做法 (已修复)
- ❌ 使用 `HTTPException(status_code=xxx, detail="...")`
- ❌ 使用多种 HTTP 状态码 (400, 401, 403)
- ❌ 返回非统一格式的错误响应

## 修改的文件

### 1. `services/auth_service.py`
**修改内容**:
- 移除 `from fastapi import HTTPException, status`
- 添加 `from core.errors import AgentException`
- 替换所有 `HTTPException` 为 `AgentException.message()`

**修改示例**:
```python
# 修改前
raise HTTPException(
    status_code=status.HTTP_400_BAD_REQUEST,
    detail="Email already registered",
)

# 修改后
raise AgentException.message("邮箱已被注册")
```

**涉及方法**:
- `register()` - 注册时的唯一性校验
- `login()` - 登录时的凭证和状态校验
- `verify_token()` - Token 验证
- `refresh_token()` - 刷新 Token
- `switch_workspace()` - 切换工作区

### 2. `api/auth.py`
**修改内容**:
- 移除 `HTTPException` 导入
- 添加 `from core.errors import AgentException`
- 修改 `switch_workspace()` 端点中的权限检查

**修改示例**:
```python
# 修改前
raise HTTPException(
    status_code=status.HTTP_403_FORBIDDEN,
    detail="Not a member of this workspace",
)

# 修改后
raise AgentException.message("不是该工作区的成员")
```

### 3. `api/deps.py`
**修改内容**:
- 移除 `HTTPException, status` 导入
- 添加 `from core.errors import AgentException`
- 修改 `get_current_active_user()` 依赖中的状态检查

**修改示例**:
```python
# 修改前
raise HTTPException(
    status_code=status.HTTP_403_FORBIDDEN,
    detail="User account has been deleted",
)

# 修改后
raise AgentException.message("用户账号已被删除")
```

### 4. `services/user_service.py`
**状态**: ✅ 已符合规范，无需修改
- 已正确使用 `AgentException.message()`

### 5. `api/users.py`
**状态**: ✅ 已符合规范，无需修改
- 未直接抛出异常，通过 service 层抛出

### 6. `services/workspace_service.py`
**状态**: ✅ 已符合规范，无需修改
- 已正确使用 `AgentException.message()`

### 7. `api/workspaces.py`
**状态**: ✅ 已符合规范，无需修改
- 未直接抛出异常，通过 service 层抛出

## 验证结果

### 语法检查
```bash
python -m py_compile services/auth_service.py services/user_service.py api/auth.py api/deps.py
```
✅ 通过

### 异常使用检查
```bash
grep -rn "raise HTTPException" services/ api/
```
✅ 仅剩注释中的引用，无实际使用

### 导入检查
```bash
grep "from core.errors import AgentException" services/auth_service.py api/auth.py api/deps.py
```
✅ 所有文件已正确导入

## 修改的异常消息对照表

| 原英文消息 | 新中文消息 | 场景 |
|-----------|----------|------|
| Email already registered | 邮箱已被注册 | 注册 |
| Username already taken | 用户名已被占用 | 注册 |
| Invalid credentials | 用户名或密码错误 | 登录 |
| Account has been deleted | 账号已被删除 | 登录/验证 |
| Account is suspended | 账号已被禁用 | 登录/验证 |
| Invalid token type | 无效的 Token 类型 | Token 验证 |
| User not found or inactive | 用户不存在或已被禁用 | Token 验证 |
| Token expired | Token 已过期 | Token 验证 |
| Invalid token | 无效的 Token | Token 验证 |
| Refresh token expired | Refresh Token 已过期 | 刷新 Token |
| Invalid refresh token | 无效的 Refresh Token | 刷新 Token |
| Not a member of this workspace | 不是该工作区的成员 | 切换工作区 |
| User account has been deleted | 用户账号已被删除 | 依赖注入检查 |
| User account is suspended | 用户账号已被禁用 | 依赖注入检查 |

## 影响范围

### 客户端影响
- ⚠️ **API 响应格式变化**: 错误响应从 FastAPI 默认格式变为项目统一格式
- ⚠️ **HTTP 状态码变化**: 所有业务异常统一返回 500（之前是 400/401/403）
- ⚠️ **错误消息字段变化**: `detail` → `error.message`

### 建议
1. 前端需要适配新的错误响应格式
2. 错误处理逻辑需要检查 `error.message` 字段
3. 不再依赖 HTTP 状态码区分业务错误类型

## 现有规范示例

参考项目中其他模块的正确实现：

### 1. Session Service
```python
# services/session_service.py
async def get_required(self, session_id: int) -> SessionRecord:
    session = await self.repo.get(session_id)
    if session is None:
        raise AgentException.message("会话不存在")
    return session
```

### 2. Session API
```python
# api/sessions.py
if not await service.check_access(session, current_user.id):
    raise AgentException.message("无权限访问该会话")
```

### 3. Workspace Service
```python
# services/workspace_service.py
async def get_workspace(self, workspace_id: int) -> WorkspaceRecord:
    workspace = await self.repo.get_by_id(workspace_id)
    if not workspace or workspace.is_deleted:
        raise AgentException.message("工作区不存在")
    return workspace
```

## 总结

- ✅ 所有新增文件的异常处理已统一为项目规范
- ✅ 移除了所有不符合规范的 `HTTPException` 使用
- ✅ 错误消息已本地化为中文
- ✅ 代码通过语法检查
- ⚠️ 需要注意前端适配新的错误响应格式
