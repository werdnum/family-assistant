"""Scope ``idx_attachment_source`` to non-email rows.

Revision ID: scope_idx_attachment_source_non_email
Revises: backfill_email_attachment_ids
Create Date: 2026-04-24

``source_id`` is ``Text``, and while that lets email rows store
arbitrarily long ``Message-Id`` headers, the non-unique btree index
``idx_attachment_source`` still covered every row including email ones.
Postgres btree tuples are capped at ~2712 bytes regardless of uniqueness,
so a large ``Message-Id`` could blow past the index-row limit during
``INSERT`` and fail with ``index row requires X bytes, maximum size is
Y``.

Email lookups are already served by the partial unique index on
``email_identity_hash`` (``uix_attachment_metadata_email_identity``), so
this migration simply excludes email rows from ``idx_attachment_source``.
Non-email sources (user, tool, script) keep their short
``source_id`` values and the index remains useful there.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import sqlalchemy as sa

from alembic import op

if TYPE_CHECKING:
    from collections.abc import Sequence

revision: str = "scope_idx_attachment_source_non_email"
down_revision: str | None = "backfill_email_attachment_ids"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.drop_index(
        "idx_attachment_source",
        table_name="attachment_metadata",
    )
    where_clause = sa.text("source_type <> 'email'")
    bind = op.get_bind()
    if bind.dialect.name == "postgresql":
        op.create_index(
            "idx_attachment_source",
            "attachment_metadata",
            ["source_type", "source_id"],
            unique=False,
            postgresql_where=where_clause,
        )
    else:
        op.create_index(
            "idx_attachment_source",
            "attachment_metadata",
            ["source_type", "source_id"],
            unique=False,
            sqlite_where=where_clause,
        )


def downgrade() -> None:
    op.drop_index(
        "idx_attachment_source",
        table_name="attachment_metadata",
    )
    op.create_index(
        "idx_attachment_source",
        "attachment_metadata",
        ["source_type", "source_id"],
        unique=False,
    )
