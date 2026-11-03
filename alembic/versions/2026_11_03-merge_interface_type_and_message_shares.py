"""Merge the two heads that branched from add_thread_start_marker.

Revision ID: merge_iface_type_msg_shares
Revises: interface_type_to_string, add_conversation_message_shares
"""

from collections.abc import Sequence

revision: str = "merge_iface_type_msg_shares"
down_revision: tuple[str, ...] = (
    "interface_type_to_string",
    "add_conversation_message_shares",
)
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
