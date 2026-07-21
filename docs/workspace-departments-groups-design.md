# Workspace 中的部门与群组设计方案

## 一、概念定义

### 1.1 Workspace（工作区）
- **定义**：租户隔离的最高层级单位
- **类型**：personal（个人）/ team（团队）/ enterprise（企业）
- **作用**：数据隔离、计费单元、权限边界

### 1.2 Department（部门）
- **定义**：企业正式的组织架构单位
- **特点**：
  - 层级化结构（支持多级部门）
  - 相对稳定，变动较少
  - 一个成员只能属于一个部门
  - 通常对应 HR 组织架构
  - 用于权限继承、报表统计

### 1.3 Group（群组/团队）
- **定义**：灵活的协作单位
- **特点**：
  - 扁平结构（不支持层级）
  - 动态灵活，可随时创建/解散
  - 一个成员可以加入多个群组
  - 跨部门协作（项目组、兴趣小组）
  - 用于资源共享、消息通知

## 二、层级关系

```
Workspace (工作区)
├── WorkspaceMembers (成员)
│   ├── User A (department_id=1, groups=[1,2])
│   ├── User B (department_id=1, groups=[2,3])
│   └── User C (department_id=2, groups=[1])
│
├── Departments (部门 - 树形结构)
│   ├── Department 1: Engineering (parent_id=NULL)
│   │   ├── Department 3: Frontend Team (parent_id=1)
│   │   └── Department 4: Backend Team (parent_id=1)
│   └── Department 2: Sales (parent_id=NULL)
│
└── Groups (群组 - 扁平结构)
    ├── Group 1: Project Alpha (跨部门项目)
    ├── Group 2: Python Community (技术兴趣组)
    └── Group 3: Sales APAC (区域团队)
```

## 三、数据模型设计

### 3.1 部门表 (departments)

```python
class Department(Base):
    __tablename__ = "departments"
    __table_args__ = (
        Index("ix_departments_workspace_parent", "workspace_id", "parent_id"),
        {"comment": "部门表（支持层级结构）"}
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    workspace_id: Mapped[int] = mapped_column(
        ForeignKey("workspaces.id", ondelete="CASCADE"), 
        index=True,
        comment="所属工作区ID"
    )
    
    # 层级结构
    parent_id: Mapped[int | None] = mapped_column(
        ForeignKey("departments.id", ondelete="SET NULL"), 
        nullable=True,
        index=True,
        comment="父部门ID（NULL表示顶级部门）"
    )
    
    # 基本信息
    name: Mapped[str] = mapped_column(String(128), comment="部门名称")
    code: Mapped[str | None] = mapped_column(
        String(32), 
        nullable=True, 
        comment="部门编码（可选，用于对接HR系统）"
    )
    description: Mapped[str | None] = mapped_column(Text, nullable=True, comment="部门描述")
    
    # 负责人
    manager_id: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), 
        nullable=True,
        comment="部门负责人用户ID"
    )
    
    # 排序与显示
    sort_order: Mapped[int] = mapped_column(Integer, default=0, comment="排序顺序")
    
    # 状态
    is_active: Mapped[bool] = mapped_column(default=True, comment="是否启用")
    
    # 时间戳（继承自 Base: created_at, updated_at, is_deleted, deleted_at）
    
    # 关联关系
    workspace: Mapped["Workspace"] = relationship(back_populates="departments")
    parent: Mapped["Department"] = relationship(
        remote_side=[id], 
        back_populates="children"
    )
    children: Mapped[list["Department"]] = relationship(
        back_populates="parent",
        cascade="all, delete-orphan"
    )
    members: Mapped[list["WorkspaceMember"]] = relationship(back_populates="department")
    manager: Mapped["User"] = relationship(foreign_keys=[manager_id])
```

**索引策略**：
- `workspace_id` 单列索引（查询工作区的所有部门）
- `parent_id` 单列索引（查询子部门）
- `(workspace_id, parent_id)` 复合索引（查询工作区的顶级部门）
- `is_deleted` 继承自 Base

### 3.2 群组表 (groups)

```python
class Group(Base):
    __tablename__ = "groups"
    __table_args__ = (
        Index("ix_groups_workspace_type", "workspace_id", "group_type"),
        {"comment": "群组表（扁平结构，用于灵活协作）"}
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    workspace_id: Mapped[int] = mapped_column(
        ForeignKey("workspaces.id", ondelete="CASCADE"), 
        index=True,
        comment="所属工作区ID"
    )
    
    # 基本信息
    name: Mapped[str] = mapped_column(String(128), comment="群组名称")
    slug: Mapped[str] = mapped_column(String(64), comment="群组标识（URL友好）")
    description: Mapped[str | None] = mapped_column(Text, nullable=True, comment="群组描述")
    avatar_url: Mapped[str | None] = mapped_column(String(512), nullable=True, comment="群组头像")
    
    # 类型
    group_type: Mapped[str] = mapped_column(
        String(32), 
        default="team",
        comment="群组类型: team(项目团队)/community(兴趣小组)/region(区域团队)/custom(自定义)"
    )
    
    # 创建者与负责人
    created_by: Mapped[int] = mapped_column(
        ForeignKey("users.id"), 
        comment="创建者用户ID"
    )
    owner_id: Mapped[int] = mapped_column(
        ForeignKey("users.id"), 
        comment="负责人用户ID"
    )
    
    # 可见性
    visibility: Mapped[str] = mapped_column(
        String(32), 
        default="workspace",
        comment="可见性: workspace(工作区可见)/private(仅成员可见)/public(公开)"
    )
    
    # 加入方式
    join_mode: Mapped[str] = mapped_column(
        String(32), 
        default="invite",
        comment="加入方式: invite(邀请制)/open(开放加入)/approval(申请审批)"
    )
    
    # 设置
    settings: Mapped[dict] = mapped_column(json_type(), default=dict, comment="群组设置")
    
    # 状态
    is_active: Mapped[bool] = mapped_column(default=True, comment="是否启用")
    
    # 时间戳（继承自 Base: created_at, updated_at, is_deleted, deleted_at）
    
    # 关联关系
    workspace: Mapped["Workspace"] = relationship(back_populates="groups")
    members: Mapped[list["GroupMember"]] = relationship(
        back_populates="group",
        cascade="all, delete-orphan"
    )
    creator: Mapped["User"] = relationship(foreign_keys=[created_by])
    owner: Mapped["User"] = relationship(foreign_keys=[owner_id])
```

**索引策略**：
- `workspace_id` 单列索引
- `(workspace_id, group_type)` 复合索引（按类型筛选群组）
- `visibility` 单列索引（查询公开群组）

### 3.3 群组成员表 (group_members)

```python
class GroupMember(Base):
    __tablename__ = "group_members"
    __table_args__ = (
        Index("ix_group_members_group_user", "group_id", "user_id", unique=True),
        Index("ix_group_members_user", "user_id"),
        {"comment": "群组成员关系表"}
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    group_id: Mapped[int] = mapped_column(
        ForeignKey("groups.id", ondelete="CASCADE"), 
        index=True
    )
    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), 
        index=True
    )
    
    # 角色（群组内角色，与工作区角色独立）
    role: Mapped[str] = mapped_column(
        String(32), 
        default="member",
        comment="角色: owner(负责人)/admin(管理员)/member(成员)"
    )
    
    # 加入信息
    invited_by: Mapped[int | None] = mapped_column(
        ForeignKey("users.id"), 
        nullable=True,
        comment="邀请人用户ID"
    )
    joined_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), 
        server_default=func.now(),
        comment="加入时间"
    )
    
    # 时间戳（继承自 Base: created_at, updated_at, is_deleted, deleted_at）
    
    # 关联关系
    group: Mapped["Group"] = relationship(back_populates="members")
    user: Mapped["User"] = relationship()
    inviter: Mapped["User"] = relationship(foreign_keys=[invited_by])
```

### 3.4 扩展 WorkspaceMember 表

```python
class WorkspaceMember(Base):
    __tablename__ = "workspace_members"
    # ... 现有字段保持不变 ...
    
    # 新增部门关联
    department_id: Mapped[int | None] = mapped_column(
        ForeignKey("departments.id", ondelete="SET NULL"), 
        nullable=True,
        index=True,
        comment="所属部门ID（一个成员只能属于一个部门）"
    )
    
    # 职位信息
    job_title: Mapped[str | None] = mapped_column(
        String(128), 
        nullable=True,
        comment="职位/岗位"
    )
    
    # 关联关系
    department: Mapped["Department"] = relationship(back_populates="members")
```

### 3.5 扩展 SessionRecord 表（可选）

```python
class SessionRecord(Base):
    # ... 现有字段 ...
    
    # 新增共享范围
    shared_with_departments: Mapped[list[int]] = mapped_column(
        json_type(), 
        default=list,
        comment="共享给的部门ID列表"
    )
    shared_with_groups: Mapped[list[int]] = mapped_column(
        json_type(), 
        default=list,
        comment="共享给的群组ID列表"
    )
```

## 四、典型使用场景

### 4.1 场景一：按部门组织的企业

```
Workspace: Acme Corp (企业工作区)

Departments (部门):
├── 1. Engineering (工程部)
│   ├── 1.1 Frontend Team (前端团队)
│   └── 1.2 Backend Team (后端团队)
├── 2. Product (产品部)
└── 3. Sales (销售部)

Groups (群组):
├── Project Alpha (跨部门项目组：工程 + 产品)
├── Python Community (技术兴趣组：工程部内)
└── Sales APAC (区域团队：销售部内)

Members:
├── Alice (department=Engineering/Frontend, groups=[Project Alpha, Python Community])
├── Bob (department=Engineering/Backend, groups=[Project Alpha, Python Community])
├── Carol (department=Product, groups=[Project Alpha])
└── David (department=Sales, groups=[Sales APAC])
```

### 4.2 场景二：扁平化的创业公司

```
Workspace: Startup Inc (团队工作区)

Departments: 无（或只有一个默认部门）

Groups (群组):
├── Core Team (核心团队)
├── Marketing (市场组)
├── Development (开发组)
└── Friday Social (周五社交活动组)

Members:
├── Alice (groups=[Core Team, Development])
├── Bob (groups=[Core Team, Development, Friday Social])
└── Carol (groups=[Core Team, Marketing, Friday Social])
```

## 五、API 设计

### 5.1 部门相关 API

```python
# 部门管理
POST   /api/workspaces/{workspace_id}/departments              创建部门
GET    /api/workspaces/{workspace_id}/departments              部门列表（树形）
GET    /api/workspaces/{workspace_id}/departments/{id}         部门详情
PATCH  /api/workspaces/{workspace_id}/departments/{id}         更新部门
DELETE /api/workspaces/{workspace_id}/departments/{id}         删除部门

# 部门成员
GET    /api/workspaces/{workspace_id}/departments/{id}/members 部门成员列表
POST   /api/workspaces/{workspace_id}/members/{user_id}/department  设置成员部门

# 部门树
GET    /api/workspaces/{workspace_id}/departments/tree         获取部门树
GET    /api/workspaces/{workspace_id}/departments/{id}/ancestors  获取祖先部门
GET    /api/workspaces/{workspace_id}/departments/{id}/descendants 获取子孙部门
```

### 5.2 群组相关 API

```python
# 群组管理
POST   /api/workspaces/{workspace_id}/groups                   创建群组
GET    /api/workspaces/{workspace_id}/groups                   群组列表
GET    /api/workspaces/{workspace_id}/groups/{id}              群组详情
PATCH  /api/workspaces/{workspace_id}/groups/{id}              更新群组
DELETE /api/workspaces/{workspace_id}/groups/{id}              删除群组

# 群组成员
GET    /api/workspaces/{workspace_id}/groups/{id}/members      成员列表
POST   /api/workspaces/{workspace_id}/groups/{id}/members/invite  邀请成员
POST   /api/workspaces/{workspace_id}/groups/{id}/members/join    加入群组（开放模式）
DELETE /api/workspaces/{workspace_id}/groups/{id}/members/{user_id}  移除成员

# 我的群组
GET    /api/users/me/groups                                    我加入的群组列表
```

## 六、权限设计

### 6.1 部门权限

```python
# 部门操作权限
"department:create"   # 创建部门（workspace admin/owner）
"department:read"     # 查看部门（所有成员）
"department:update"   # 更新部门（workspace admin/owner 或部门负责人）
"department:delete"   # 删除部门（workspace owner）
"department:manage_members"  # 管理部门成员（workspace admin/owner 或部门负责人）
```

**权限检查逻辑**：
```python
async def check_department_permission(user: User, department: Department, action: str):
    # 1. 工作区级权限
    workspace_member = await get_workspace_member(department.workspace_id, user.id)
    if workspace_member.role in ["owner", "admin"]:
        return True
    
    # 2. 部门负责人权限
    if action in ["update", "manage_members"] and department.manager_id == user.id:
        return True
    
    # 3. 只读权限（所有成员）
    if action == "read" and workspace_member:
        return True
    
    return False
```

### 6.2 群组权限

```python
# 群组操作权限
"group:create"    # 创建群组（所有工作区成员）
"group:read"      # 查看群组（根据 visibility）
"group:update"    # 更新群组（群组 owner/admin）
"group:delete"    # 删除群组（群组 owner 或 workspace owner）
"group:invite"    # 邀请成员（群组 owner/admin）
"group:remove"    # 移除成员（群组 owner/admin）
```

**权限检查逻辑**：
```python
async def check_group_permission(user: User, group: Group, action: str):
    # 1. 工作区 owner 全权限
    workspace_member = await get_workspace_member(group.workspace_id, user.id)
    if workspace_member.role == "owner":
        return True
    
    # 2. 群组成员权限
    group_member = await get_group_member(group.id, user.id)
    if not group_member:
        # 非成员只能查看公开/工作区可见的群组
        if action == "read" and group.visibility in ["public", "workspace"]:
            return True
        return False
    
    # 3. 群组内角色权限
    if group_member.role in ["owner", "admin"]:
        return action in ["read", "update", "invite", "remove"]
    
    if group_member.role == "member":
        return action == "read"
    
    return False
```

## 七、资源共享设计

### 7.1 会话共享范围扩展

```python
class SessionRecord(Base):
    # 原有字段
    visibility: Mapped[str]  # private/workspace/public
    shared_with: Mapped[list[int]]  # 共享给的用户ID列表
    
    # 新增共享范围
    shared_with_departments: Mapped[list[int]]  # 共享给的部门ID列表
    shared_with_groups: Mapped[list[int]]  # 共享给的群组ID列表
```

**权限检查逻辑**：
```python
async def can_access_session(user: User, session: SessionRecord) -> bool:
    # 1. 会话创建者
    if session.user_id == user.id:
        return True
    
    # 2. workspace 可见性
    if session.visibility == "workspace":
        member = await get_workspace_member(session.workspace_id, user.id)
        return member is not None
    
    # 3. 共享给特定用户
    if user.id in session.shared_with:
        return True
    
    # 4. 共享给部门
    if session.shared_with_departments:
        member = await get_workspace_member(session.workspace_id, user.id)
        if member and member.department_id in session.shared_with_departments:
            return True
    
    # 5. 共享给群组
    if session.shared_with_groups:
        user_groups = await get_user_groups(user.id)
        user_group_ids = [g.id for g in user_groups]
        if any(gid in user_group_ids for gid in session.shared_with_groups):
            return True
    
    return False
```

### 7.2 共享 UI 示例

```python
# POST /api/sessions/{session_id}/share
{
  "visibility": "private",  # 保持私有，但共享给特定范围
  "share_with": {
    "users": [101, 102],  # 用户ID列表
    "departments": [1, 3],  # 部门ID列表（包含子部门）
    "groups": [5, 7]  # 群组ID列表
  }
}
```

## 八、查询优化

### 8.1 部门树查询（递归 CTE）

```sql
-- 查询部门及其所有子孙部门
WITH RECURSIVE department_tree AS (
  -- 锚点：目标部门
  SELECT id, name, parent_id, 0 as level
  FROM departments
  WHERE id = :department_id AND workspace_id = :workspace_id
  
  UNION ALL
  
  -- 递归：子部门
  SELECT d.id, d.name, d.parent_id, dt.level + 1
  FROM departments d
  INNER JOIN department_tree dt ON d.parent_id = dt.id
  WHERE d.workspace_id = :workspace_id
)
SELECT * FROM department_tree ORDER BY level, name;
```

### 8.2 用户的群组列表（多对多）

```sql
-- 查询用户加入的所有群组
SELECT g.*
FROM groups g
INNER JOIN group_members gm ON g.id = gm.group_id
WHERE gm.user_id = :user_id 
  AND gm.is_deleted = false
  AND g.is_deleted = false
ORDER BY gm.joined_at DESC;
```

### 8.3 部门成员统计（包含子部门）

```sql
-- 统计部门及子部门的成员数
WITH RECURSIVE dept_tree AS (
  SELECT id FROM departments WHERE id = :department_id
  UNION ALL
  SELECT d.id FROM departments d
  INNER JOIN dept_tree dt ON d.parent_id = dt.id
)
SELECT COUNT(DISTINCT wm.user_id) as member_count
FROM workspace_members wm
WHERE wm.department_id IN (SELECT id FROM dept_tree)
  AND wm.is_deleted = false;
```

## 九、实施路线图

### Phase 1: 部门基础功能（1-2 周）

- [ ] Department 模型与数据库表
- [ ] 部门 CRUD API
- [ ] WorkspaceMember 添加 department_id
- [ ] 部门树查询接口
- [ ] 基础权限检查

**交付物**：
- 可以创建层级部门结构
- 可以给成员分配部门
- 可以按部门查询成员

### Phase 2: 群组基础功能（1-2 周）

- [ ] Group 和 GroupMember 模型
- [ ] 群组 CRUD API
- [ ] 群组成员管理
- [ ] 群组类型与可见性
- [ ] 加入/退出群组流程

**交付物**：
- 可以创建和管理群组
- 支持邀请制、开放制、审批制
- 用户可以加入多个群组

### Phase 3: 资源共享（1 周）

- [ ] Session 添加部门/群组共享字段
- [ ] 共享 API
- [ ] 权限检查集成
- [ ] 共享通知

**交付物**：
- 会话可以共享给部门
- 会话可以共享给群组
- 按共享范围控制访问权限

### Phase 4: 高级功能（1-2 周）

- [ ] 部门负责人管理
- [ ] 群组管理员功能
- [ ] 批量操作（批量分配部门）
- [ ] 统计报表（部门使用量、群组活跃度）
- [ ] 搜索与筛选优化

## 十、优势总结

### 10.1 灵活性

✅ **部门**：适合稳定的组织结构
- 层级化，对应企业 HR 架构
- 一个成员只属于一个部门（清晰）
- 用于权限继承和统计报表

✅ **群组**：适合灵活的协作场景
- 扁平结构，快速创建
- 一个成员可以加入多个群组（灵活）
- 支持跨部门协作

### 10.2 与 Workspace 的关系

```
Workspace (租户隔离)
├── Departments (正式组织结构)
│   └── 用于：权限管理、成本分摊、报表统计
└── Groups (灵活协作单位)
    └── 用于：项目协作、资源共享、消息通知
```

**Workspace** 是最高层级，提供：
- 数据隔离（多租户）
- 计费边界
- 权限边界

**Departments** 和 **Groups** 是 Workspace 内部的组织方式，不影响租户隔离。

### 10.3 可扩展性

- ✅ 支持从小型团队（只用 Groups）到大型企业（Departments + Groups）
- ✅ 部门可以对接外部 HR 系统（通过 code 字段）
- ✅ 群组可以扩展更多类型（项目、社区、临时任务组等）
- ✅ 资源共享可以继续扩展到其他实体（文档、数据集等）

## 十一、总结与建议

### 回答您的问题

> **增加了 Workspace 的概念，之后如果我还需要在 Workspace 中增加部门和群组的概念的话，你觉得合适吗？方便吗？**

**答案：非常合适且方便！**

### 理由

1. **Workspace 是完美的容器**
   - Workspace 提供了租户隔离边界
   - Departments 和 Groups 都是 Workspace 内的组织方式
   - 不会破坏多租户架构

2. **职责清晰**
   - Workspace：租户隔离 + 计费
   - Department：正式组织结构
   - Group：灵活协作单位

3. **实现简单**
   - 只需添加 2-3 张表
   - 外键关联到 workspace_id
   - 不影响现有的 WorkspaceMember 设计

4. **灵活性高**
   - 小团队可以不用 Departments（只用 Groups）
   - 大企业可以同时使用两者
   - 可以逐步迁移（先不用 → 后面再加）

### 实施建议

1. **MVP 阶段**：只实现 Workspace + WorkspaceMember
2. **成长阶段**：用户有需求时再加 Groups（更常用）
3. **企业阶段**：大客户需要时再加 Departments（更复杂）

这样可以**渐进式实现**，避免过度设计！
