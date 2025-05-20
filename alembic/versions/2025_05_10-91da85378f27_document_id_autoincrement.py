"""document ID autoincrement

Revision ID: 91da85378f27
Revises: 17e44fab84d2
Create Date: 2025-05-10 23:09:01.559064+10:00

"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "91da85378f27"
down_revision: str | None = "17e44fab84d2"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    with op.batch_alter_table("document_embeddings", schema=None) as batch_op:  # pylint: disable=no-member
        batch_op.alter_column(
            "id",
            existing_type=sa.BIGINT(),
            type_=sa.Integer(),
            existing_nullable=False,
            autoincrement=True,
        )
        batch_op.alter_column(
            "document_id",
            existing_type=sa.BIGINT(),
            type_=sa.Integer(),
            existing_nullable=False,
        )

    with op.batch_alter_table("documents", schema=None) as batch_op:  # pylint: disable=no-member
        batch_op.alter_column(
            "id",
            existing_type=sa.BIGINT(),
            type_=sa.Integer(),
            existing_nullable=False,
            autoincrement=True,
        )
    # ### end Alembic commands ###


def downgrade() -> None:
    """Downgrade schema."""
    with op.batch_alter_table("documents", schema=None) as batch_op:  # pylint: disable=no-member
        batch_op.alter_column(
            "id",
            existing_type=sa.Integer(),
            type_=sa.BIGINT(),
            existing_nullable=False,
            autoincrement=True,
        )

    with op.batch_alter_table("document_embeddings", schema=None) as batch_op:  # pylint: disable=no-member
        batch_op.alter_column(
            "document_id",
            existing_type=sa.Integer(),
            type_=sa.BIGINT(),
            existing_nullable=False,
        )
        batch_op.alter_column(
            "id",
            existing_type=sa.Integer(),
            type_=sa.BIGINT(),
            existing_nullable=False,
            autoincrement=True,
        )
    # ### end Alembic commands ###
