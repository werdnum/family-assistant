"""Add partial unique index on email attachment identity.

Revision ID: add_email_attachment_unique_idx
Revises: add_email_auth_results
Create Date: 2026-04-23

Email attachments are registered in ``attachment_metadata`` with
``source_type="email"``. Two concurrent indexer runs or a select-then-insert
race could otherwise create duplicate registry rows for the same underlying
attachment. This partial unique index enforces uniqueness for email rows only
(other ``source_type`` values keep their existing duplicate-friendly
semantics) and lets the indexer rely on INSERT conflicts to recover the
canonical ``attachment_id`` via re-query.
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "add_email_attachment_unique_idx"
down_revision: str | None = "add_email_auth_results"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    bind = op.get_bind()
    where_clause = sa.text("source_type = 'email' AND storage_path IS NOT NULL")
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


def downgrade() -> None:
    op.drop_index(
        "uix_attachment_metadata_email_identity",
        table_name="attachment_metadata",
    )
