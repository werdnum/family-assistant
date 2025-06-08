"""add_include_in_prompt_to_notes

Revision ID: dda18702319b
Revises: 717b6a577405
Create Date: 2025-06-08 09:51:01.111477+10:00

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'dda18702319b'
down_revision: Union[str, None] = '717b6a577405'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    # Add the new column with a server default
    op.add_column('notes', sa.Column('include_in_prompt', sa.Boolean(), server_default='true', nullable=False))
    
    # Explicitly set all existing notes to include_in_prompt=true to preserve current behavior
    # This ensures that even if the server_default doesn't apply retroactively on some databases,
    # all existing notes will still be included in prompts
    connection = op.get_bind()
    connection.execute(sa.text("UPDATE notes SET include_in_prompt = true WHERE include_in_prompt IS NULL"))


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_column('notes', 'include_in_prompt')
