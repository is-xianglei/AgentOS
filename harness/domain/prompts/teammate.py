"""队友的提示文案：系统提示开头的身份头，以及历史首条的身份块。

身份头叠在子代理规格提示之上，不改规格本身；身份块是一条 user 消息，随成员历史落库，
每次唤醒前检查首条是否已是身份块，避免重复注入。
"""

IDENTITY_BLOCK_PREFIX = "<identity>"
IDENTITY_BLOCK_MARKER = "Continue your work."


def render_teammate_header(session_id: int, name: str, role: str) -> str:
    """队友系统提示开头的身份头。"""
    return (
        f"你是团队成员 '{name}',角色: {role},隶属会话 {session_id} 的团队。\n"
        f"你可以通过 Team 工具与其他成员协作,并认领本会话的待办任务。\n"
    )


def render_identity_block(session_id: int, name: str, role: str) -> str:
    """历史首条的身份块(对应 s11 的 make_identity_block)。"""
    return (
        f"<identity>You are '{name}', role: {role}, "
        f"team session {session_id}. Continue your work.</identity>"
    )
