"""Merge the history compaction and confirmation resolution heads.

Both revisions were written against add_message_activated_tools and merged on
the same day, leaving two heads, which `alembic upgrade head` refuses. A
merge revision rather than re-parenting either: it upgrades correctly whichever
of the two a database has already applied.

Revision ID: merge_compaction_confirm_heads
Revises: add_history_compaction_events, add_confirm_resolved_by_system
"""

from collections.abc import Sequence

revision: str = "merge_compaction_confirm_heads"
down_revision: str | Sequence[str] | None = (
    "add_history_compaction_events",
    "add_confirm_resolved_by_system",
)
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Nothing to change: this only joins the two histories."""


def downgrade() -> None:
    """Nothing to change: this only joins the two histories."""
