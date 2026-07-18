"""Add unique active-subconversation index to delegation_runs

Enforces at most one non-terminal (queued/running/awaiting_remote) delegation
run per subconversation_id. A fresh delegation always mints a unique
subconversation_id, so this never constrains the normal path; it atomically
serializes resumes (which reuse a prior run's subconversation_id) so two
concurrent resumes cannot both create active runs that interleave in one
delegated history. Terminal statuses (completed/failed) are excluded so a
finished run can be resumed.

Revision ID: delegation_active_subconv_unique
Revises: attachment_owner_user_id
Create Date: 2026-07-18 01:40:00.000000+00:00

"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "delegation_active_subconv_unique"
down_revision: str | None = "attachment_owner_user_id"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_INDEX_NAME = "uq_delegation_runs_active_subconversation"
_ACTIVE_WHERE = "status NOT IN ('completed', 'failed')"


def upgrade() -> None:
    """Upgrade schema."""
    op.create_index(
        _INDEX_NAME,
        "delegation_runs",
        ["subconversation_id"],
        unique=True,
        sqlite_where=sa.text(_ACTIVE_WHERE),
        postgresql_where=sa.text(_ACTIVE_WHERE),
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index(_INDEX_NAME, table_name="delegation_runs")
