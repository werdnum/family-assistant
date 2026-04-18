"""Add target user id to received emails.

Revision ID: add_email_target_user
Revises: add_scripts_table
Create Date: 2026-04-18

"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "add_email_target_user"
down_revision: str | None = "add_scripts_table"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "received_emails",
        sa.Column("target_user_id", sa.Text(), nullable=True),
    )
    op.create_index(
        op.f("ix_received_emails_target_user_id"),
        "received_emails",
        ["target_user_id"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(
        op.f("ix_received_emails_target_user_id"), table_name="received_emails"
    )
    op.drop_column("received_emails", "target_user_id")
