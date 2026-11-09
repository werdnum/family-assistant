"""Record a delegation run's earlier trusted requests on the run

Revision ID: delegation_earlier_requests
Revises: add_confirmation_origin_subconv
Create Date: 2026-11-04

"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB

from alembic import op

revision: str = "delegation_earlier_requests"
down_revision: str | None = "add_confirmation_origin_subconv"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("delegation_runs") as batch_op:
        batch_op.add_column(
            sa.Column(
                "earlier_requests_json",
                sa.JSON().with_variant(JSONB(), "postgresql"),
                nullable=True,
            )
        )


def downgrade() -> None:
    with op.batch_alter_table("delegation_runs") as batch_op:
        batch_op.drop_column("earlier_requests_json")
