"""Repository for managing iOS APNs device token storage."""

import logging

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as postgresql_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
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
        # Atomic upsert keyed on the device_token unique index. A database-level ON CONFLICT
        # avoids a select-then-insert race in which two concurrent registrations of the same
        # token both insert and one fails the unique constraint.
        insert = (
            postgresql_insert
            if self._db.dialect_name == "postgresql"
            else sqlite_insert
        )
        stmt = insert(ios_push_tokens_table).values(
            user_identifier=user_identifier,
            device_token=device_token,
            environment=environment,
            bundle_id=bundle_id,
        )
        stmt = stmt.on_conflict_do_update(
            index_elements=[ios_push_tokens_table.c.device_token],
            set_={
                "user_identifier": user_identifier,
                "environment": environment,
                "bundle_id": bundle_id,
                "updated_at": func.now(),
            },
        )
        await self._db.execute(stmt)

        row = await self._db.fetch_one(
            select(ios_push_tokens_table.c.id).where(
                ios_push_tokens_table.c.device_token == device_token
            )
        )
        if row is None:  # pragma: no cover - row always exists after upsert
            raise RuntimeError("iOS push token row missing after upsert")
        return row["id"]

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
        result = await self._db.execute(stmt)
        return result.rowcount

    async def delete_by_token(self, device_token: str) -> int:
        """Delete a device token regardless of owner (used for APNs cleanup).

        Returns:
            Number of rows deleted.
        """
        stmt = ios_push_tokens_table.delete().where(
            ios_push_tokens_table.c.device_token == device_token
        )
        result = await self._db.execute(stmt)
        return result.rowcount

    async def update_environment(self, device_token: str, environment: str) -> None:
        """Persist a corrected APNs environment for a device token."""
        stmt = (
            ios_push_tokens_table
            .update()
            .where(ios_push_tokens_table.c.device_token == device_token)
            .values(environment=environment, updated_at=func.now())
        )
        await self._db.execute(stmt)
