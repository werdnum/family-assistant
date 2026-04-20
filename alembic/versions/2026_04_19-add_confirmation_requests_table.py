"""Add confirmation_requests table.

Revision ID: add_confirmation_requests
Revises: add_email_target_user
Create Date: 2026-04-19

"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "add_confirmation_requests"
down_revision: str | None = "add_email_target_user"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "confirmation_requests",
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column("target_user_id", sa.String(length=255), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("tool_name", sa.String(length=255), nullable=False),
        sa.Column(
            "tool_args_json",
            sa.JSON().with_variant(
                postgresql.JSONB(astext_type=sa.Text()), "postgresql"
            ),
            nullable=False,
        ),
        sa.Column("tool_call_id", sa.String(length=255), nullable=True),
        sa.Column("source_message_internal_id", sa.Integer(), nullable=True),
        sa.Column("confirmation_prompt", sa.Text(), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("resolved_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("resolved_by_user_id", sa.String(length=255), nullable=True),
        sa.Column("resolved_via_interface", sa.String(length=50), nullable=True),
        sa.Column("execution_task_id", sa.String(length=255), nullable=True),
        sa.ForeignKeyConstraint(
            ["source_message_internal_id"],
            ["message_history.internal_id"],
            ondelete="SET NULL",
        ),
        sa.CheckConstraint(
            "status IN ('pending', 'approved', 'rejected', 'expired')",
            name="ck_confirmation_requests_status",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("execution_task_id"),
    )
    op.create_index(
        op.f("ix_confirmation_requests_target_user_id"),
        "confirmation_requests",
        ["target_user_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_confirmation_requests_status"),
        "confirmation_requests",
        ["status"],
        unique=False,
    )
    op.create_index(
        op.f("ix_confirmation_requests_source_message_internal_id"),
        "confirmation_requests",
        ["source_message_internal_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_confirmation_requests_expires_at"),
        "confirmation_requests",
        ["expires_at"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(
        op.f("ix_confirmation_requests_expires_at"),
        table_name="confirmation_requests",
    )
    op.drop_index(
        op.f("ix_confirmation_requests_source_message_internal_id"),
        table_name="confirmation_requests",
    )
    op.drop_index(
        op.f("ix_confirmation_requests_status"),
        table_name="confirmation_requests",
    )
    op.drop_index(
        op.f("ix_confirmation_requests_target_user_id"),
        table_name="confirmation_requests",
    )
    op.drop_table("confirmation_requests")
