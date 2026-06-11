"""Record the originating processing profile on confirmation requests

Adds ``processing_profile_id`` to ``confirmation_requests`` so that a deferred
tool execution (``confirmation_tool_execution``) runs under the profile that
originally requested confirmation. Script-originated confirmations have no
source message row to derive the profile from, so without this the approved
action would fall back to the worker's default profile.

Revision ID: add_confirmation_profile
Revises: add_automation_provenance
Create Date: 2026-06-11 00:00:00.000000+00:00

"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "add_confirmation_profile"
down_revision: str | None = "add_automation_provenance"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column(
        "confirmation_requests",
        sa.Column("processing_profile_id", sa.String(length=255), nullable=True),
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_column("confirmation_requests", "processing_profile_id")
