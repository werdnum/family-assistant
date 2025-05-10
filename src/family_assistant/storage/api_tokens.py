"""
CRUD operations for API tokens.
"""

import logging
from datetime import datetime

from sqlalchemy import insert

from family_assistant.storage.base import api_tokens_table
from family_assistant.storage.context import DatabaseContext

logger = logging.getLogger(__name__)


async def add_api_token(
    db_context: DatabaseContext,
    user_identifier: str,
    name: str,
    hashed_token: str,
    prefix: str,
    created_at: datetime,
    expires_at: datetime | None = None,
) -> int:
    """
    Adds a new API token to the database.

    Args:
        db_context: The database context for executing queries.
        user_identifier: The identifier of the user this token belongs to.
        name: A user-friendly name for the token.
        hashed_token: The securely hashed token secret.
        prefix: The token prefix for identification.
        created_at: The timestamp when the token was created.
        expires_at: Optional timestamp when the token should expire.

    Returns:
        The ID of the newly created API token.

    Raises:
        Various SQLAlchemy exceptions on database errors.
    """
    query = (
        insert(api_tokens_table)
        .values(
            user_identifier=user_identifier,
            name=name,
            hashed_token=hashed_token,
            prefix=prefix,
            created_at=created_at,
            expires_at=expires_at,
            is_revoked=False,  # New tokens are not revoked by default
        )
        .returning(api_tokens_table.c.id)
    )
    result = await db_context.execute_with_retry(query)
    new_token_id = result.scalar_one_or_none()

    if new_token_id is None:
        # This case should ideally not be reached if execute_with_retry works as expected
        # and the database operation is successful.
        logger.error(
            "Failed to retrieve ID for newly inserted API token for user %s, name %s.",
            user_identifier,
            name,
        )
        raise RuntimeError("Failed to retrieve ID for newly inserted API token.")

    logger.info(
        "Added API token with ID %s for user %s (Name: %s, Prefix: %s)",
        new_token_id,
        user_identifier,
        name,
        prefix,
    )
    return new_token_id
