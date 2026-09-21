"""Add model selection to delegation runs.

Revision ID: d7065490c04e
Revises: 631e7ea62ec4
Create Date: 2026-09-05 18:55:25.705806+10:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "d7065490c04e"
down_revision: str | None = "631e7ea62ec4"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Persist the model-selection envelope a delegated run was created with.

    Nullable, so runs queued before tier selection existed -- and runs on a
    target that admits no selection -- carry nothing and resolve to the
    target's own model when the worker picks them up.
    """
    op.add_column(
        "delegation_runs",
        sa.Column(
            "model_selection_json",
            sa.JSON().with_variant(
                postgresql.JSONB(astext_type=sa.Text()), "postgresql"
            ),
            nullable=True,
        ),
    )


def downgrade() -> None:
    """Drop the persisted model-selection envelope."""
    op.drop_column("delegation_runs", "model_selection_json")
