"""OAuth clients registered with the MCP adapter's authorization server.

A client is the SDK's ``OAuthClientInformationFull`` stored whole, so what the
registration endpoint issued is exactly what the token endpoint later checks.
"""

import logging
from datetime import UTC, datetime

from mcp.shared.auth import OAuthClientInformationFull
from sqlalchemy import insert, select

from family_assistant.storage.base import oauth_clients_table
from family_assistant.storage.database import DatabaseExecutor

logger = logging.getLogger(__name__)


async def add_client(
    db_context: DatabaseExecutor, client: OAuthClientInformationFull
) -> None:
    """Persist a newly registered client."""
    await db_context.execute(
        insert(oauth_clients_table).values(
            client_id=client.client_id,
            client_metadata=client.model_dump(mode="json"),
            created_at=datetime.now(UTC),
        )
    )
    logger.info("Registered OAuth client %s (%s)", client.client_id, client.client_name)


async def get_client(
    db_context: DatabaseExecutor, client_id: str
) -> OAuthClientInformationFull | None:
    """Look a registered client up by id."""
    row = await db_context.fetch_one(
        select(oauth_clients_table.c.client_metadata).where(
            oauth_clients_table.c.client_id == client_id
        )
    )
    if row is None:
        return None
    return OAuthClientInformationFull.model_validate(row["client_metadata"])
