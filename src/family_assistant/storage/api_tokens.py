"""
CRUD operations for API tokens.
"""

import logging
import secrets
import string
from datetime import datetime, timezone

from sqlalchemy import insert

from family_assistant.storage.base import api_tokens_table
from family_assistant.storage.context import DatabaseContext
from family_assistant.web.auth import pwd_context  # For hashing

logger = logging.getLogger(__name__)


# --- Token Generation Helpers ---
TOKEN_PREFIX_LENGTH = 8
TOKEN_SECRET_LENGTH = 32


def _generate_token_part(length: int, alphabet: str) -> str:
    """Generates a random string of a given length from an alphabet."""
    return "".join(secrets.choice(alphabet) for _ in range(length))


def generate_token_prefix() -> str:
    """Generates a unique prefix for an API token."""
    # Using a simpler alphabet for the prefix to make it more memorable/manageable if needed,
    # though its primary purpose is quick DB lookup.
    # Ensure it's sufficiently random to avoid collisions in practice.
    # For true uniqueness guarantee, a DB check would be needed, but for prefixes of
    # this length generated randomly, collisions are highly improbable.
    alphabet = string.ascii_uppercase + string.digits
    return _generate_token_part(TOKEN_PREFIX_LENGTH, alphabet)


def generate_token_secret() -> str:
    """Generates the secret part of an API token."""
    alphabet = (
        string.ascii_letters
        + string.digits
        + string.punctuation.replace('"', "").replace("'", "")
    )  # Avoid quotes
    return _generate_token_part(TOKEN_SECRET_LENGTH, alphabet)


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


async def create_and_store_api_token(
    db_context: DatabaseContext,
    user_identifier: str,
    name: str,
    expires_at: datetime | None = None,
) -> tuple[str, int, datetime]:
    """
    Generates a new API token, stores its hashed version, and returns the full token.

    Args:
        db_context: The database context.
        user_identifier: The identifier of the user this token belongs to.
        name: A user-friendly name for the token.
        expires_at: Optional timestamp when the token should expire.

    Returns:
        A tuple containing:
            - The full, unhashed API token (prefix + secret).
            - The ID of the newly created token in the database.
            - The creation timestamp (UTC).
    """
    prefix = generate_token_prefix()
    secret = generate_token_secret()
    full_token = f"{prefix}{secret}"

    hashed_secret = pwd_context.hash(secret)
    created_at_utc = datetime.now(timezone.utc)

    # Note: The `api_tokens_table` uses `hashed_token` for the column name
    # that stores the hashed version of the *secret part* of the token.
    # The `prefix` is stored separately.
    token_id = await add_api_token(
        db_context=db_context,
        user_identifier=user_identifier,
        name=name,
        hashed_token=hashed_secret,  # This is the hash of the 'secret' part
        prefix=prefix,
        created_at=created_at_utc,
        expires_at=expires_at,
    )

    logger.info(
        "Successfully generated and stored API token ID %s for user %s (Name: %s)",
        token_id,
        user_identifier,
        name,
    )
    return full_token, token_id, created_at_utc
