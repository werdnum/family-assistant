"""OAuth clients registered with the MCP adapter's authorization server.

A client is the SDK's ``OAuthClientInformationFull`` stored whole, so what the
registration endpoint issued is exactly what the token endpoint later checks.
"""

import logging
from datetime import UTC, datetime

from mcp.shared.auth import OAuthClientInformationFull
from sqlalchemy import delete, exists, insert, or_, select
from sqlalchemy.sql import functions as func

from family_assistant.storage.base import api_tokens_table, oauth_clients_table
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


async def count_clients(db_context: DatabaseExecutor) -> int:
    row = await db_context.fetch_one(
        select(func.count(oauth_clients_table.c.client_id).label("count"))
    )
    return int(row["count"]) if row else 0


async def prune_idle_clients(db_context: DatabaseExecutor, keep_at_most: int) -> int:
    """Delete the oldest clients holding no live token until at most ``keep_at_most`` remain.

    A client with an unrevoked, unexpired token is a working connector and is
    never pruned; one with none is a registration that never completed, or a
    grant the user has since revoked or let lapse, and can be re-registered at
    no cost.
    """
    excess = await count_clients(db_context) - keep_at_most
    if excess <= 0:
        return 0
    now = datetime.now(UTC)
    has_live_token = exists().where(
        api_tokens_table.c.oauth_client_id == oauth_clients_table.c.client_id,
        api_tokens_table.c.is_revoked == False,  # noqa: E712 - SQL comparison
        or_(
            api_tokens_table.c.expires_at.is_(None),
            api_tokens_table.c.expires_at > now,
        ),
    )
    idle = await db_context.fetch_all(
        select(oauth_clients_table.c.client_id)
        .where(~has_live_token)
        .order_by(oauth_clients_table.c.created_at)
        .limit(excess)
    )
    idle_ids = [row["client_id"] for row in idle]
    if idle_ids:
        await db_context.execute(
            delete(oauth_clients_table).where(
                oauth_clients_table.c.client_id.in_(idle_ids)
            )
        )
        logger.info("Pruned %d idle OAuth client registrations", len(idle_ids))
    return len(idle_ids)
