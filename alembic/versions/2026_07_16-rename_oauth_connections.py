"""Rename Google connection tables to provider-neutral OAuth names

Revision ID: rename_oauth_connections
Revises: attachment_owner_user_id
Create Date: 2026-07-16 00:00:00.000000+00:00

"""

from collections.abc import Sequence

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "rename_oauth_connections"
down_revision: str | None = "attachment_owner_user_id"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    op.rename_table("user_google_connections", "user_oauth_connections")
    op.rename_table("pending_google_oauth_flows", "pending_oauth_flows")

    op.drop_index(
        "ix_pending_google_oauth_flows_created_at",
        "pending_oauth_flows",
    )
    op.create_index(
        "ix_pending_oauth_flows_created_at",
        "pending_oauth_flows",
        ["created_at"],
    )

    # Batch mode renames the unique constraint on both engines: PostgreSQL gets
    # plain ALTER TABLE DROP/ADD CONSTRAINT, SQLite a table recreation (inline
    # constraint names cannot be altered in place there).
    with op.batch_alter_table("user_oauth_connections") as batch_op:
        batch_op.drop_constraint(
            "uq_user_google_connections_user_provider", type_="unique"
        )
        batch_op.create_unique_constraint(
            "uq_user_oauth_connections_user_provider", ["user_id", "provider"]
        )


def downgrade() -> None:
    """Downgrade schema."""
    with op.batch_alter_table("user_oauth_connections") as batch_op:
        batch_op.drop_constraint(
            "uq_user_oauth_connections_user_provider", type_="unique"
        )
        batch_op.create_unique_constraint(
            "uq_user_google_connections_user_provider", ["user_id", "provider"]
        )

    op.drop_index(
        "ix_pending_oauth_flows_created_at",
        "pending_oauth_flows",
    )
    op.create_index(
        "ix_pending_google_oauth_flows_created_at",
        "pending_oauth_flows",
        ["created_at"],
    )

    op.rename_table("pending_oauth_flows", "pending_google_oauth_flows")
    op.rename_table("user_oauth_connections", "user_google_connections")
