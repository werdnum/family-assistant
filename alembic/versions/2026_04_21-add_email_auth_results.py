"""Add DKIM/SPF/DMARC verification results to received_emails.

Revision ID: add_email_auth_results
Revises: add_confirmation_requests
Create Date: 2026-04-21

"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "add_email_auth_results"
down_revision: str | None = "add_confirmation_requests"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "received_emails",
        sa.Column("dkim_result", sa.Text(), nullable=True),
    )
    op.add_column(
        "received_emails",
        sa.Column("spf_result", sa.Text(), nullable=True),
    )
    op.add_column(
        "received_emails",
        sa.Column("dmarc_result", sa.Text(), nullable=True),
    )
    op.add_column(
        "received_emails",
        sa.Column("dmarc_policy", sa.Text(), nullable=True),
    )
    op.add_column(
        "received_emails",
        sa.Column("dkim_domain", sa.Text(), nullable=True),
    )
    op.create_index(
        op.f("ix_received_emails_dmarc_result"),
        "received_emails",
        ["dmarc_result"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(
        op.f("ix_received_emails_dmarc_result"),
        table_name="received_emails",
    )
    op.drop_column("received_emails", "dkim_domain")
    op.drop_column("received_emails", "dmarc_policy")
    op.drop_column("received_emails", "dmarc_result")
    op.drop_column("received_emails", "spf_result")
    op.drop_column("received_emails", "dkim_result")
