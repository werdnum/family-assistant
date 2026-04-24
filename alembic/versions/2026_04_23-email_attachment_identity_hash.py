"""Replace partial unique index on raw Text columns with bounded hash.

Revision ID: email_attachment_identity_hash
Revises: widen_attachment_source_id
Create Date: 2026-04-23

The previous ``uix_attachment_metadata_email_identity`` index used the raw
``source_id`` and ``storage_path`` columns (both ``Text``). Postgres
btree index tuples are capped at ~2712 bytes, so long-but-valid
``Message-Id`` headers or long external paths could fail the INSERT with
``index row requires X bytes, maximum size is Y``.

Switch to a bounded surrogate: ``email_identity_hash`` is the SHA-256
hex digest of ``f"{source_id}\\0{storage_path}"`` (64 chars). The
partial unique index now keys on that fixed-length column, so the index
row size stays small regardless of the underlying identifier length.
The column is nullable; only email attachments populate it.
"""

from __future__ import annotations

import hashlib
from typing import TYPE_CHECKING

import sqlalchemy as sa

from alembic import op

if TYPE_CHECKING:
    from collections.abc import Sequence

revision: str = "email_attachment_identity_hash"
down_revision: str | None = "widen_attachment_source_id"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _identity_hash(source_id: str, storage_path: str) -> str:
    return hashlib.sha256(f"{source_id}\0{storage_path}".encode()).hexdigest()


def upgrade() -> None:
    # Drop the old partial unique index (uses Text columns directly).
    op.drop_index(
        "uix_attachment_metadata_email_identity",
        table_name="attachment_metadata",
    )

    with op.batch_alter_table("attachment_metadata") as batch_op:
        batch_op.add_column(
            sa.Column("email_identity_hash", sa.String(length=64), nullable=True)
        )

    # Backfill for any existing email rows.
    bind = op.get_bind()
    attachment_metadata = sa.Table(
        "attachment_metadata",
        sa.MetaData(),
        sa.Column("attachment_id", sa.String(length=36), primary_key=True),
        sa.Column("source_type", sa.String(length=20)),
        sa.Column("source_id", sa.Text),
        sa.Column("storage_path", sa.Text),
        sa.Column("email_identity_hash", sa.String(length=64)),
    )
    rows = bind.execute(
        sa.select(
            attachment_metadata.c.attachment_id,
            attachment_metadata.c.source_id,
            attachment_metadata.c.storage_path,
        ).where(
            attachment_metadata.c.source_type == "email",
        )
    ).fetchall()
    for row in rows:
        attachment_id, source_id, storage_path = row[0], row[1], row[2]
        if source_id is None or storage_path is None:
            continue
        identity_hash = _identity_hash(source_id, storage_path)
        bind.execute(
            sa
            .update(attachment_metadata)
            .where(attachment_metadata.c.attachment_id == attachment_id)
            .values(email_identity_hash=identity_hash)
        )

    # Recreate the partial unique index on the bounded hash column.
    where_clause = sa.text("source_type = 'email' AND email_identity_hash IS NOT NULL")
    if bind.dialect.name == "postgresql":
        op.create_index(
            "uix_attachment_metadata_email_identity",
            "attachment_metadata",
            ["email_identity_hash"],
            unique=True,
            postgresql_where=where_clause,
        )
    else:
        op.create_index(
            "uix_attachment_metadata_email_identity",
            "attachment_metadata",
            ["email_identity_hash"],
            unique=True,
            sqlite_where=where_clause,
        )


def downgrade() -> None:
    op.drop_index(
        "uix_attachment_metadata_email_identity",
        table_name="attachment_metadata",
    )
    with op.batch_alter_table("attachment_metadata") as batch_op:
        batch_op.drop_column("email_identity_hash")

    where_clause = sa.text("source_type = 'email' AND storage_path IS NOT NULL")
    bind = op.get_bind()
    if bind.dialect.name == "postgresql":
        op.create_index(
            "uix_attachment_metadata_email_identity",
            "attachment_metadata",
            ["source_id", "storage_path"],
            unique=True,
            postgresql_where=where_clause,
        )
    else:
        op.create_index(
            "uix_attachment_metadata_email_identity",
            "attachment_metadata",
            ["source_id", "storage_path"],
            unique=True,
            sqlite_where=where_clause,
        )
