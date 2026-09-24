"""Data migration: the anchor existing schedule automations are evaluated from.

An existing automation's original series cannot be recovered -- it drifted with
every run -- so it is anchored at its next firing, which moves no one's
schedule, on the whole minute that a newly created series starts on.
"""

from datetime import UTC, datetime
from pathlib import Path

import sqlalchemy as sa
from alembic.config import Config
from sqlalchemy import create_engine

from alembic import command
from family_assistant.storage.datetime_utils import normalize_datetime

_ALEMBIC_INI = Path(__file__).resolve().parents[3] / "alembic.ini"
_PRIOR_HEAD = "add_authenticated_site_result"
_ANCHOR_HEAD = "add_schedule_recurrence_anchor"
# The migration-built SQLite schema carries a ``now()`` server default for
# ``created_at`` that SQLite cannot evaluate, so rows set it explicitly.
_CREATED_AT = datetime(2026, 9, 1, 12, 34, 56, tzinfo=UTC)

_legacy_schedule_automations = sa.table(
    "schedule_automations",
    sa.column("name", sa.String),
    sa.column("conversation_id", sa.String),
    sa.column("recurrence_rule", sa.Text),
    sa.column("next_scheduled_at", sa.DateTime(timezone=True)),
    sa.column("action_config", sa.JSON),
    sa.column("created_at", sa.DateTime(timezone=True)),
)

_anchored_schedule_automations = sa.table(
    "schedule_automations",
    sa.column("name", sa.String),
    sa.column("recurrence_anchor", sa.DateTime(timezone=True)),
)


def test_anchors_existing_automations_at_their_next_firing(tmp_path: Path) -> None:
    engine = create_engine(f"sqlite:///{tmp_path / 'anchor.db'}")
    try:
        config = Config(str(_ALEMBIC_INI))
        # ast-grep-ignore: no-raw-transaction-management - test fixture setup, outside the application transaction model
        with engine.begin() as conn:
            config.attributes["connection"] = conn
            command.upgrade(config, _PRIOR_HEAD)

        # ast-grep-ignore: no-raw-transaction-management - test fixture setup, outside the application transaction model
        with engine.begin() as conn:
            conn.execute(
                _legacy_schedule_automations.insert(),
                [
                    {
                        "name": "scheduled",
                        "conversation_id": "conv",
                        "recurrence_rule": "FREQ=DAILY;BYHOUR=9;BYMINUTE=0;COUNT=10",
                        "next_scheduled_at": datetime(
                            2026, 9, 25, 9, 0, 37, 250_000, tzinfo=UTC
                        ),
                        "action_config": {"context": "x"},
                        "created_at": _CREATED_AT,
                    },
                    {
                        "name": "never scheduled",
                        "conversation_id": "conv",
                        "recurrence_rule": "FREQ=DAILY",
                        "next_scheduled_at": None,
                        "action_config": {"context": "x"},
                        "created_at": _CREATED_AT,
                    },
                ],
            )

        # ast-grep-ignore: no-raw-transaction-management - test fixture setup, outside the application transaction model
        with engine.begin() as conn:
            config.attributes["connection"] = conn
            command.upgrade(config, _ANCHOR_HEAD)

        with engine.connect() as conn:
            anchors = {
                name: normalize_datetime(anchor)
                for name, anchor in conn.execute(
                    sa.select(
                        _anchored_schedule_automations.c.name,
                        _anchored_schedule_automations.c.recurrence_anchor,
                    )
                ).all()
            }

        assert anchors == {
            "scheduled": datetime(2026, 9, 25, 9, 0, tzinfo=UTC),
            "never scheduled": datetime(2026, 9, 1, 12, 34, tzinfo=UTC),
        }
    finally:
        engine.dispose()
