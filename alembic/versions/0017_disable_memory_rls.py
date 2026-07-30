"""关闭 Memory RLS，统一使用应用层用户与工作区隔离。

Revision ID: 0017_disable_memory_rls
Revises: 0016_enable_memory_rls
Create Date: 2026-07-26
"""

from alembic import op

revision = "0017_disable_memory_rls"
down_revision = "0016_enable_memory_rls"
branch_labels = None
depends_on = None

_TABLES = (
    "memory_spaces",
    "memory_items",
    "memory_revisions",
    "memory_sources",
    "turn_memory_contexts",
    "memory_jobs",
)
_WORKER_ROLE = "agentos_memory_worker"
_WORKER_READ_TABLES = (
    "workspaces",
    "workspace_members",
    "sessions",
    "session_messages",
    "session_turns",
)
_WORKER_SEQUENCES = (
    "memory_spaces_id_seq",
    "memory_items_id_seq",
    "memory_revisions_id_seq",
    "memory_sources_id_seq",
    "turn_memory_contexts_id_seq",
)
_API_USER_ID = """(
    CASE
        WHEN current_setting('app.user_id', true) ~ '^[1-9][0-9]{0,9}$'
        THEN current_setting('app.user_id', true)::bigint
        ELSE NULL
    END
)"""
_API_WORKSPACE_ID = """(
    CASE
        WHEN current_setting('app.workspace_id', true) ~ '^[1-9][0-9]{0,9}$'
        THEN current_setting('app.workspace_id', true)::bigint
        ELSE NULL
    END
)"""


def _active_membership(*, workspace_expression: str, user_expression: str) -> str:
    return f"""
        EXISTS (
            SELECT 1
            FROM workspace_members AS scoped_member
            JOIN workspaces AS scoped_workspace
              ON scoped_workspace.id = scoped_member.workspace_id
            WHERE scoped_member.workspace_id = {workspace_expression}
              AND scoped_member.user_id = {user_expression}
              AND scoped_member.joined_at IS NOT NULL
              AND scoped_member.is_deleted IS FALSE
              AND scoped_workspace.is_deleted IS FALSE
              AND scoped_workspace.suspended IS FALSE
        )
    """


def _space_scope(space_expression: str) -> str:
    membership = _active_membership(
        workspace_expression="scoped_space.workspace_id",
        user_expression="scoped_space.user_id",
    )
    return f"""
        EXISTS (
            SELECT 1
            FROM memory_spaces AS scoped_space
            WHERE scoped_space.id = {space_expression}
              AND scoped_space.user_id = {_API_USER_ID}
              AND scoped_space.workspace_id = {_API_WORKSPACE_ID}
              AND {membership}
        )
    """


def _memory_scope(memory_expression: str) -> str:
    membership = _active_membership(
        workspace_expression="scoped_space.workspace_id",
        user_expression="scoped_space.user_id",
    )
    return f"""
        EXISTS (
            SELECT 1
            FROM memory_items AS scoped_item
            JOIN memory_spaces AS scoped_space ON scoped_space.id = scoped_item.space_id
            WHERE scoped_item.id = {memory_expression}
              AND scoped_space.user_id = {_API_USER_ID}
              AND scoped_space.workspace_id = {_API_WORKSPACE_ID}
              AND {membership}
        )
    """


def _turn_context_scope() -> str:
    membership = _active_membership(
        workspace_expression="scoped_turn.workspace_id",
        user_expression="scoped_turn.user_id",
    )
    return f"""
        EXISTS (
            SELECT 1
            FROM session_turns AS scoped_turn
            WHERE scoped_turn.id = turn_memory_contexts.turn_id
              AND scoped_turn.user_id = {_API_USER_ID}
              AND scoped_turn.workspace_id = {_API_WORKSPACE_ID}
              AND {membership}
              AND (
                  turn_memory_contexts.space_id IS NULL
                  OR EXISTS (
                      SELECT 1
                      FROM memory_spaces AS context_space
                      WHERE context_space.id = turn_memory_contexts.space_id
                        AND context_space.user_id = scoped_turn.user_id
                        AND context_space.workspace_id = scoped_turn.workspace_id
                  )
              )
        )
    """


def _create_policies(table: str, api_scope: str) -> None:
    op.execute(
        f"""
        CREATE POLICY {table}_api_scope ON {table}
        FOR ALL TO PUBLIC
        USING ({api_scope})
        WITH CHECK ({api_scope})
        """
    )
    op.execute(
        f"""
        CREATE POLICY {table}_worker_all ON {table}
        FOR ALL TO PUBLIC
        USING (session_user = '{_WORKER_ROLE}')
        WITH CHECK (session_user = '{_WORKER_ROLE}')
        """
    )


def _revoke_worker_permissions_if_role_exists() -> None:
    memory_tables = ", ".join(_TABLES)
    read_tables = ", ".join(_WORKER_READ_TABLES)
    sequences = ", ".join(_WORKER_SEQUENCES)
    op.execute(
        f"""
        DO $$
        BEGIN
            IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = '{_WORKER_ROLE}') THEN
                REVOKE SELECT, INSERT, UPDATE, DELETE ON {memory_tables} FROM {_WORKER_ROLE};
                REVOKE SELECT ON {read_tables} FROM {_WORKER_ROLE};
                REVOKE USAGE, SELECT ON SEQUENCE {sequences} FROM {_WORKER_ROLE};
            END IF;
        END
        $$
        """
    )


def _grant_worker_permissions_if_role_exists() -> None:
    memory_tables = ", ".join(_TABLES)
    read_tables = ", ".join(_WORKER_READ_TABLES)
    sequences = ", ".join(_WORKER_SEQUENCES)
    op.execute(
        f"""
        DO $$
        BEGIN
            IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = '{_WORKER_ROLE}') THEN
                GRANT SELECT, INSERT, UPDATE, DELETE ON {memory_tables} TO {_WORKER_ROLE};
                GRANT SELECT ON {read_tables} TO {_WORKER_ROLE};
                GRANT USAGE, SELECT ON SEQUENCE {sequences} TO {_WORKER_ROLE};
            END IF;
        END
        $$
        """
    )


def upgrade() -> None:
    for table in reversed(_TABLES):
        op.execute(f"DROP POLICY IF EXISTS {table}_worker_all ON {table}")
        op.execute(f"DROP POLICY IF EXISTS {table}_api_scope ON {table}")
        op.execute(f"ALTER TABLE {table} NO FORCE ROW LEVEL SECURITY")
        op.execute(f"ALTER TABLE {table} DISABLE ROW LEVEL SECURITY")
    _revoke_worker_permissions_if_role_exists()


def downgrade() -> None:
    for table in _TABLES:
        op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
        op.execute(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY")

    membership = _active_membership(
        workspace_expression="memory_spaces.workspace_id",
        user_expression="memory_spaces.user_id",
    )
    spaces_scope = f"""
        memory_spaces.user_id = {_API_USER_ID}
        AND memory_spaces.workspace_id = {_API_WORKSPACE_ID}
        AND {membership}
    """
    _create_policies("memory_spaces", spaces_scope)
    _create_policies("memory_items", _space_scope("memory_items.space_id"))
    _create_policies("memory_revisions", _memory_scope("memory_revisions.memory_id"))
    _create_policies("memory_sources", _memory_scope("memory_sources.memory_id"))
    _create_policies("turn_memory_contexts", _turn_context_scope())
    _create_policies("memory_jobs", _space_scope("memory_jobs.space_id"))
    _grant_worker_permissions_if_role_exists()
