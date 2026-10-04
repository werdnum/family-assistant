"""Record the on-demand tools a tool result activated.

Revision ID: add_message_activated_tools
Revises: add_taint_audit_result_tier
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "add_message_activated_tools"
down_revision: str | None = "add_taint_audit_result_tier"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Persist activations so they last for the rest of the conversation."""
    op.add_column(
        "message_history",
        sa.Column(
            "activated_tools",
            sa.JSON().with_variant(postgresql.JSONB(), "postgresql"),
            nullable=True,
        ),
    )


def downgrade() -> None:
    """Remove the activation record."""
    op.drop_column("message_history", "activated_tools")
