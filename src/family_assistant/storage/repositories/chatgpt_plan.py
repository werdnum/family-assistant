"""Repository for the deployment's single ChatGPT plan sign-in."""

from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, Literal

from pydantic import BaseModel, Field
from sqlalchemy import delete, insert, select, update

from family_assistant.storage.chatgpt_plan import chatgpt_plan_credentials_table
from family_assistant.storage.repositories.base import BaseRepository

if TYPE_CHECKING:
    from family_assistant.storage.database import DatabaseTransaction

type ChatGPTPlanStatus = Literal["active", "needs_reauth", "signed_out"]

_table = chatgpt_plan_credentials_table


class ChatGPTPlanCredentialsModel(BaseModel):
    """The stored sign-in, tokens still encrypted."""

    id: int
    ext_agent_host_id: str
    issued_client_id: str
    subject: str
    email: str | None = None
    scopes: list[str] = Field(default_factory=list)
    id_token_encrypted: str | None = None
    access_token_encrypted: str | None = None
    refresh_token_encrypted: str | None = None
    access_token_expires_at: datetime | None = None
    earliest_refresh_at: datetime | None = None
    status: ChatGPTPlanStatus
    credential_generation: str
    connected_by_user_id: str | None = None
    created_at: datetime
    updated_at: datetime
    last_used_at: datetime | None = None


class ChatGPTPlanTokenSet(BaseModel):
    """A complete encrypted token set, as written after an exchange or refresh."""

    id_token_encrypted: str | None
    access_token_encrypted: str
    refresh_token_encrypted: str
    access_token_expires_at: datetime
    earliest_refresh_at: datetime | None
    scopes: list[str]


def _as_utc(value: datetime | None) -> datetime | None:
    """SQLite drops the zone on the way back; every stored value is UTC."""
    if value is None or value.tzinfo is not None:
        return value
    return value.replace(tzinfo=UTC)


# ast-grep-ignore: no-dict-any - dict[str, Any] from Database.fetch_one
def _row_to_model(row: dict[str, Any]) -> ChatGPTPlanCredentialsModel:
    data = dict(row)
    scopes = data.get("scopes")
    if isinstance(scopes, str):
        data["scopes"] = json.loads(scopes)
    for key in (
        "access_token_expires_at",
        "earliest_refresh_at",
        "created_at",
        "updated_at",
        "last_used_at",
    ):
        data[key] = _as_utc(data.get(key))
    return ChatGPTPlanCredentialsModel.model_validate(data)


class ChatGPTPlanRepository(BaseRepository):
    """Reads and writes the single ``chatgpt_plan_credentials`` row."""

    async def get(self) -> ChatGPTPlanCredentialsModel | None:
        """The stored sign-in, or None if nobody has ever signed in."""
        row = await self._db.fetch_one(
            select(_table).order_by(_table.c.id.desc()).limit(1)
        )
        return _row_to_model(row) if row is not None else None

    async def replace_sign_in(
        self,
        *,
        ext_agent_host_id: str,
        issued_client_id: str,
        subject: str,
        email: str | None,
        tokens: ChatGPTPlanTokenSet,
        connected_by_user_id: str,
    ) -> ChatGPTPlanCredentialsModel:
        """Store a freshly validated sign-in as the deployment's only one."""
        now = datetime.now(UTC)

        async def write(txn: DatabaseTransaction) -> None:
            await txn.execute(delete(_table))
            await txn.execute(
                insert(_table).values(
                    ext_agent_host_id=ext_agent_host_id,
                    issued_client_id=issued_client_id,
                    subject=subject,
                    email=email,
                    status="active",
                    credential_generation=str(uuid.uuid4()),
                    connected_by_user_id=connected_by_user_id,
                    created_at=now,
                    updated_at=now,
                    **tokens.model_dump(),
                )
            )

        await self._db.atomic(write)
        stored = await self.get()
        if stored is None:  # pragma: no cover - the row was just written
            raise RuntimeError("ChatGPT plan sign-in missing immediately after write")
        return stored

    async def store_refreshed_tokens(
        self, expected_generation: str, tokens: ChatGPTPlanTokenSet
    ) -> str | None:
        """Replace the token set if the row is still the one refreshed.

        Returns the new generation, or None when a sign-in, sign-out or another
        refresh replaced the row in between -- its tokens are then the ones to
        keep.
        """
        new_generation = str(uuid.uuid4())
        result = await self._db.execute(
            update(_table)
            .where(
                _table.c.credential_generation == expected_generation,
                _table.c.status == "active",
            )
            .values(
                credential_generation=new_generation,
                updated_at=datetime.now(UTC),
                **tokens.model_dump(),
            )
        )
        return new_generation if result.rowcount > 0 else None

    async def mark_needs_reauth(self, expected_generation: str) -> bool:
        """Flip an active sign-in whose refresh token is unusable."""
        result = await self._db.execute(
            update(_table)
            .where(
                _table.c.credential_generation == expected_generation,
                _table.c.status == "active",
            )
            .values(
                status="needs_reauth",
                credential_generation=str(uuid.uuid4()),
                updated_at=datetime.now(UTC),
            )
        )
        return result.rowcount > 0

    async def sign_out(self, expected_generation: str) -> bool:
        """Clear the tokens, keeping the registration for a later sign-in.

        Conditional on the generation the caller read, so a sign-in completed
        in between is left alone. Returns True iff the row was cleared.
        """
        result = await self._db.execute(
            update(_table)
            .where(
                _table.c.credential_generation == expected_generation,
                _table.c.status != "signed_out",
            )
            .values(
                status="signed_out",
                id_token_encrypted=None,
                access_token_encrypted=None,
                refresh_token_encrypted=None,
                access_token_expires_at=None,
                earliest_refresh_at=None,
                credential_generation=str(uuid.uuid4()),
                updated_at=datetime.now(UTC),
            )
        )
        return result.rowcount > 0

    async def touch_last_used(self, generation: str) -> None:
        """Stamp ``last_used_at`` for the settings page."""
        await self._db.execute(
            update(_table)
            .where(_table.c.credential_generation == generation)
            .values(last_used_at=datetime.now(UTC))
        )
