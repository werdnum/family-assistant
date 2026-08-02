"""
CRUD operations for API tokens.
"""

import asyncio
import logging
import secrets
import string
from datetime import UTC, datetime
from typing import NamedTuple

from sqlalchemy import insert, select, update  # Added select and update

from family_assistant.storage.base import api_tokens_table
from family_assistant.storage.database import DatabaseExecutor, DatabaseTransaction
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
    db_context: DatabaseExecutor,
    user_identifier: str,
    name: str,
    hashed_token: str,
    prefix: str,
    created_at: datetime,
    expires_at: datetime | None = None,
    token_type: str = "api",
    parent_token_id: int | None = None,
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
            is_revoked=False,
            token_type=token_type,
            parent_token_id=parent_token_id,
        )
        .returning(api_tokens_table.c.id)
    )
    result = await db_context.execute(query)
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


class MintedApiToken(NamedTuple):
    """Token material generated before any database work.

    Hashing is deliberately separable: bcrypt is blocking and must not run
    inside a transaction, where on SQLite it would hold the engine-wide
    transaction lock for its whole duration.
    """

    full_token: str
    hashed_secret: str
    prefix: str
    created_at: datetime


def mint_api_token() -> MintedApiToken:
    """Generate a new API token and hash its secret. Does no database work."""
    prefix = generate_token_prefix()
    secret = generate_token_secret()
    return MintedApiToken(
        full_token=f"{prefix}{secret}",
        hashed_secret=pwd_context.hash(secret),
        prefix=prefix,
        created_at=datetime.now(UTC),
    )


async def create_and_store_api_token(
    db_context: DatabaseExecutor,
    user_identifier: str,
    name: str,
    expires_at: datetime | None = None,
    token_type: str = "api",
    parent_token_id: int | None = None,
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
    minted = mint_api_token()
    full_token = minted.full_token
    created_at_utc = minted.created_at

    # Note: The `api_tokens_table` uses `hashed_token` for the column name
    # that stores the hashed version of the *secret part* of the token.
    # The `prefix` is stored separately.
    token_id = await add_api_token(
        db_context=db_context,
        user_identifier=user_identifier,
        name=name,
        hashed_token=minted.hashed_secret,
        prefix=minted.prefix,
        created_at=created_at_utc,
        expires_at=expires_at,
        token_type=token_type,
        parent_token_id=parent_token_id,
    )

    logger.info(
        "Successfully generated and stored API token ID %s for user %s (Name: %s)",
        token_id,
        user_identifier,
        name,
    )
    return full_token, token_id, created_at_utc


async def get_api_tokens_for_user(
    db_context: DatabaseExecutor, user_identifier: str
) -> list[dict]:
    """
    Retrieves all API tokens for a given user.
    The hashed_token field is excluded for security.

    Args:
        db_context: The database context.
        user_identifier: The identifier of the user.

    Returns:
        A list of dictionaries, where each dictionary represents an API token.
    """
    query = (
        select(
            api_tokens_table.c.id,
            api_tokens_table.c.name,
            api_tokens_table.c.prefix,
            api_tokens_table.c.user_identifier,
            api_tokens_table.c.created_at,
            api_tokens_table.c.expires_at,
            api_tokens_table.c.last_used_at,
            api_tokens_table.c.is_revoked,
        )
        .where(
            api_tokens_table.c.user_identifier == user_identifier,
            api_tokens_table.c.token_type == "api",
        )
        .order_by(api_tokens_table.c.created_at.desc())
    )
    results = await db_context.fetch_all(query)
    return [dict(row) for row in results]


async def get_api_token_by_id_and_user(
    db_context: DatabaseExecutor, token_id: int, user_identifier: str
) -> dict | None:
    """
    Retrieves a specific API token by its ID, ensuring it belongs to the user.
    The hashed_token field is excluded.

    Args:
        db_context: The database context.
        token_id: The ID of the token to retrieve.
        user_identifier: The identifier of the user who should own the token.

    Returns:
        A dictionary representing the token if found and owned by the user, else None.
    """
    query = select(
        api_tokens_table.c.id,
        api_tokens_table.c.name,
        api_tokens_table.c.prefix,
        api_tokens_table.c.user_identifier,
        api_tokens_table.c.created_at,
        api_tokens_table.c.expires_at,
        api_tokens_table.c.last_used_at,
        api_tokens_table.c.is_revoked,
    ).where(
        api_tokens_table.c.id == token_id,
        api_tokens_table.c.user_identifier == user_identifier,
    )
    row = await db_context.fetch_one(query)
    return dict(row) if row else None


async def revoke_api_token(
    db_context: DatabaseExecutor, token_id: int, user_identifier: str
) -> bool:
    """
    Revokes an API token by setting its is_revoked flag to True.
    Ensures that the token belongs to the user attempting to revoke it.

    Args:
        db_context: The database context.
        token_id: The ID of the token to revoke.
        user_identifier: The identifier of the user attempting the revocation.

    Returns:
        True if the token was successfully revoked, False otherwise (e.g., token
        not found or not owned by the user).
    """
    # First, verify ownership and that the token is not already revoked (optional check)
    token_to_revoke = await get_api_token_by_id_and_user(
        db_context, token_id, user_identifier
    )

    if not token_to_revoke:
        logger.warning(
            "Attempt to revoke non-existent or unauthorized token ID %s by user %s.",
            token_id,
            user_identifier,
        )
        return False

    if token_to_revoke["is_revoked"]:
        logger.info(
            "Token ID %s for user %s is already revoked. No action taken.",
            token_id,
            user_identifier,
        )
        return True  # Considered success as the state is already as desired

    async def _revoke(txn: DatabaseTransaction) -> bool:
        """Revoke the token and its refresh-token children, atomically.

        A failure between the two would leave a revoked token whose refresh
        token can still mint a replacement.
        """
        update_query = (
            update(api_tokens_table)
            .where(
                api_tokens_table.c.id == token_id,
                api_tokens_table.c.user_identifier
                == user_identifier,  # Double check ownership
            )
            .values(
                is_revoked=True, last_used_at=datetime.now(UTC)
            )  # Update last_used_at on revoke
            .returning(api_tokens_table.c.id)  # To check if any row was updated
        )
        result = await txn.execute(update_query)
        updated_id = result.scalar_one_or_none()

        if updated_id is None:
            # This case should ideally be caught by the initial check, but as a safeguard:
            logger.warning(
                "Failed to revoke token ID %s for user %s (possibly due to ownership mismatch or token not found during update).",
                token_id,
                user_identifier,
            )
            return False

        # Cascade: also revoke any child tokens (e.g., refresh tokens linked to this API token)
        child_revoke_query = (
            update(api_tokens_table)
            .where(
                api_tokens_table.c.parent_token_id == token_id,
                api_tokens_table.c.is_revoked == False,  # noqa: E712 - SQLAlchemy requires == for column comparison
            )
            .values(is_revoked=True, last_used_at=datetime.now(UTC))
        )
        await txn.execute(child_revoke_query)

        logger.info(
            "Successfully revoked API token ID %s (and child tokens) for user %s.",
            token_id,
            user_identifier,
        )
        return True

    return await db_context.atomic(_revoke)


async def validate_token_by_value(
    db_context: DatabaseExecutor,
    token_value: str,
    expected_type: str = "refresh",
) -> dict | None:
    """Validate a token (API or refresh) by its raw value (prefix + secret).

    Returns the token row as a dict if valid, None otherwise.
    Does NOT update last_used_at (caller should do that if needed).
    """
    if len(token_value) <= TOKEN_PREFIX_LENGTH:
        return None

    token_prefix = token_value[:TOKEN_PREFIX_LENGTH]
    token_secret = token_value[TOKEN_PREFIX_LENGTH:]

    query = select(api_tokens_table).where(
        api_tokens_table.c.prefix == token_prefix,
        api_tokens_table.c.token_type == expected_type,
    )
    row = await db_context.fetch_one(query)
    if not row:
        return None

    row_dict = dict(row)
    if not await asyncio.to_thread(
        pwd_context.verify, token_secret, row_dict["hashed_token"]
    ):
        return None

    if row_dict["is_revoked"]:
        return None

    now = datetime.now(UTC)
    expires_at = row_dict["expires_at"]
    if expires_at:
        if expires_at.tzinfo is None:
            expires_at = expires_at.replace(tzinfo=UTC)
        if expires_at < now:
            return None

    return row_dict


async def is_token_valid(db_context: DatabaseExecutor, token_id: int) -> bool:
    """Check if a token (by ID) is still valid (not revoked, not expired)."""
    query = select(
        api_tokens_table.c.is_revoked,
        api_tokens_table.c.expires_at,
    ).where(api_tokens_table.c.id == token_id)
    row = await db_context.fetch_one(query)
    if not row:
        return False
    if row["is_revoked"]:
        return False
    now = datetime.now(UTC)
    expires_at = row["expires_at"]
    if expires_at and expires_at.tzinfo is None:
        expires_at = expires_at.replace(tzinfo=UTC)
    return not (expires_at and expires_at < now)
