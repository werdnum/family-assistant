"""Unit tests for OAuthConnectionsRepository (SQLite + PostgreSQL)."""

import asyncio
from collections.abc import AsyncGenerator
from datetime import UTC, datetime, timedelta

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncEngine

from family_assistant.storage.database import Database


@pytest_asyncio.fixture(scope="function")
async def db_context(db_engine: AsyncEngine) -> AsyncGenerator[Database]:
    """Provides an entered Database for repository tests."""
    db_ctx = Database(engine=db_engine)
    yield db_ctx


class TestGoogleConnections:
    """Connection round-trip, upsert, and needs_reauth semantics."""

    @pytest.mark.asyncio
    async def test_connection_round_trip_includes_scopes(
        self, db_context: Database
    ) -> None:
        scopes = [
            "https://www.googleapis.com/auth/gmail.readonly",
            "https://www.googleapis.com/auth/drive.readonly",
        ]
        created = await db_context.oauth_connections.upsert_connection(
            user_id="user-a",
            provider="google",
            provider_account_email="a@example.com",
            scopes=scopes,
            refresh_token_encrypted="cipher-a",
        )

        assert created.id is not None
        assert created.status == "active"
        assert created.credential_generation
        assert created.scopes == scopes

        fetched = await db_context.oauth_connections.get_connection("user-a", "google")
        assert fetched is not None
        assert fetched.provider_account_email == "a@example.com"
        assert fetched.scopes == scopes
        assert fetched.refresh_token_encrypted == "cipher-a"
        assert fetched.credential_generation == created.credential_generation

    @pytest.mark.asyncio
    async def test_get_connection_missing_returns_none(
        self, db_context: Database
    ) -> None:
        assert (
            await db_context.oauth_connections.get_connection("nobody", "google")
            is None
        )

    @pytest.mark.asyncio
    async def test_upsert_rotates_credential_generation(
        self, db_context: Database
    ) -> None:
        first = await db_context.oauth_connections.upsert_connection(
            user_id="user-a",
            provider="google",
            provider_account_email="a@example.com",
            scopes=["s1"],
            refresh_token_encrypted="cipher-1",
        )
        second = await db_context.oauth_connections.upsert_connection(
            user_id="user-a",
            provider="google",
            provider_account_email="a2@example.com",
            scopes=["s1", "s2"],
            refresh_token_encrypted="cipher-2",
        )

        assert second.id == first.id  # same row (unique on user_id+provider)
        assert second.credential_generation != first.credential_generation
        assert second.provider_account_email == "a2@example.com"
        assert second.scopes == ["s1", "s2"]
        assert second.refresh_token_encrypted == "cipher-2"
        assert second.status == "active"

    @pytest.mark.asyncio
    async def test_upsert_preserves_created_at_on_update(
        self, db_context: Database
    ) -> None:
        """The atomic upsert keeps ``created_at`` while bumping ``updated_at``."""
        first = await db_context.oauth_connections.upsert_connection(
            user_id="user-a",
            provider="google",
            provider_account_email="a@example.com",
            scopes=["s1"],
            refresh_token_encrypted="cipher-1",
        )
        second = await db_context.oauth_connections.upsert_connection(
            user_id="user-a",
            provider="google",
            provider_account_email="a2@example.com",
            scopes=["s1"],
            refresh_token_encrypted="cipher-2",
        )

        assert second.id == first.id
        assert second.created_at == first.created_at
        assert second.updated_at >= first.updated_at

    @pytest.mark.asyncio
    async def test_concurrent_upserts_yield_single_active_row(
        self, db_engine: AsyncEngine
    ) -> None:
        """Two concurrent same-user upserts converge on one active connection.

        Each callback runs in its own :class:`Database` (a separate
        transaction, mirroring two concurrent OAuth callbacks). The dialect-native
        ``INSERT ... ON CONFLICT DO UPDATE`` means neither raises on the unique
        constraint — the second becomes an update — so exactly one row survives.
        """

        async def _upsert(email: str, cipher: str) -> None:
            ctx = Database(engine=db_engine)
            await ctx.oauth_connections.upsert_connection(
                user_id="user-a",
                provider="google",
                provider_account_email=email,
                scopes=["s1"],
                refresh_token_encrypted=cipher,
            )

        await asyncio.gather(
            _upsert("a1@example.com", "cipher-1"),
            _upsert("a2@example.com", "cipher-2"),
        )

        ctx = Database(engine=db_engine)
        connections = await ctx.oauth_connections.list_connections()
        user_a = [c for c in connections if c.user_id == "user-a"]
        assert len(user_a) == 1
        assert user_a[0].status == "active"
        assert user_a[0].provider_account_email in {"a1@example.com", "a2@example.com"}

    @pytest.mark.asyncio
    async def test_upsert_reactivates_needs_reauth_connection(
        self, db_context: Database
    ) -> None:
        conn = await db_context.oauth_connections.upsert_connection(
            user_id="user-a",
            provider="google",
            provider_account_email="a@example.com",
            scopes=["s1"],
            refresh_token_encrypted="cipher-1",
        )
        await db_context.oauth_connections.mark_needs_reauth(
            "user-a", "google", conn.credential_generation
        )

        reconnected = await db_context.oauth_connections.upsert_connection(
            user_id="user-a",
            provider="google",
            provider_account_email="a@example.com",
            scopes=["s1"],
            refresh_token_encrypted="cipher-new",
        )
        assert reconnected.status == "active"

    @pytest.mark.asyncio
    async def test_list_connections(self, db_context: Database) -> None:
        await db_context.oauth_connections.upsert_connection(
            "user-a", "google", "a@example.com", ["s1"], "cipher-a"
        )
        await db_context.oauth_connections.upsert_connection(
            "user-b", "google", "b@example.com", ["s1"], "cipher-b"
        )
        connections = await db_context.oauth_connections.list_connections()
        assert {c.user_id for c in connections} == {"user-a", "user-b"}

    @pytest.mark.asyncio
    async def test_mark_needs_reauth_matching_generation(
        self, db_context: Database
    ) -> None:
        conn = await db_context.oauth_connections.upsert_connection(
            "user-a", "google", "a@example.com", ["s1"], "cipher-a"
        )
        updated = await db_context.oauth_connections.mark_needs_reauth(
            "user-a", "google", conn.credential_generation
        )
        assert updated is True

        after = await db_context.oauth_connections.get_connection("user-a", "google")
        assert after is not None
        assert after.status == "needs_reauth"
        # generation rotates on the flip
        assert after.credential_generation != conn.credential_generation

    @pytest.mark.asyncio
    async def test_mark_needs_reauth_stale_generation_is_noop(
        self, db_context: Database
    ) -> None:
        """A stale generation must not touch a replacement connection."""
        original = await db_context.oauth_connections.upsert_connection(
            "user-a", "google", "a@example.com", ["s1"], "cipher-1"
        )
        stale_generation = original.credential_generation

        # A reconnect rotates the generation (simulating a replacement connection).
        replacement = await db_context.oauth_connections.upsert_connection(
            "user-a", "google", "a@example.com", ["s1"], "cipher-2"
        )
        assert replacement.credential_generation != stale_generation

        updated = await db_context.oauth_connections.mark_needs_reauth(
            "user-a", "google", stale_generation
        )
        assert updated is False

        after = await db_context.oauth_connections.get_connection("user-a", "google")
        assert after is not None
        assert after.status == "active"
        assert after.credential_generation == replacement.credential_generation

    @pytest.mark.asyncio
    async def test_update_last_used(self, db_context: Database) -> None:
        conn = await db_context.oauth_connections.upsert_connection(
            "user-a", "google", "a@example.com", ["s1"], "cipher-a"
        )
        assert conn.last_used_at is None

        await db_context.oauth_connections.update_last_used("user-a", "google")
        after = await db_context.oauth_connections.get_connection("user-a", "google")
        assert after is not None
        assert after.last_used_at is not None

    @pytest.mark.asyncio
    async def test_delete_connection(self, db_context: Database) -> None:
        await db_context.oauth_connections.upsert_connection(
            "user-a", "google", "a@example.com", ["s1"], "cipher-a"
        )
        deleted = await db_context.oauth_connections.delete_connection(
            "user-a", "google"
        )
        assert deleted is True
        assert (
            await db_context.oauth_connections.get_connection("user-a", "google")
            is None
        )

        # Deleting again reports no row removed.
        assert (
            await db_context.oauth_connections.delete_connection("user-a", "google")
            is False
        )


class TestPendingOAuthFlows:
    """Single-use claim, expiry, and cleanup semantics."""

    @pytest.mark.asyncio
    async def test_claim_pending_flow_success_then_second_claim_none(
        self, db_context: Database
    ) -> None:
        await db_context.oauth_connections.create_pending_flow(
            state_hash="hash-1", code_verifier="verifier-1", user_id="user-a"
        )

        claimed = await db_context.oauth_connections.claim_pending_flow(
            "hash-1", max_age_seconds=600
        )
        assert claimed is not None
        assert claimed.code_verifier == "verifier-1"
        assert claimed.user_id == "user-a"

        # Single-use: a second claim of the same state returns None.
        second = await db_context.oauth_connections.claim_pending_flow(
            "hash-1", max_age_seconds=600
        )
        assert second is None

    @pytest.mark.asyncio
    async def test_claim_unknown_state_returns_none(self, db_context: Database) -> None:
        assert (
            await db_context.oauth_connections.claim_pending_flow(
                "missing", max_age_seconds=600
            )
            is None
        )

    @pytest.mark.asyncio
    async def test_claim_expired_flow_returns_none(self, db_context: Database) -> None:
        await db_context.oauth_connections.create_pending_flow(
            state_hash="hash-old", code_verifier="verifier", user_id="user-a"
        )
        # Claim with an injected "now" far in the future so the flow is expired.
        future = datetime.now(UTC) + timedelta(seconds=1200)
        claimed = await db_context.oauth_connections.claim_pending_flow(
            "hash-old", max_age_seconds=600, now=future
        )
        assert claimed is None

        # The expired row is consumed by the claim (single-use, even on expiry).
        again = await db_context.oauth_connections.claim_pending_flow(
            "hash-old", max_age_seconds=600
        )
        assert again is None

    @pytest.mark.asyncio
    async def test_cleanup_expired_flows(self, db_context: Database) -> None:
        await db_context.oauth_connections.create_pending_flow("hash-1", "v1", "user-a")
        await db_context.oauth_connections.create_pending_flow("hash-2", "v2", "user-b")

        # Nothing expired yet at the natural "now".
        assert await db_context.oauth_connections.cleanup_expired_flows(600) == 0

        # With an injected future now, both flows are past the TTL.
        future = datetime.now(UTC) + timedelta(seconds=1200)
        removed = await db_context.oauth_connections.cleanup_expired_flows(
            600, now=future
        )
        assert removed == 2
        assert (
            await db_context.oauth_connections.claim_pending_flow(
                "hash-1", max_age_seconds=600
            )
            is None
        )
