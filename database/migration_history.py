from collections.abc import Collection, Mapping
from typing import Any

import sqlalchemy as sa
from alembic.runtime.migration import MigrationContext, MigrationInfo

HISTORY_TABLE_NAME = "alembic_version_history"

_history_table = sa.table(
    HISTORY_TABLE_NAME,
    sa.column("revision", sa.String()),
    sa.column("operation", sa.String()),
    sa.column("source_revisions", sa.JSON()),
    sa.column("destination_revisions", sa.JSON()),
    sa.column("resulting_heads", sa.JSON()),
    sa.column("is_backfilled", sa.Boolean()),
)


def include_alembic_name(
    name: str | None,
    type_: str,
    parent_names: dict[str, str | None],
) -> bool:
    """从 autogenerate 中排除 Alembic 自身维护的历史表。"""
    del parent_names
    return type_ != "table" or name != HISTORY_TABLE_NAME


def record_version_apply(
    *,
    ctx: MigrationContext,
    step: MigrationInfo,
    heads: Collection[str],
    run_args: Mapping[str, Any],
) -> None:
    """在迁移事务内记录版本变化；历史表尚未创建时跳过。"""
    del run_args
    if ctx.as_sql:
        return

    connection = ctx.connection
    if connection is None or not sa.inspect(connection).has_table(HISTORY_TABLE_NAME):
        return

    if step.is_stamp:
        operation = "stamp"
        revision_ids = step.destination_revision_ids or step.source_revision_ids
        revision = ",".join(revision_ids)
    else:
        operation = "upgrade" if step.is_upgrade else "downgrade"
        revision = step.up_revision_id

    if not revision:
        raise RuntimeError("无法确定本次 Alembic 版本变化对应的 revision")

    connection.execute(
        sa.insert(_history_table).values(
            revision=revision,
            operation=operation,
            source_revisions=list(step.source_revision_ids),
            destination_revisions=list(step.destination_revision_ids),
            resulting_heads=sorted(heads),
            is_backfilled=False,
        )
    )
