"""merge heads

Revision ID: 5c039369c3d7
Revises: add_script_enum, add_condition_script
Create Date: 2025-07-22 23:25:59.854116+10:00

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '5c039369c3d7'
down_revision: Union[str, None] = ('add_script_enum', 'add_condition_script')
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    pass


def downgrade() -> None:
    """Downgrade schema."""
    pass
