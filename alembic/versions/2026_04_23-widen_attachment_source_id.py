"""Widen attachment_metadata.source_id to Text.

Revision ID: widen_attachment_source_id
Revises: add_email_attachment_unique_idx
Create Date: 2026-04-23

Email attachments use the Message-Id header as ``source_id``. That header
is stored in ``received_emails`` as ``Text`` and, per RFC, has no hard
upper bound; long-but-valid Message-Ids exceed the previous 255-char
limit and would fail the INSERT into ``attachment_metadata``. Widen
``source_id`` to ``Text`` so every supported ``source_type`` (user,
tool, script, email) can store its natural identifier without
truncation.
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "widen_attachment_source_id"
down_revision: str | None = "add_email_attachment_unique_idx"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # ``batch_alter_table`` handles SQLite's lack of native ALTER COLUMN
    # support by copying the table, and is a no-op rename on Postgres.
    with op.batch_alter_table("attachment_metadata") as batch_op:
        batch_op.alter_column(
            "source_id",
            existing_type=sa.String(length=255),
            type_=sa.Text(),
            existing_nullable=False,
        )


def downgrade() -> None:
    with op.batch_alter_table("attachment_metadata") as batch_op:
        batch_op.alter_column(
            "source_id",
            existing_type=sa.Text(),
            type_=sa.String(length=255),
            existing_nullable=False,
        )
