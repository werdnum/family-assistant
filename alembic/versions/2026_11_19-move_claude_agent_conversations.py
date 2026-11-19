"""Move what the removed ``claude_agent`` profile owned to ``default_assistant``.

Revision ID: move_claude_agent_conversations
Revises: add_review_rejection_notice

The ``claude_agent`` profile ran Opus in a Claude Code session. That is now the
``deep`` tier of ``default_assistant`` (a tier with ``runtime: claude_code``), so
``default_assistant`` is where its conversations continue. Both are full-trust
[BC] profiles.

Conversations: a thread pinned to ``claude_agent`` refuses every new turn once
the profile is gone, and a Telegram reply to an old ``/claude`` message falls
back to the default profile. History is read per profile, so either way the
continuation would not see the earlier turns. Threads and their history rows are
moved together. Unlike ``restamp_automation_creation``, ``message_history`` is
rewritten here: its profile column is the partition a turn reads its history
from, and leaving it would strand the conversation. What actually ran stays on
record in ``claude_agent_session_turns`` and ``taint_audit``, which are left be,
as are ``delegation_runs.source_profile_id`` (a label) and
``history_compaction_events`` (a moved conversation is simply compacted afresh).

Executable work follows ``restamp_automation_creation``, for the same reason:
profile resolution at execution is fail-loud, so automations, queued task
payloads, in-flight delegations and outstanding confirmations stamped with the
removed profile would raise instead of running.
"""

import json
from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "move_claude_agent_conversations"
down_revision: str | None = "add_review_rejection_notice"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_OLD_PROFILE_ID = "claude_agent"
_NEW_PROFILE_ID = "default_assistant"
_PROFILE_KEY = "processing_profile_id"
_PROFILE_COLUMNS = (
    ("conversation_threads", "profile_id"),
    ("message_history", "processing_profile_id"),
    ("event_listeners", "processing_profile_id"),
    ("schedule_automations", "processing_profile_id"),
)
_LIVE_TASK_STATUSES = ("pending", "processing", "failed")
_LIVE_DELEGATION_STATUSES = ("queued", "running")
_LIVE_CONFIRMATION_STATUSES = ("pending", "approved")

_delegation_runs = sa.table(
    "delegation_runs",
    sa.column("target_service_id", sa.String),
    sa.column("status", sa.String),
)
_confirmation_requests = sa.table(
    "confirmation_requests",
    sa.column("processing_profile_id", sa.String),
    sa.column("status", sa.String),
)
_tasks = sa.table(
    "tasks",
    sa.column("id", sa.Integer),
    sa.column("payload", sa.JSON),
    sa.column("status", sa.String),
)


def _restamp_live_task_payloads(bind: sa.Connection) -> None:
    """Rewrite the profile inside queued task payloads, in Python for portability."""
    rows = bind.execute(
        sa.select(_tasks.c.id, _tasks.c.payload).where(
            _tasks.c.status.in_(_LIVE_TASK_STATUSES)
        )
    ).all()
    for task_id, raw_payload in rows:
        payload = raw_payload
        if isinstance(payload, str):
            try:
                payload = json.loads(payload)
            except ValueError:
                continue
        if (
            not isinstance(payload, dict)
            or payload.get(_PROFILE_KEY) != _OLD_PROFILE_ID
        ):
            continue
        bind.execute(
            _tasks
            .update()
            .where(_tasks.c.id == task_id)
            .values(payload={**payload, _PROFILE_KEY: _NEW_PROFILE_ID})
        )


def upgrade() -> None:
    for table_name, column_name in _PROFILE_COLUMNS:
        table = sa.table(table_name, sa.column(column_name, sa.String))
        column = table.c[column_name]
        op.execute(
            table
            .update()
            .where(column == _OLD_PROFILE_ID)
            .values({column_name: _NEW_PROFILE_ID})
        )

    op.execute(
        _delegation_runs
        .update()
        .where(_delegation_runs.c.target_service_id == _OLD_PROFILE_ID)
        .where(_delegation_runs.c.status.in_(_LIVE_DELEGATION_STATUSES))
        .values(target_service_id=_NEW_PROFILE_ID)
    )
    op.execute(
        _confirmation_requests
        .update()
        .where(_confirmation_requests.c.processing_profile_id == _OLD_PROFILE_ID)
        .where(_confirmation_requests.c.status.in_(_LIVE_CONFIRMATION_STATUSES))
        .values(processing_profile_id=_NEW_PROFILE_ID)
    )
    _restamp_live_task_payloads(op.get_bind())


def downgrade() -> None:
    """Not reversible: moved rows are indistinguishable from default_assistant's own."""
