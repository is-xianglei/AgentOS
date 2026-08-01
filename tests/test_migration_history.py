from types import SimpleNamespace

import sqlalchemy as sa

from database.migration_history import (
    HISTORY_TABLE_NAME,
    include_alembic_name,
    record_version_apply,
)


def _create_history_table(connection: sa.Connection) -> None:
    metadata = sa.MetaData()
    sa.Table(
        HISTORY_TABLE_NAME,
        metadata,
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("revision", sa.String(), nullable=False),
        sa.Column("operation", sa.String(), nullable=False),
        sa.Column("source_revisions", sa.JSON(), nullable=False),
        sa.Column("destination_revisions", sa.JSON(), nullable=False),
        sa.Column("resulting_heads", sa.JSON(), nullable=False),
        sa.Column("is_backfilled", sa.Boolean(), nullable=False),
    )
    metadata.create_all(connection)


def test_history_table_is_excluded_from_autogenerate() -> None:
    assert not include_alembic_name(HISTORY_TABLE_NAME, "table", {})
    assert include_alembic_name("sessions", "table", {})
    assert include_alembic_name("revision", "column", {})


def test_record_version_apply_skips_before_history_table_exists() -> None:
    engine = sa.create_engine("sqlite://")
    with engine.begin() as connection:
        step = SimpleNamespace(
            is_stamp=False,
            is_upgrade=True,
            up_revision_id="0017_workspace_org_units",
            source_revision_ids=("0016_interaction_plan_mode",),
            destination_revision_ids=("0017_workspace_org_units",),
        )
        record_version_apply(
            ctx=SimpleNamespace(connection=connection, as_sql=False),
            step=step,
            heads={"0017_workspace_org_units"},
            run_args={},
        )


def test_record_version_apply_writes_upgrade_in_same_connection() -> None:
    engine = sa.create_engine("sqlite://")
    with engine.begin() as connection:
        _create_history_table(connection)
        step = SimpleNamespace(
            is_stamp=False,
            is_upgrade=True,
            up_revision_id="0018_alembic_history",
            source_revision_ids=("0017_workspace_org_units",),
            destination_revision_ids=("0018_alembic_history",),
        )
        record_version_apply(
            ctx=SimpleNamespace(connection=connection, as_sql=False),
            step=step,
            heads={"0018_alembic_history"},
            run_args={},
        )

        row = connection.execute(
            sa.text("SELECT * FROM alembic_version_history")
        ).mappings().one()

    assert row["revision"] == "0018_alembic_history"
    assert row["operation"] == "upgrade"
    assert row["source_revisions"] == '["0017_workspace_org_units"]'
    assert row["destination_revisions"] == '["0018_alembic_history"]'
    assert row["resulting_heads"] == '["0018_alembic_history"]'
    assert row["is_backfilled"] == 0


def test_record_version_apply_distinguishes_downgrade_and_stamp() -> None:
    engine = sa.create_engine("sqlite://")
    with engine.begin() as connection:
        _create_history_table(connection)
        downgrade = SimpleNamespace(
            is_stamp=False,
            is_upgrade=False,
            up_revision_id="0018_alembic_history",
            source_revision_ids=("0018_alembic_history",),
            destination_revision_ids=("0017_workspace_org_units",),
        )
        stamp = SimpleNamespace(
            is_stamp=True,
            is_upgrade=True,
            up_revision_id="0018_alembic_history",
            source_revision_ids=("0017_workspace_org_units",),
            destination_revision_ids=("0018_alembic_history",),
        )
        context = SimpleNamespace(connection=connection, as_sql=False)
        record_version_apply(
            ctx=context,
            step=downgrade,
            heads={"0017_workspace_org_units"},
            run_args={},
        )
        record_version_apply(
            ctx=context,
            step=stamp,
            heads={"0018_alembic_history"},
            run_args={},
        )
        rows = connection.execute(
            sa.text(
                "SELECT revision, operation FROM alembic_version_history ORDER BY id"
            )
        ).all()

    assert rows == [
        ("0018_alembic_history", "downgrade"),
        ("0018_alembic_history", "stamp"),
    ]
