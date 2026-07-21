"""Migration idempotency against ``metadata.create_all``-built databases.

``init_db`` bootstraps a *new* database via ``metadata.create_all()`` and stamps
it at the current Alembic head, so such a database already carries schema the
model declares (indexes, column-type variants) that historical migrations added
only later. When a create_all-built database that was stamped at an earlier head
is subsequently upgraded, those migrations must tolerate the pre-existing schema
rather than aborting on a duplicate object.

pytest-alembic's built-in tests only exercise the upgrade-from-base path, where
the schema is built purely by migrations, so this gap needs its own coverage.
"""

from pathlib import Path

from alembic.config import Config
from sqlalchemy import create_engine, inspect

from alembic import command
from family_assistant.storage import metadata

_ALEMBIC_INI = Path(__file__).resolve().parents[3] / "alembic.ini"
# The head that predates this PR's migrations, i.e. the down_revision of the
# first migration under test.
_PRIOR_HEAD = "drop_approval_fingerprint"
_MIGRATIONS_HEAD = "a2a_tasks_json_to_jsonb"


def test_upgrade_tolerates_create_all_schema(tmp_path: Path) -> None:
    engine = create_engine(f"sqlite:///{tmp_path / 'create_all.db'}")
    try:
        # New-install path: full model schema (which includes
        # ix_tasks_original_task_id because tasks.original_task_id is index=True),
        # then stamped at the head that predates this PR's migrations.
        metadata.create_all(engine)
        config = Config(str(_ALEMBIC_INI))
        with engine.begin() as conn:
            config.attributes["connection"] = conn
            command.stamp(config, _PRIOR_HEAD)
            # Deploying this PR runs the new migrations against a database that
            # already has the index; the upgrade must succeed rather than
            # failing on the pre-existing object.
            command.upgrade(config, _MIGRATIONS_HEAD)

        with engine.connect() as conn:
            index_names = [ix["name"] for ix in inspect(conn).get_indexes("tasks")]
        assert "ix_tasks_original_task_id" in index_names
    finally:
        engine.dispose()
