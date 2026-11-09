"""Add origin_subconversation_id to confirmation_requests

Revision ID: add_confirmation_origin_subconv
Revises: merge_iface_type_msg_shares
Create Date: 2026-11-07 00:00:00.000000+00:00

"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "add_confirmation_origin_subconv"
down_revision: str | None = "merge_iface_type_msg_shares"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Record the subconversation a deferred confirmation was requested in."""
    op.add_column(
        "confirmation_requests",
        sa.Column("origin_subconversation_id", sa.String(36), nullable=True),
    )


def downgrade() -> None:
    """Remove origin_subconversation_id."""
    op.drop_column("confirmation_requests", "origin_subconversation_id")
