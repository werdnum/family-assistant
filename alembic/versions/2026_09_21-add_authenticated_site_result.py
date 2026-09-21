"""Add the authenticated-site envelope to delegation runs.

Revision ID: add_authenticated_site_result
Revises: add_mcp_oauth_clients
Create Date: 2026-09-21

"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "add_authenticated_site_result"
down_revision: str | None = "add_mcp_oauth_clients"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Add the nullable authenticated-site envelope column."""
    op.add_column(
        "delegation_runs",
        sa.Column(
            "authenticated_site_json",
            sa.JSON().with_variant(postgresql.JSONB(), "postgresql"),
            nullable=True,
        ),
    )


def downgrade() -> None:
    """Drop the authenticated-site envelope column."""
    op.drop_column("delegation_runs", "authenticated_site_json")
