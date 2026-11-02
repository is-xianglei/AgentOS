"""工具执行身份和值域。

工具调用过程中会同时携带数据库、事件总线等运行时依赖，以及 profile、主体和
资源白名单等安全边界。后者必须在网关创建时冻结，不能由单次模型调用覆盖。
"""

from dataclasses import dataclass
from pathlib import Path
from uuid import UUID

from tools.policy import ToolProfile
from tools.shell_policy import ShellAccess


@dataclass(frozen=True)
class ToolExecutionContext:
    """一次工具网关的不可变执行身份。

    该对象只保存安全边界，不包含数据库连接或事件总线；运行时依赖仍由
    ``ToolService`` 在调用时注入。这样上下文可以安全地在恢复、审计和测试中复用。
    """

    profile: ToolProfile
    principal_name: str
    agent_id: int | None = None
    allowed_skill_names: frozenset[str] | None = None
    shell_access: ShellAccess = ShellAccess.FULL
    shell_tmp_root: Path | None = None

    def __post_init__(self) -> None:
        if not self.principal_name.strip():
            raise ValueError("工具执行主体不能为空")
        if self.allowed_skill_names is not None:
            object.__setattr__(
                self,
                "allowed_skill_names",
                frozenset(self.allowed_skill_names),
            )
        if self.shell_tmp_root is not None:
            object.__setattr__(self, "shell_tmp_root", Path(self.shell_tmp_root))

    def with_turn(self, turn_id: UUID | None = None) -> "ToolExecutionContext":
        """保留兼容接口；身份本身不包含 turn，不允许借此变更权限。"""
        del turn_id
        return self
