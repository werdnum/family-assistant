"""Re-stamp automations owned by the removed ``automation_creation`` profile.

Revision ID: restamp_automation_creation
Revises: a2a_tasks_json_to_jsonb
Create Date: 2026-08-04

The ``automation_creation`` profile has been replaced by the "Automation
Creation" built-in skill, which the main assistant loads to activate the
automation tools. Automations created by the old profile carry
``processing_profile_id='automation_creation'`` in their creator provenance.

Profile resolution at execution time is deliberately fail-loud: an automation
stamped with a profile that no longer resolves raises rather than silently
downgrading to the default profile (see ``docs/design/automation_provenance.md``).
Removing the profile without re-stamping would therefore turn every such
automation from "runs" into "raises on every fire", so this migration moves them
to ``default_assistant`` -- the profile the work was always meant to run under,
and the one the skill now stamps.

For ``wake_llm`` automations this restores the intended behaviour: they woke into
the narrow authoring profile and could not reach the tools the task needed. For
``script`` automations it widens the tool set the script is validated and
executed against, from the authoring profile's to the main assistant's. That is a
deliberate, accepted trade-off: both profiles are full-trust [BC], and
``default_assistant`` is a superset of what ``automation_creation`` could reach.
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "restamp_automation_creation"
down_revision: str | None = "a2a_tasks_json_to_jsonb"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_OLD_PROFILE_ID = "automation_creation"
_NEW_PROFILE_ID = "default_assistant"
_TABLES = ("event_listeners", "schedule_automations")


def _profile_column(table_name: str) -> sa.Table:
    """Minimal table definition for the provenance column being rewritten."""
    return sa.table(
        table_name,
        sa.column("processing_profile_id", sa.String),
    )


def upgrade() -> None:
    for table_name in _TABLES:
        table = _profile_column(table_name)
        op.execute(
            table
            .update()
            .where(table.c.processing_profile_id == _OLD_PROFILE_ID)
            .values(processing_profile_id=_NEW_PROFILE_ID)
        )


def downgrade() -> None:
    """Not reversible.

    Rows stamped ``default_assistant`` by this migration are indistinguishable
    from those the main assistant stamped itself, so restoring the old value
    would also re-stamp automations that never belonged to the removed profile.
    """
