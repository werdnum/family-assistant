"""Record the origin interface/conversation on confirmation requests

Adds ``origin_interface_type`` and ``origin_conversation_id`` to
``confirmation_requests``. Automation-script confirmations have no source
message row, so without these the deferred ``confirmation_tool_execution``
rebuilt its context from the worker defaults ("unknown_conversation") and
approved tools could stamp or act in the wrong conversation.

Revision ID: add_confirmation_origin
Revises: add_confirmation_profile
Create Date: 2026-06-11 12:00:00.000000+00:00

"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "add_confirmation_origin"
down_revision: str | None = "add_confirmation_profile"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column(
        "confirmation_requests",
        sa.Column("origin_interface_type", sa.String(length=50), nullable=True),
    )
    op.add_column(
        "confirmation_requests",
        sa.Column("origin_conversation_id", sa.String(length=255), nullable=True),
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_column("confirmation_requests", "origin_conversation_id")
    op.drop_column("confirmation_requests", "origin_interface_type")
