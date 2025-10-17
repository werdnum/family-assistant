"""Repository for managing push subscription storage."""

import logging
from typing import Any

from sqlalchemy import select

from family_assistant.storage.push_subscription import push_subscriptions_table

from .base import BaseRepository

logger = logging.getLogger(__name__)


class PushSubscriptionRepository(BaseRepository):
    """Repository for managing push subscriptions."""

    async def add(self, user_identifier: str, subscription_json: dict[str, Any]) -> int:
        """Add a new push subscription.

        Args:
            user_identifier: The user identifier
            subscription_json: The subscription data as a dictionary

        Returns:
            The ID of the created subscription
        """
        stmt = push_subscriptions_table.insert().values(
            user_identifier=user_identifier,
            subscription_json=subscription_json,
        )
        result = await self._db.execute_with_retry(stmt)
        # Use inserted_primary_key for PostgreSQL compatibility, fallback to lastrowid for SQLite
        if hasattr(result, "inserted_primary_key") and result.inserted_primary_key:
            return result.inserted_primary_key[0]  # type: ignore[return-value]
        return result.lastrowid  # type: ignore[attr-defined]

    async def get_by_user(self, user_identifier: str) -> list[dict[str, Any]]:
        """Get all push subscriptions for a user.

        Args:
            user_identifier: The user identifier

        Returns:
            List of subscription dictionaries
        """
        query = select(push_subscriptions_table).where(
            push_subscriptions_table.c.user_identifier == user_identifier
        )
        rows = await self._db.fetch_all(query)
        return [dict(row) for row in rows]

    async def delete(self, subscription_id: int) -> int:
        """Delete a push subscription by ID.

        Args:
            subscription_id: The subscription ID to delete

        Returns:
            Number of rows deleted
        """
        stmt = push_subscriptions_table.delete().where(
            push_subscriptions_table.c.id == subscription_id
        )
        result = await self._db.execute_with_retry(stmt)
        return result.rowcount  # type: ignore[attr-defined]

    async def delete_by_endpoint(self, user_identifier: str, endpoint: str) -> int:
        """Delete push subscriptions by endpoint URL for a specific user.

        Args:
            user_identifier: The user identifier
            endpoint: The subscription endpoint URL

        Returns:
            Number of rows deleted
        """
        stmt = push_subscriptions_table.delete().where(
            (push_subscriptions_table.c.user_identifier == user_identifier)
            & (
                push_subscriptions_table.c.subscription_json["endpoint"].as_string()
                == endpoint
            )
        )
        result = await self._db.execute_with_retry(stmt)
        return result.rowcount  # type: ignore[attr-defined]
