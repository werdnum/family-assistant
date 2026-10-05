"""Record whether a confirmation was resolved by the system rather than a human.

Revision ID: add_confirm_resolved_by_system
Revises: add_message_activated_tools
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "add_confirm_resolved_by_system"
down_revision: str | None = "add_message_activated_tools"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Separate cleanup rejections from human decisions, and index call ids.

    The tool-call reviewer reads a conversation's human confirmation decisions
    by the tool call ids in its history, and a rejection the system made when a
    turn was stopped or a prompt could not be delivered is not a decision.
    """
    op.add_column(
        "confirmation_requests",
        sa.Column(
            "resolved_by_system",
            sa.Boolean(),
            nullable=False,
            server_default=sa.false(),
        ),
    )
    op.create_index(
        "ix_confirmation_requests_tool_call_id",
        "confirmation_requests",
        ["tool_call_id"],
    )


def downgrade() -> None:
    """Remove the system-resolution flag and the call id index."""
    op.drop_index(
        "ix_confirmation_requests_tool_call_id",
        table_name="confirmation_requests",
    )
    op.drop_column("confirmation_requests", "resolved_by_system")
