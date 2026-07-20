"""Drop unused approval_policy_fingerprint from confirmation requests

The approval_policy_fingerprint column fed a single execution-time guard that
recomputed a hash of the confirmation's own stored fields and compared it to the
stored hash. Because every fingerprint input was persisted verbatim on the same
row at creation, the check was a stored-vs-stored tautology that could only fire
on impossible states -- and its one real-world effect was spuriously rejecting
every confirmation created without a pinned processing profile. The guard, the
fingerprint helper, and this column are removed; profile drift is already caught
by _resolve_confirmation_processing_service and argument integrity by the nested
approved_confirmation_callback.

Revision ID: drop_approval_fingerprint
Revises: delegation_active_subconv_unique
Create Date: 2026-07-19 00:00:00.000000+00:00

"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "drop_approval_fingerprint"
down_revision: str | None = "delegation_active_subconv_unique"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    op.drop_column("confirmation_requests", "approval_policy_fingerprint")


def downgrade() -> None:
    """Downgrade schema."""
    op.add_column(
        "confirmation_requests",
        sa.Column("approval_policy_fingerprint", sa.String(length=255), nullable=True),
    )
