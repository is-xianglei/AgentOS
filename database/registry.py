"""ORM 模型注册入口：导入所有实体，确保 Base.metadata 与映射关系完整。

Alembic autogenerate 与字符串式 relationship() 都依赖此处的副作用导入。
遗漏任一模块会导致自动迁移误判为删表，因此新增实体必须同步登记到这里。
"""

from memory.models import (  # noqa: F401
    MemoryItemRecord,
    MemoryJobRecord,
    MemoryRevisionRecord,
    MemorySourceRecord,
    MemorySpaceRecord,
    TurnMemoryContextRecord,
)
from permission.models import PermissionRuleRecord  # noqa: F401
from session.models import (  # noqa: F401
    SessionMessage,
    SessionRecord,
    SessionSnapshot,
    SessionTurnRecord,
)
from skill.models import SkillRecord  # noqa: F401
from task.models import TaskRecord  # noqa: F401
from team.models import (  # noqa: F401
    SubAgentRunRecord,
    TeamMemberRecord,
    TeamMessageRecord,
    TeamRecord,
)
from tools.models import ToolCallRecord  # noqa: F401
from user.models import UserRecord  # noqa: F401
from workspace.models import WorkspaceMemberRecord, WorkspaceRecord  # noqa: F401
