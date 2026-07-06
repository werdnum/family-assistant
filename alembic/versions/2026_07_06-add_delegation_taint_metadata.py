"""Add taint metadata to delegation runs."""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "delegation_taint_metadata"
down_revision: str | Sequence[str] | None = "confirmation_taint_metadata"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Add compact parent taint state to async delegation runs."""
    op.add_column(
        "delegation_runs",
        sa.Column(
            "taint_state_json",
            sa.JSON().with_variant(
                postgresql.JSONB(astext_type=sa.Text()), "postgresql"
            ),
            nullable=True,
        ),
    )


def downgrade() -> None:
    """Remove delegation taint state."""
    op.drop_column("delegation_runs", "taint_state_json")
