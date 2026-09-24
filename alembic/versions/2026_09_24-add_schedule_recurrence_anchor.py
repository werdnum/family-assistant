"""Anchor each schedule automation's recurrence series.

Revision ID: add_schedule_recurrence_anchor
Revises: add_authenticated_site_result
Create Date: 2026-09-24

The scheduler used to evaluate a recurrence rule from the moment each firing
finished, so every firing began a new series: a ``COUNT`` never ran out, and a
rule with no fixed clock time slid later by each run's duration. The rule is now
evaluated from a stored anchor.

An existing automation is anchored at its next scheduled firing, so no firing
time moves. The series it was actually created with is not recoverable: its
anchor drifted with every run. A ``COUNT``-bounded automation therefore runs its
full count again from here, which is the accepted cost of not moving anyone's
schedule. The anchor starts on the whole minute, like a newly created one, so
the second a legacy series had crept to does not persist.
"""

from collections.abc import Sequence
from datetime import UTC, datetime

import sqlalchemy as sa

from alembic import op

revision: str = "add_schedule_recurrence_anchor"
down_revision: str | None = "add_authenticated_site_result"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_schedule_automations = sa.table(
    "schedule_automations",
    sa.column("id", sa.Integer),
    sa.column("next_scheduled_at", sa.DateTime(timezone=True)),
    sa.column("created_at", sa.DateTime(timezone=True)),
    sa.column("recurrence_anchor", sa.DateTime(timezone=True)),
)


def _whole_minute(value: datetime) -> datetime:
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return value.replace(second=0, microsecond=0)


def upgrade() -> None:
    """Add the anchor, backfill it from the next firing, then require it."""
    op.add_column(
        "schedule_automations",
        sa.Column("recurrence_anchor", sa.DateTime(timezone=True), nullable=True),
    )

    bind = op.get_bind()
    rows = bind.execute(
        sa.select(
            _schedule_automations.c.id,
            _schedule_automations.c.next_scheduled_at,
            _schedule_automations.c.created_at,
        )
    ).all()
    for row in rows:
        bind.execute(
            sa
            .update(_schedule_automations)
            .where(_schedule_automations.c.id == row.id)
            .values(
                recurrence_anchor=_whole_minute(row.next_scheduled_at or row.created_at)
            )
        )

    with op.batch_alter_table("schedule_automations") as batch_op:
        batch_op.alter_column(
            "recurrence_anchor",
            existing_type=sa.DateTime(timezone=True),
            nullable=False,
        )


def downgrade() -> None:
    """Drop the anchor column."""
    with op.batch_alter_table("schedule_automations") as batch_op:
        batch_op.drop_column("recurrence_anchor")
