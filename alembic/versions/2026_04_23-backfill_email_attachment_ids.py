"""Backfill attachment_ids for legacy emails by enqueueing reindex tasks.

Revision ID: backfill_email_attachment_ids
Revises: widen_attachment_source_id
Create Date: 2026-04-23

Email attachments received before this PR have ``attachment_info`` rows
on ``received_emails`` but no ``attachment_id`` values, so
``get_full_document_content`` surfaces them with ``attachment_id: null``
and they are unusable via ``read_text_attachment`` /
``get_attachment_info`` until the email is reindexed.

``get_full_document_content`` is now tagged ``READ_ONLY`` and no longer
auto-enqueues a reindex from the read path. This one-time data
migration closes the gap by enqueueing an ``index_email`` task for
every email that has attachments with no ``attachment_id``. The
indexer's partial unique index
(``uix_attachment_metadata_email_identity``) keeps registration
idempotent, so duplicate work is harmless.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import TYPE_CHECKING

import sqlalchemy as sa

from alembic import op

if TYPE_CHECKING:
    from collections.abc import Sequence

revision: str = "backfill_email_attachment_ids"
# Runs AFTER ``email_attachment_identity_hash`` so that any ``index_email``
# task picked up while the migration is still running (or right after) sees
# the new ``attachment_metadata.email_identity_hash`` column that the
# indexer now reads and writes.
down_revision: str | None = "email_attachment_identity_hash"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _needs_reindex(attachment_info: object) -> bool:
    """Return True if ``attachment_info`` has at least one entry missing an id."""
    if not isinstance(attachment_info, list):
        return False
    for entry in attachment_info:
        if not isinstance(entry, dict):
            continue
        if not entry.get("attachment_id"):
            return True
    return False


def upgrade() -> None:
    bind = op.get_bind()
    emails = sa.Table(
        "received_emails",
        sa.MetaData(),
        sa.Column("id", sa.BigInteger, primary_key=True),
        sa.Column("attachment_info", sa.JSON),
    )
    tasks = sa.Table(
        "tasks",
        sa.MetaData(),
        sa.Column("task_id", sa.String),
        sa.Column("task_type", sa.String),
        sa.Column("payload", sa.JSON),
        sa.Column("scheduled_at", sa.DateTime(timezone=True)),
        sa.Column("created_at", sa.DateTime(timezone=True)),
        sa.Column("status", sa.String),
        sa.Column("retry_count", sa.Integer),
        sa.Column("max_retries", sa.Integer),
        sa.Column("original_task_id", sa.String),
    )

    # Stream the result set — for a large mailbox the full
    # ``attachment_info`` JSON blob can be tens of MB per row, so a plain
    # ``fetchall()`` would materialize the whole table into memory.
    # Filter out obviously-empty rows at the SQL level, then iterate
    # with a bounded yield_per window.
    select_stmt = (
        sa
        .select(emails.c.id, emails.c.attachment_info)
        .where(emails.c.attachment_info.isnot(None))
        .execution_options(yield_per=500, stream_results=True)
    )
    for row in bind.execute(select_stmt):
        email_db_id = row[0]
        attachment_info = row[1]
        if not _needs_reindex(attachment_info):
            continue
        task_id = f"index_email_{email_db_id}_{uuid.uuid4()}"
        bind.execute(
            sa.insert(tasks).values(
                task_id=task_id,
                task_type="index_email",
                payload={"email_db_id": email_db_id},
                status="pending",
                # ``tasks.created_at`` is NOT NULL with no server default —
                # populate it explicitly so the migration works on a DB
                # where the column was never defaulted by a prior INSERT.
                created_at=datetime.now(UTC),
                retry_count=0,
                max_retries=3,
                original_task_id=task_id,
            )
        )


def downgrade() -> None:
    # Nothing to reverse — the enqueued tasks, whether they ran or not,
    # only registered attachment_metadata rows and are safe to leave in
    # place.
    pass
