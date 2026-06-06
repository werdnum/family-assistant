"""Repository for managing iOS APNs device token storage."""

import logging

from sqlalchemy import select
from sqlalchemy.sql import functions as func

from family_assistant.storage.ios_push_token import (
    IosPushToken,
    ios_push_tokens_table,
)

from .base import BaseRepository

logger = logging.getLogger(__name__)


class IosPushTokenRepository(BaseRepository):
    """Repository for managing iOS APNs device tokens."""

    async def upsert(
        self,
        *,
        user_identifier: str,
        device_token: str,
        environment: str,
        bundle_id: str | None = None,
    ) -> int:
        """Register a device token, or refresh it if already present.

        A device token is unique to a device/app install, so this is keyed on `device_token`:
        re-registering moves the token to the current user and refreshes the environment and
        bundle id.

        Returns:
            The ID of the created or updated token row.
        """
        existing = await self._db.fetch_one(
            select(ios_push_tokens_table.c.id).where(
                ios_push_tokens_table.c.device_token == device_token
            )
        )
        if existing is not None:
            stmt = (
                ios_push_tokens_table
                .update()
                .where(ios_push_tokens_table.c.device_token == device_token)
                .values(
                    user_identifier=user_identifier,
                    environment=environment,
                    bundle_id=bundle_id,
                    updated_at=func.now(),
                )
            )
            await self._db.execute_with_retry(stmt)
            return existing["id"]

        stmt = ios_push_tokens_table.insert().values(
            user_identifier=user_identifier,
            device_token=device_token,
            environment=environment,
            bundle_id=bundle_id,
        )
        result = await self._db.execute_with_retry(stmt)
        if hasattr(result, "inserted_primary_key") and result.inserted_primary_key:
            return result.inserted_primary_key[0]  # type: ignore[return-value]
        return result.lastrowid  # type: ignore[attr-defined]

    async def get_by_user(self, user_identifier: str) -> list[IosPushToken]:
        """Get all iOS device tokens for a user."""
        query = select(ios_push_tokens_table).where(
            ios_push_tokens_table.c.user_identifier == user_identifier
        )
        rows = await self._db.fetch_all(query)
        return [IosPushToken.model_validate(row) for row in rows]

    async def delete_for_user(self, user_identifier: str, device_token: str) -> int:
        """Delete a device token belonging to a specific user.

        Returns:
            Number of rows deleted.
        """
        stmt = ios_push_tokens_table.delete().where(
            (ios_push_tokens_table.c.user_identifier == user_identifier)
            & (ios_push_tokens_table.c.device_token == device_token)
        )
        result = await self._db.execute_with_retry(stmt)
        return result.rowcount  # type: ignore[attr-defined]

    async def delete_by_token(self, device_token: str) -> int:
        """Delete a device token regardless of owner (used for APNs cleanup).

        Returns:
            Number of rows deleted.
        """
        stmt = ios_push_tokens_table.delete().where(
            ios_push_tokens_table.c.device_token == device_token
        )
        result = await self._db.execute_with_retry(stmt)
        return result.rowcount  # type: ignore[attr-defined]

    async def update_environment(self, device_token: str, environment: str) -> None:
        """Persist a corrected APNs environment for a device token."""
        stmt = (
            ios_push_tokens_table
            .update()
            .where(ios_push_tokens_table.c.device_token == device_token)
            .values(environment=environment, updated_at=func.now())
        )
        await self._db.execute_with_retry(stmt)
