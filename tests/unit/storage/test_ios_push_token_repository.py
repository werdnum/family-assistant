"""Unit tests for IosPushTokenRepository."""

from collections.abc import AsyncGenerator

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncEngine

from family_assistant.storage.context import DatabaseContext


@pytest_asyncio.fixture(scope="function")
async def db_context(db_engine: AsyncEngine) -> AsyncGenerator[DatabaseContext]:
    """Provides an entered DatabaseContext for repository tests."""
    async with DatabaseContext(engine=db_engine) as db_ctx:
        yield db_ctx


class TestIosPushTokenRepository:
    """Tests for IosPushTokenRepository operations."""

    @pytest.mark.asyncio
    async def test_upsert_and_get_by_user(self, db_context: DatabaseContext) -> None:
        """A registered token can be retrieved for its owner."""
        token_id = await db_context.ios_push_tokens.upsert(
            user_identifier="user-1",
            device_token="abc123",
            environment="sandbox",
            bundle_id="com.example.app",
        )
        assert token_id is not None

        tokens = await db_context.ios_push_tokens.get_by_user("user-1")
        assert len(tokens) == 1
        assert tokens[0].device_token == "abc123"
        assert tokens[0].environment == "sandbox"
        assert tokens[0].bundle_id == "com.example.app"

    @pytest.mark.asyncio
    async def test_upsert_is_idempotent_on_token(
        self, db_context: DatabaseContext
    ) -> None:
        """Re-registering the same token updates it instead of duplicating."""
        await db_context.ios_push_tokens.upsert(
            user_identifier="user-1",
            device_token="abc123",
            environment="sandbox",
        )
        await db_context.ios_push_tokens.upsert(
            user_identifier="user-2",
            device_token="abc123",
            environment="production",
            bundle_id="com.example.app",
        )

        # Token moved to the new owner; old owner has none.
        assert await db_context.ios_push_tokens.get_by_user("user-1") == []
        tokens = await db_context.ios_push_tokens.get_by_user("user-2")
        assert len(tokens) == 1
        assert tokens[0].environment == "production"
        assert tokens[0].bundle_id == "com.example.app"

    @pytest.mark.asyncio
    async def test_delete_for_user_scoped_to_owner(
        self, db_context: DatabaseContext
    ) -> None:
        """A user can only delete their own token."""
        await db_context.ios_push_tokens.upsert(
            user_identifier="user-1",
            device_token="abc123",
            environment="production",
        )

        # Wrong user cannot delete it.
        assert await db_context.ios_push_tokens.delete_for_user("user-2", "abc123") == 0
        # Owner can.
        assert await db_context.ios_push_tokens.delete_for_user("user-1", "abc123") == 1
        assert await db_context.ios_push_tokens.get_by_user("user-1") == []

    @pytest.mark.asyncio
    async def test_delete_by_token_and_update_environment(
        self, db_context: DatabaseContext
    ) -> None:
        """APNs cleanup helpers operate by token regardless of owner."""
        await db_context.ios_push_tokens.upsert(
            user_identifier="user-1",
            device_token="abc123",
            environment="production",
        )

        await db_context.ios_push_tokens.update_environment("abc123", "sandbox")
        tokens = await db_context.ios_push_tokens.get_by_user("user-1")
        assert tokens[0].environment == "sandbox"

        assert await db_context.ios_push_tokens.delete_by_token("abc123") == 1
        assert await db_context.ios_push_tokens.get_by_user("user-1") == []
