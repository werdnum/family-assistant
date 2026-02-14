"""Add is_skill, skill_name, skill_description columns to notes table

Revision ID: add_is_skill_notes
Revises: add_visibility_labels_docs
Create Date: 2026-02-13

"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "add_is_skill_notes"
down_revision: str | None = "add_visibility_labels_docs"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Add skill metadata columns to notes table.

    is_skill: whether this note is a skill (detected from frontmatter at write time)
    skill_name: the skill's display name from frontmatter
    skill_description: the skill's description from frontmatter
    """
    op.add_column(
        "notes",
        sa.Column(
            "is_skill",
            sa.Boolean(),
            nullable=False,
            server_default="false",
        ),
    )
    op.add_column(
        "notes",
        sa.Column("skill_name", sa.String(), nullable=True),
    )
    op.add_column(
        "notes",
        sa.Column("skill_description", sa.String(), nullable=True),
    )


def downgrade() -> None:
    """Remove skill metadata columns from notes table."""
    op.drop_column("notes", "skill_description")
    op.drop_column("notes", "skill_name")
    op.drop_column("notes", "is_skill")
