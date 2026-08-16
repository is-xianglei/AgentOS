# Error Handling

> AgentOS 的错误处理规范

---

## Overview

AgentOS 使用统一的业务异常类 `AgentException` 和标准化的 API 响应外壳 `ApiResponse[T]`，确保所有错误都以一致的格式返回给客户端。业务逻辑层通过抛出异常传播错误，由全局异常处理器统一捕获并转换为标准响应。

核心原则：
- 业务错误统一使用 `AgentException.message("中文消息", details)`
- 不使用裸 `HTTPException` 或临时错误响应结构
- 包装底层异常时使用 `raise ... from exc` 保留异常链
- 普通 JSON API 使用 `ApiResponse[T]` 和 `ok(data, request)` 维持 `{data, error, request_id}` 响应外壳
- SSE `StreamingResponse` 属于例外，不使用 `ApiResponse[T]`

---

## Error Types

### AgentException

业务异常的唯一入口，定义在 `core/errors.py`：

```python
class AgentException(Exception):
    """业务异常，携带可读消息、可选详情和 HTTP 状态码。"""

    status_code = 500

    def __init__(
        self,
        message: str,
        details: dict[str, Any] | None = None,
        status_code: int | None = None,
    ):
        self.message = message
        self.details = details or {}
        self.status_code = status_code or type(self).status_code
        super().__init__(message)

    @classmethod
    def message(
        cls,
        message: str,
        details: dict[str, Any] | None = None,
        *,
        status_code: int | None = None,
    ) -> AgentException:
        """业务异常唯一入口。"""
        return cls(message, details, status_code)
```

**使用场景：**
- 资源不存在（404）
- 参数校验失败（422）
- 权限不足（403）
- 业务规则冲突（400）
- 内部处理失败（500）

**关键字段：**
- `message`：简体中文错误消息，面向最终用户
- `details`：结构化错误详情，包含上下文信息（如 `tool_ids`、`workspace_id`）
- `status_code`：HTTP 状态码，默认 500

---

## Error Handling Patterns

### Service 层抛出业务异常

Service 层是业务校验的核心位置，所有业务错误都应在此抛出。

**示例 1：资源不存在（`tools/service.py`）**

```python
async def get_required(self, tool_id: int) -> ToolRecord:
    record = await self.repo.get_by_id(tool_id)
    if record is None:
        raise AgentException.message("工具不存在", status_code=404)
    return record
```

**示例 2：批量资源校验失败（`tools/service.py`）**

```python
async def require_bindable_tool_ids(self, tool_ids: list[int]) -> list[int]:
    normalized = list(dict.fromkeys(tool_ids))
    records = await self.repo.list_by_ids(normalized)
    records_by_id = {record.id: record for record in records}
    missing = [tool_id for tool_id in normalized if tool_id not in records_by_id]
    if missing:
        raise AgentException.message(
            "绑定的工具不存在",
            {"tool_ids": missing},
            status_code=404,
        )
    # 继续处理...
```

### 包装底层异常

捕获数据库、网络或第三方 SDK 异常时，使用 `raise ... from exc` 保留异常链：

```python
try:
    result = await some_external_service.call()
except ExternalServiceError as exc:
    raise AgentException.message(
        "调用外部服务失败",
        {"service": "example"},
        status_code=502,
    ) from exc
```

### API 层轻薄处理

API 层不执行业务校验，仅负责依赖注入、Schema 转换和响应包装。异常由全局处理器捕获。

**示例：典型 API 端点（`tools/api.py`）**

```python
@router.get("/{tool_id}", summary="查询内置工具详情", response_model=ApiResponse[ToolDetailResponse])
async def get_tool(
    tool_id: int,
    request: Request,
    db: DatabaseSession,
    current_user: CurrentUser,
    workspace_id: CurrentWorkspaceId,
):
    record = await ToolCatalogService(db).get_required(tool_id)
    return ok(ToolDetailResponse.model_validate(record), request)
```

**关键点：**
- 调用 Service 方法，不捕获 `AgentException`
- 使用 `ok(data, request)` 构造成功响应
- 失败时由全局异常处理器统一处理

---

## API Error Responses

### 标准响应外壳

所有 JSON API 响应遵循统一结构，定义在 `core/responses.py`：

```python
class ApiResponse(BaseModel, Generic[T]):
    data: T | None = Field(default=None, description="响应数据")
    error: ErrorBody | None = Field(default=None, description="错误信息")
    request_id: str | None = Field(default=None, description="请求ID")


def ok(data: Any, request: Request) -> dict[str, Any]:
    return {
        "data": data,
        "error": None,
        "request_id": getattr(request.state, "request_id", None),
    }
```

### 成功响应示例

```json
{
  "data": {
    "id": 1,
    "name": "example_tool",
    "description": "示例工具"
  },
  "error": null,
  "request_id": "req_abc123"
}
```

### 错误响应示例

当 Service 层抛出 `AgentException` 时，全局处理器自动转换为标准格式：

```json
{
  "data": null,
  "error": {
    "message": "工具不存在",
    "details": {}
  },
  "request_id": "req_abc123"
}
```

带详情的错误响应：

```json
{
  "data": null,
  "error": {
    "message": "绑定的工具不存在",
    "details": {
      "tool_ids": [5, 8, 12]
    }
  },
  "request_id": "req_def456"
}
```

### 全局异常处理器

在 `main.py` 中注册了三个全局异常处理器：

```python
app.add_exception_handler(AgentException, agent_error_handler)
app.add_exception_handler(RequestValidationError, validation_error_handler)
app.add_exception_handler(Exception, unhandled_error_handler)
```

**处理器定义（`core/errors.py`）：**

1. **AgentException 处理器**：将业务异常转换为标准响应
2. **RequestValidationError 处理器**：处理 Pydantic 参数校验失败
3. **Exception 处理器**：捕获未预期的内部错误，返回通用"内部错误"消息

### 流式响应的例外

SSE `StreamingResponse` 不使用 `ApiResponse[T]` 外壳，因为流式数据结构由事件协议决定。流式错误通过事件流中的错误事件传递。

---

## Common Mistakes

### ❌ 错误：使用裸 HTTPException

```python
# 不要这样做
from fastapi import HTTPException

if not user:
    raise HTTPException(status_code=404, detail="User not found")
```

**问题：** 破坏了统一响应外壳，`detail` 直接暴露为字符串，无法携带结构化 `details`。

### ✅ 正确：使用 AgentException.message

```python
# 应该这样做
from core.errors import AgentException

if not user:
    raise AgentException.message("用户不存在", status_code=404)
```

---

### ❌ 错误：API 层执行业务校验

```python
# 不要这样做
@router.post("/tools/{tool_id}/bind")
async def bind_tool(tool_id: int, db: DatabaseSession):
    tool = await db.get(ToolRecord, tool_id)
    if not tool:
        raise AgentException.message("工具不存在", status_code=404)
    # 业务逻辑...
```

**问题：** API 层不应直接访问 Repository 或执行业务校验。

### ✅ 正确：Service 层负责业务逻辑

```python
# API 层
@router.post("/tools/{tool_id}/bind")
async def bind_tool(tool_id: int, db: DatabaseSession):
    result = await ToolService(db).bind_tool(tool_id)
    return ok(result, request)

# Service 层
async def bind_tool(self, tool_id: int):
    tool = await self.repo.get_by_id(tool_id)
    if not tool:
        raise AgentException.message("工具不存在", status_code=404)
    # 业务逻辑...
```

---

### ❌ 错误：吞掉底层异常链

```python
# 不要这样做
try:
    await anthropic_client.messages.create(...)
except Exception:
    raise AgentException.message("调用 AI 服务失败")
```

**问题：** 丢失了原始异常堆栈，无法追溯根因。

### ✅ 正确：使用 raise ... from exc

```python
# 应该这样做
try:
    await anthropic_client.messages.create(...)
except Exception as exc:
    raise AgentException.message("调用 AI 服务失败") from exc
```

---

### ❌ 错误：API 层捕获并处理业务异常

```python
# 不要这样做
@router.get("/tools/{tool_id}")
async def get_tool(tool_id: int, db: DatabaseSession):
    try:
        tool = await ToolService(db).get_required(tool_id)
        return ok(tool, request)
    except AgentException as e:
        return JSONResponse(status_code=e.status_code, content={"error": e.message})
```

**问题：** 破坏了统一错误处理流程，响应格式不一致。

### ✅ 正确：让全局处理器捕获

```python
# 应该这样做
@router.get("/tools/{tool_id}")
async def get_tool(tool_id: int, db: DatabaseSession):
    tool = await ToolService(db).get_required(tool_id)
    return ok(tool, request)
```

---

### ❌ 错误：错误消息使用英文或暴露技术细节

```python
# 不要这样做
raise AgentException.message("SQLAlchemy integrity error: duplicate key")
```

**问题：** 错误消息应面向最终用户，使用简体中文，不暴露内部实现细节。

### ✅ 正确：用户友好的中文消息

```python
# 应该这样做
raise AgentException.message("该工具已存在，无法重复添加")
```

---

## 参考文件

- **异常定义与处理器：** `core/errors.py`
- **响应外壳定义：** `core/responses.py`
- **全局异常注册：** `main.py`
- **Service 层示例：** `tools/service.py`, `memory/service.py`
- **API 层示例：** `tools/api.py`, `memory/api.py`

