"""Delete the second copies of scheduled-callback replies in web conversations.

Callback delivery used to send an already-saved reply through
``WebChatInterface.send_message``, which saved it again. The canonical row was
then stamped with the copy's id as its ``interface_message_id``, which is what
identifies the copy here.

Revision ID: delete_dup_web_callback_replies
Revises: add_conversation_summaries
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "delete_dup_web_callback_replies"
down_revision: str | None = "add_conversation_summaries"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _history() -> sa.TableClause:
    return sa.table(
        "message_history",
        sa.column("internal_id", sa.Integer),
        sa.column("interface_type", sa.String),
        sa.column("conversation_id", sa.String),
        sa.column("interface_message_id", sa.String),
        sa.column("turn_id", sa.String),
        sa.column("thread_root_id", sa.Integer),
        sa.column("role", sa.String),
        sa.column("content", sa.Text),
    )


attachment_metadata = sa.table(
    "attachment_metadata", sa.column("message_id", sa.Integer)
)


def upgrade() -> None:
    """Delete each copy whose canonical row points at it with the same content.

    Copies something else still references are left alone.
    """
    copy = _history().alias("copy")
    canonical = _history().alias("canonical")
    other = _history().alias("other")

    duplicate_ids = sa.select(copy.c.internal_id).where(
        copy.c.role == "assistant",
        copy.c.interface_type.in_(("web", "mcp")),
        copy.c.turn_id.is_(None),
        copy.c.interface_message_id.is_(None),
        sa.exists().where(
            canonical.c.role == "assistant",
            canonical.c.interface_type == copy.c.interface_type,
            canonical.c.conversation_id == copy.c.conversation_id,
            canonical.c.internal_id < copy.c.internal_id,
            canonical.c.interface_message_id == sa.cast(copy.c.internal_id, sa.String),
            sa.func.coalesce(canonical.c.content, "")
            == sa.func.coalesce(copy.c.content, ""),
        ),
        ~sa.exists().where(attachment_metadata.c.message_id == copy.c.internal_id),
        ~sa.exists().where(other.c.thread_root_id == copy.c.internal_id),
    )
    history = _history()
    op.execute(sa.delete(history).where(history.c.internal_id.in_(duplicate_ids)))


def downgrade() -> None:
    """Deleted copies are not restored; the canonical rows hold the same reply."""
