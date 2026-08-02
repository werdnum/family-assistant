"""Repository for per-user OAuth connections and pending flows."""

from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

from pydantic import BaseModel, Field
from sqlalchemy import delete, insert, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.exc import SQLAlchemyError

from family_assistant.storage.oauth_connections import (
    pending_oauth_flows_table,
    user_oauth_connections_table,
)
from family_assistant.storage.repositories.base import BaseRepository


class OAuthConnectionModel(BaseModel):
    """A stored per-user OAuth connection row."""

    id: int
    user_id: str
    provider: str
    provider_account_email: str
    scopes: list[str] = Field(default_factory=list)
    refresh_token_encrypted: str
    credential_generation: str
    status: str
    created_at: datetime
    updated_at: datetime
    last_used_at: datetime | None = None


class PendingOAuthFlowModel(BaseModel):
    """A stored pending OAuth authorization-code flow."""

    id: int
    state_hash: str
    code_verifier: str
    user_id: str
    created_at: datetime


def _coerce_scopes(value: list[str] | str | None) -> list[str]:
    """Return the scopes list, tolerating a JSON-string round-trip from SQLite."""
    if value is None:
        return []
    if isinstance(value, list):
        return list(value)
    parsed = json.loads(value)
    return list(parsed) if isinstance(parsed, list) else []


# ast-grep-ignore: no-dict-any - dict[str, Any] from Database.fetch_one
def _row_to_connection(row: dict[str, Any]) -> OAuthConnectionModel:
    """Convert a database row dict to a OAuthConnectionModel."""
    return OAuthConnectionModel(
        id=row["id"],
        user_id=row["user_id"],
        provider=row["provider"],
        provider_account_email=row["provider_account_email"],
        scopes=_coerce_scopes(row["scopes"]),
        refresh_token_encrypted=row["refresh_token_encrypted"],
        credential_generation=row["credential_generation"],
        status=row["status"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
        last_used_at=row.get("last_used_at"),
    )


# ast-grep-ignore: no-dict-any - dict[str, Any] from Database.fetch_one
def _row_to_pending_flow(row: dict[str, Any]) -> PendingOAuthFlowModel:
    """Convert a database row dict to a PendingOAuthFlowModel."""
    return PendingOAuthFlowModel(
        id=row["id"],
        state_hash=row["state_hash"],
        code_verifier=row["code_verifier"],
        user_id=row["user_id"],
        created_at=row["created_at"],
    )


class OAuthConnectionsRepository(BaseRepository):
    """Repository for per-user OAuth connections and pending flows."""

    async def get_connection(
        self, user_id: str, provider: str
    ) -> OAuthConnectionModel | None:
        """Return the connection for a user/provider, or None."""
        try:
            stmt = select(user_oauth_connections_table).where(
                user_oauth_connections_table.c.user_id == user_id,
                user_oauth_connections_table.c.provider == provider,
            )
            row = await self._db.fetch_one(stmt)
            return _row_to_connection(row) if row is not None else None
        except SQLAlchemyError as e:
            self._logger.exception(
                f"Database error in get_connection({user_id!r}): {e}"
            )
            raise

    async def list_connections(self) -> list[OAuthConnectionModel]:
        """Return all connections, for status/diagnostics surfaces."""
        try:
            stmt = select(user_oauth_connections_table).order_by(
                user_oauth_connections_table.c.user_id,
                user_oauth_connections_table.c.provider,
            )
            rows = await self._db.fetch_all(stmt)
            return [_row_to_connection(row) for row in rows]
        except SQLAlchemyError as e:
            self._logger.exception(f"Database error in list_connections: {e}")
            raise

    async def upsert_connection(
        self,
        user_id: str,
        provider: str,
        provider_account_email: str,
        scopes: list[str],
        refresh_token_encrypted: str,
    ) -> OAuthConnectionModel:
        """Create or replace a connection, rotating ``credential_generation``.

        Every write sets ``status='active'`` and rotates
        ``credential_generation`` to a fresh UUID so stale in-memory access-token
        cache entries become unreachable immediately.
        """
        now = datetime.now(UTC)
        new_generation = str(uuid.uuid4())

        # Dialect-native atomic upsert (single INSERT ... ON CONFLICT DO UPDATE)
        # so two concurrent same-user callbacks cannot both pick INSERT and have
        # one die on the unique constraint after its authorization code was
        # already consumed. ``created_at`` is only in the INSERT values, so it is
        # preserved on an update; every write rotates ``credential_generation``,
        # resets ``status`` to active, and bumps ``updated_at``.
        insert_ctor = (
            pg_insert if self._db.dialect_name == "postgresql" else sqlite_insert
        )
        base_stmt = insert_ctor(user_oauth_connections_table).values(
            user_id=user_id,
            provider=provider,
            provider_account_email=provider_account_email,
            scopes=scopes,
            refresh_token_encrypted=refresh_token_encrypted,
            credential_generation=new_generation,
            status="active",
            created_at=now,
            updated_at=now,
        )
        stmt = base_stmt.on_conflict_do_update(
            index_elements=[
                user_oauth_connections_table.c.user_id,
                user_oauth_connections_table.c.provider,
            ],
            set_={
                "provider_account_email": base_stmt.excluded.provider_account_email,
                "scopes": base_stmt.excluded.scopes,
                "refresh_token_encrypted": base_stmt.excluded.refresh_token_encrypted,
                "credential_generation": base_stmt.excluded.credential_generation,
                "status": base_stmt.excluded.status,
                "updated_at": base_stmt.excluded.updated_at,
            },
        )
        await self._db.execute(stmt)
        connection = await self.get_connection(user_id, provider)
        if connection is None:  # pragma: no cover - row always exists after write
            raise RuntimeError("Connection row missing immediately after upsert")
        return connection

    async def mark_needs_reauth(
        self, user_id: str, provider: str, expected_generation: str
    ) -> bool:
        """Flip an active connection to ``needs_reauth`` if the generation matches.

        Conditional on ``credential_generation == expected_generation`` and
        ``status == 'active'`` so a refresh failure can never mark a *replacement*
        connection. Rotates the generation on the flip too. Returns True iff a row
        was updated.
        """
        now = datetime.now(UTC)
        stmt = (
            update(user_oauth_connections_table)
            .where(
                user_oauth_connections_table.c.user_id == user_id,
                user_oauth_connections_table.c.provider == provider,
                user_oauth_connections_table.c.credential_generation
                == expected_generation,
                user_oauth_connections_table.c.status == "active",
            )
            .values(
                status="needs_reauth",
                credential_generation=str(uuid.uuid4()),
                updated_at=now,
            )
        )
        result = await self._db.execute(stmt)
        return result.rowcount > 0  # type: ignore[union-attr] # rowcount available on CursorResult

    async def update_last_used(self, user_id: str, provider: str) -> None:
        """Stamp ``last_used_at`` after a successful API use."""
        now = datetime.now(UTC)
        stmt = (
            update(user_oauth_connections_table)
            .where(
                user_oauth_connections_table.c.user_id == user_id,
                user_oauth_connections_table.c.provider == provider,
            )
            .values(last_used_at=now)
        )
        await self._db.execute(stmt)

    async def delete_connection(self, user_id: str, provider: str) -> bool:
        """Delete a connection. Returns True iff a row was removed."""
        stmt = delete(user_oauth_connections_table).where(
            user_oauth_connections_table.c.user_id == user_id,
            user_oauth_connections_table.c.provider == provider,
        )
        result = await self._db.execute(stmt)
        return result.rowcount > 0  # type: ignore[union-attr] # rowcount available on CursorResult

    async def create_pending_flow(
        self, state_hash: str, code_verifier: str, user_id: str
    ) -> None:
        """Persist a pending OAuth flow keyed by the hashed state nonce."""
        now = datetime.now(UTC)
        stmt = insert(pending_oauth_flows_table).values(
            state_hash=state_hash,
            code_verifier=code_verifier,
            user_id=user_id,
            created_at=now,
        )
        await self._db.execute(stmt)

    async def claim_pending_flow(
        self,
        state_hash: str,
        max_age_seconds: int,
        now: datetime | None = None,
    ) -> PendingOAuthFlowModel | None:
        """Atomically claim (single-use consume) a pending flow by state hash.

        Selects the row, then a conditional DELETE on its id; exactly one caller
        wins (``rowcount == 1``), so a replayed callback presenting the same state
        returns None. Rows older than ``max_age_seconds`` are treated as expired:
        the claim deletes them but returns None.
        """
        current = now if now is not None else datetime.now(UTC)
        select_stmt = select(pending_oauth_flows_table).where(
            pending_oauth_flows_table.c.state_hash == state_hash
        )
        row = await self._db.fetch_one(select_stmt)
        if row is None:
            return None

        delete_stmt = delete(pending_oauth_flows_table).where(
            pending_oauth_flows_table.c.id == row["id"]
        )
        result = await self._db.execute(delete_stmt)
        if result.rowcount != 1:  # type: ignore[union-attr] # rowcount available on CursorResult
            return None

        flow = _row_to_pending_flow(row)
        expiry_cutoff = current - timedelta(seconds=max_age_seconds)
        created_at = flow.created_at
        if created_at.tzinfo is None:
            created_at = created_at.replace(tzinfo=UTC)
        if created_at < expiry_cutoff:
            return None
        return flow

    async def cleanup_expired_flows(
        self, max_age_seconds: int, now: datetime | None = None
    ) -> int:
        """Delete pending flows older than ``max_age_seconds``. Returns count."""
        current = now if now is not None else datetime.now(UTC)
        expiry_cutoff = current - timedelta(seconds=max_age_seconds)
        stmt = delete(pending_oauth_flows_table).where(
            pending_oauth_flows_table.c.created_at < expiry_cutoff
        )
        result = await self._db.execute(stmt)
        return result.rowcount or 0  # type: ignore[union-attr] # rowcount available on CursorResult
