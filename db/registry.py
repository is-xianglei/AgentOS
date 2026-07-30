"""ORM 模型注册入口：导入所有实体，确保 Base.metadata 与映射关系完整。

Alembic autogenerate 与字符串式 relationship() 都依赖此处的副作用导入。
遗漏任一模块会导致自动迁移误判为删表，因此新增实体必须同步登记到这里。
"""

# 按 feature 包组织的实体
from permission.models import PermissionRuleRecord  # noqa: F401
from session.models import (  # noqa: F401
    SessionMessage,
    SessionRecord,
    SessionSnapshot,
    SessionTurnRecord,
)
from user.models import UserRecord  # noqa: F401
from workspace.models import WorkspaceMemberRecord, WorkspaceRecord  # noqa: F401

# 仍位于 models/ 的实体（随域迁移逐步移出）
from models import (  # noqa: F401
    MemoryItemRecord,
    MemoryJobRecord,
    MemoryRevisionRecord,
    MemorySourceRecord,
    MemorySpaceRecord,
    SkillRecord,
    SubAgentRunRecord,
    TaskRecord,
    TeamMemberRecord,
    TeamMessageRecord,
    TeamRecord,
    ToolCallRecord,
    TurnMemoryContextRecord,
)
