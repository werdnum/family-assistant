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

The automation rows are not the whole story. A recurring schedule automation
enqueues its *next* occurrence as soon as the previous one finishes, and
``schedule_action`` enqueues one-time work the same way, both copying the
provenance into the task payload. So a deployment upgrading with an existing
automation also has a queued ``tasks`` row carrying the removed profile id, which
would fail the same fail-loud resolution -- silently missing the next occurrence
after its retries, or never running the one-time action at all. Live task
payloads are re-stamped too.

An async delegation that was in flight across the upgrade is a third case: its
task payload carries only the ``delegation_id``, and the target profile lives in
``delegation_runs.target_service_id``, which ``handle_delegated_profile_run``
resolves separately and fails the run over. Non-terminal runs are re-stamped as
well so an in-flight authoring request survives the upgrade.
"""

import json
from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "restamp_automation_creation"
down_revision: str | None = "a2a_tasks_json_to_jsonb"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_OLD_PROFILE_ID = "automation_creation"
_NEW_PROFILE_ID = "default_assistant"
_PROFILE_KEY = "processing_profile_id"
_TABLES = ("event_listeners", "schedule_automations")
# Statuses from which a task can still execute: "pending" runs next, "processing"
# is returned to the queue when a worker dies mid-task, and "failed" becomes
# pending again on manual retry. Terminal-success rows are history and left be.
_LIVE_TASK_STATUSES = ("pending", "processing", "failed")
# A delegation run stores its target profile in its own column rather than the
# task payload, and handle_delegated_profile_run fails the run when that target
# no longer resolves. Only non-terminal runs can still be dispatched.
_LIVE_DELEGATION_STATUSES = ("queued", "running")

_delegation_runs_table = sa.table(
    "delegation_runs",
    sa.column("target_service_id", sa.String),
    sa.column("status", sa.String),
)

_tasks_table = sa.table(
    "tasks",
    sa.column("id", sa.Integer),
    sa.column("payload", sa.JSON),
    sa.column("status", sa.String),
)


def _profile_column(table_name: str) -> sa.Table:
    """Minimal table definition for the provenance column being rewritten."""
    return sa.table(
        table_name,
        sa.column("processing_profile_id", sa.String),
    )


def _restamp_live_task_payloads(bind: sa.Connection) -> None:
    """Rewrite the provenance inside queued task payloads.

    Done in Python rather than with a JSON update expression because the two
    supported backends spell that differently (``jsonb_set`` vs ``json_set``),
    and the row count here is small -- only tasks that have not finished yet.
    """
    rows = bind.execute(
        sa.select(_tasks_table.c.id, _tasks_table.c.payload).where(
            _tasks_table.c.status.in_(_LIVE_TASK_STATUSES)
        )
    ).all()

    for task_id, raw_payload in rows:
        payload = raw_payload
        # SQLite hands back the stored text when the column was created without
        # the JSON type by an older migration.
        if isinstance(payload, str):
            try:
                payload = json.loads(payload)
            except ValueError:
                continue
        if not isinstance(payload, dict):
            continue
        if payload.get(_PROFILE_KEY) != _OLD_PROFILE_ID:
            continue
        bind.execute(
            _tasks_table
            .update()
            .where(_tasks_table.c.id == task_id)
            .values(payload={**payload, _PROFILE_KEY: _NEW_PROFILE_ID})
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

    op.execute(
        _delegation_runs_table
        .update()
        .where(_delegation_runs_table.c.target_service_id == _OLD_PROFILE_ID)
        .where(_delegation_runs_table.c.status.in_(_LIVE_DELEGATION_STATUSES))
        .values(target_service_id=_NEW_PROFILE_ID)
    )

    _restamp_live_task_payloads(op.get_bind())


def downgrade() -> None:
    """Not reversible.

    Rows stamped ``default_assistant`` by this migration are indistinguishable
    from those the main assistant stamped itself, so restoring the old value
    would also re-stamp automations that never belonged to the removed profile.
    """
