"""Add token_type and parent_token_id columns to api_tokens table.

Supports refresh tokens for iOS app auth flow by storing them
alongside API tokens with a type discriminator.

Revision ID: add_token_type_parent
Revises: add_a2a_tasks
Create Date: 2026-03-29

"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "add_token_type_parent"
down_revision: str | None = "add_a2a_tasks"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("api_tokens") as batch_op:
        batch_op.add_column(
            sa.Column(
                "token_type",
                sa.String(length=16),
                nullable=False,
                server_default="api",
            ),
        )
        batch_op.add_column(
            sa.Column("parent_token_id", sa.Integer(), nullable=True),
        )
        batch_op.create_foreign_key(
            "fk_api_tokens_parent_token_id",
            "api_tokens",
            ["parent_token_id"],
            ["id"],
        )


def downgrade() -> None:
    with op.batch_alter_table("api_tokens") as batch_op:
        batch_op.drop_constraint("fk_api_tokens_parent_token_id", type_="foreignkey")
        batch_op.drop_column("parent_token_id")
        batch_op.drop_column("token_type")
