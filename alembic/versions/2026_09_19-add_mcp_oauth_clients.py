"""Add OAuth client storage for the MCP adapter's authorization server.

Revision ID: add_mcp_oauth_clients
Revises: add_memory_review_state
Create Date: 2026-09-19

Clients that register dynamically (RFC 7591) are persisted so a restart does
not invalidate a connector, and an access token remembers the client it was
issued to so the token endpoint can refuse another client's refresh token.

See ``docs/design/mcp-adapter.md``.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "add_mcp_oauth_clients"
down_revision: str | None = "add_memory_review_state"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "oauth_clients",
        sa.Column("client_id", sa.String(length=64), nullable=False),
        sa.Column(
            "client_metadata",
            sa.JSON().with_variant(
                postgresql.JSONB(astext_type=sa.Text()), "postgresql"
            ),
            nullable=False,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),  # pylint: disable=not-callable
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("client_id"),
    )
    op.add_column(
        "api_tokens",
        sa.Column("oauth_client_id", sa.String(length=64), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("api_tokens", "oauth_client_id")
    op.drop_table("oauth_clients")
