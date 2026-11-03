"""Repository for read-only conversation and message share links."""

from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import Table, delete, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert

from family_assistant.storage.conversation_shares import (
    conversation_message_shares_table,
    conversation_shares_table,
)
from family_assistant.storage.repositories.base import BaseRepository


@dataclass(frozen=True, slots=True)
class ConversationShare:
    """A persisted active share of a conversation, or of one message in it."""

    conversation_id: str
    owner_user_id: str
    token_hash: str
    created_at: datetime
    message_id: int | None = None


def _share_from_row(row: dict[str, object]) -> ConversationShare:
    return ConversationShare(
        conversation_id=str(row["conversation_id"]),
        owner_user_id=str(row["owner_user_id"]),
        token_hash=str(row["token_hash"]),
        created_at=row["created_at"],  # type: ignore[arg-type]
        message_id=row.get("message_id"),  # type: ignore[arg-type]
    )


class ConversationSharesRepository(BaseRepository):
    """Store one rotatable active share per conversation and per message."""

    async def _upsert(
        self,
        table: Table,
        key_column: str,
        key: str | int,
        **values: str,
    ) -> None:
        insert_ctor = (
            pg_insert if self._db.dialect_name == "postgresql" else sqlite_insert
        )
        base_stmt = insert_ctor(table).values({key_column: key, **values})
        stmt = base_stmt.on_conflict_do_update(
            index_elements=[table.c[key_column]],
            set_={
                **{name: base_stmt.excluded[name] for name in values},
                "created_at": base_stmt.excluded.created_at,
            },
        )
        await self._db.execute(stmt)

    async def rotate(
        self, conversation_id: str, owner_user_id: str, token_hash: str
    ) -> ConversationShare:
        """Create or replace the active share for a conversation."""
        await self._upsert(
            conversation_shares_table,
            "conversation_id",
            conversation_id,
            owner_user_id=owner_user_id,
            token_hash=token_hash,
        )
        share = await self.get_by_conversation(conversation_id)
        if share is None:  # pragma: no cover - row exists after successful upsert
            raise RuntimeError("Conversation share missing immediately after rotation")
        return share

    async def rotate_message(
        self,
        message_id: int,
        conversation_id: str,
        owner_user_id: str,
        token_hash: str,
    ) -> ConversationShare:
        """Create or replace the active share for one message."""
        await self._upsert(
            conversation_message_shares_table,
            "message_id",
            message_id,
            conversation_id=conversation_id,
            owner_user_id=owner_user_id,
            token_hash=token_hash,
        )
        share = await self.get_by_message(message_id)
        if share is None:  # pragma: no cover - row exists after successful upsert
            raise RuntimeError("Message share missing immediately after rotation")
        return share

    async def get_by_conversation(
        self, conversation_id: str
    ) -> ConversationShare | None:
        """Return the active share for a conversation, if any."""
        row = await self._db.fetch_one(
            select(conversation_shares_table).where(
                conversation_shares_table.c.conversation_id == conversation_id
            )
        )
        return _share_from_row(row) if row is not None else None

    async def get_by_message(self, message_id: int) -> ConversationShare | None:
        """Return the active share for one message, if any."""
        row = await self._db.fetch_one(
            select(conversation_message_shares_table).where(
                conversation_message_shares_table.c.message_id == message_id
            )
        )
        return _share_from_row(row) if row is not None else None

    async def get_by_token_hash(self, token_hash: str) -> ConversationShare | None:
        """Return the active share matching a token digest, of either scope."""
        for table in (conversation_shares_table, conversation_message_shares_table):
            row = await self._db.fetch_one(
                select(table).where(table.c.token_hash == token_hash)
            )
            if row is not None:
                return _share_from_row(row)
        return None

    async def revoke(self, conversation_id: str) -> bool:
        """Remove the active share for a conversation."""
        result = await self._db.execute(
            delete(conversation_shares_table).where(
                conversation_shares_table.c.conversation_id == conversation_id
            )
        )
        return result.rowcount > 0

    async def revoke_message(self, message_id: int) -> bool:
        """Remove the active share for one message."""
        result = await self._db.execute(
            delete(conversation_message_shares_table).where(
                conversation_message_shares_table.c.message_id == message_id
            )
        )
        return result.rowcount > 0
