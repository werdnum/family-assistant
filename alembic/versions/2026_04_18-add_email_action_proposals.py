"""Add email action proposals.

Revision ID: add_email_action_proposals
Revises: add_email_target_user
Create Date: 2026-04-18

"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "add_email_action_proposals"
down_revision: str | None = "add_email_target_user"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "email_action_proposals",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("email_id", sa.BigInteger(), nullable=False),
        sa.Column("message_id_header", sa.Text(), nullable=False),
        sa.Column("target_user_id", sa.Text(), nullable=False),
        sa.Column("action_type", sa.String(length=64), nullable=False),
        sa.Column(
            "status", sa.String(length=32), server_default="proposed", nullable=False
        ),
        sa.Column("title", sa.Text(), nullable=False),
        sa.Column("rationale", sa.Text(), nullable=True),
        sa.Column("confidence", sa.Float(), nullable=True),
        sa.Column(
            "proposal_json",
            sa.JSON().with_variant(
                postgresql.JSONB(astext_type=sa.Text()), "postgresql"
            ),
            nullable=False,
        ),
        sa.Column(
            "safety_warnings",
            sa.JSON().with_variant(
                postgresql.JSONB(astext_type=sa.Text()), "postgresql"
            ),
            nullable=True,
        ),
        sa.Column("planning_task_id", sa.String(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["email_id"], ["received_emails.id"], ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        op.f("ix_email_action_proposals_action_type"),
        "email_action_proposals",
        ["action_type"],
        unique=False,
    )
    op.create_index(
        op.f("ix_email_action_proposals_created_at"),
        "email_action_proposals",
        ["created_at"],
        unique=False,
    )
    op.create_index(
        op.f("ix_email_action_proposals_email_id"),
        "email_action_proposals",
        ["email_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_email_action_proposals_message_id_header"),
        "email_action_proposals",
        ["message_id_header"],
        unique=False,
    )
    op.create_index(
        op.f("ix_email_action_proposals_planning_task_id"),
        "email_action_proposals",
        ["planning_task_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_email_action_proposals_status"),
        "email_action_proposals",
        ["status"],
        unique=False,
    )
    op.create_index(
        op.f("ix_email_action_proposals_target_user_id"),
        "email_action_proposals",
        ["target_user_id"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(
        op.f("ix_email_action_proposals_target_user_id"),
        table_name="email_action_proposals",
    )
    op.drop_index(
        op.f("ix_email_action_proposals_status"),
        table_name="email_action_proposals",
    )
    op.drop_index(
        op.f("ix_email_action_proposals_planning_task_id"),
        table_name="email_action_proposals",
    )
    op.drop_index(
        op.f("ix_email_action_proposals_message_id_header"),
        table_name="email_action_proposals",
    )
    op.drop_index(
        op.f("ix_email_action_proposals_email_id"),
        table_name="email_action_proposals",
    )
    op.drop_index(
        op.f("ix_email_action_proposals_created_at"),
        table_name="email_action_proposals",
    )
    op.drop_index(
        op.f("ix_email_action_proposals_action_type"),
        table_name="email_action_proposals",
    )
    op.drop_table("email_action_proposals")
