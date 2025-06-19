"""Add SCRIPT to EventActionType enum

Revision ID: add_script_enum
Revises: d93343aecc37
Create Date: 2025-06-19 17:40:00.000000+10:00

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'add_script_enum'
down_revision: Union[str, None] = 'd93343aecc37'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Add SCRIPT to EventActionType enum."""
    # For PostgreSQL, we need to alter the enum type
    connection = op.get_bind()
    if connection.dialect.name == 'postgresql':
        # Add the new value to the enum type
        op.execute("ALTER TYPE eventactiontype ADD VALUE IF NOT EXISTS 'script'")
    # For SQLite, enum constraints are not enforced, so no action needed


def downgrade() -> None:
    """Remove SCRIPT from EventActionType enum."""
    # Note: PostgreSQL doesn't support removing values from enums easily
    # This would require creating a new enum type without 'script' and migrating the data
    # For now, we'll leave this as a no-op since removing enum values is complex
    pass