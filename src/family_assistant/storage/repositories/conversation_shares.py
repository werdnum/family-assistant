"""Repository for read-only conversation share links."""

from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import delete, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert

from family_assistant.storage.conversation_shares import conversation_shares_table
from family_assistant.storage.repositories.base import BaseRepository


@dataclass(frozen=True, slots=True)
class ConversationShare:
    """A persisted active conversation share."""

    conversation_id: str
    owner_user_id: str
    token_hash: str
    created_at: datetime


def _share_from_row(row: dict[str, object]) -> ConversationShare:
    return ConversationShare(
        conversation_id=str(row["conversation_id"]),
        owner_user_id=str(row["owner_user_id"]),
        token_hash=str(row["token_hash"]),
        created_at=row["created_at"],  # type: ignore[arg-type]
    )


class ConversationSharesRepository(BaseRepository):
    """Store one rotatable active share per conversation."""

    async def rotate(
        self, conversation_id: str, owner_user_id: str, token_hash: str
    ) -> ConversationShare:
        """Create or replace the active share for a conversation."""
        insert_ctor = (
            pg_insert if self._db.dialect_name == "postgresql" else sqlite_insert
        )
        base_stmt = insert_ctor(conversation_shares_table).values(
            conversation_id=conversation_id,
            owner_user_id=owner_user_id,
            token_hash=token_hash,
        )
        stmt = base_stmt.on_conflict_do_update(
            index_elements=[conversation_shares_table.c.conversation_id],
            set_={
                "owner_user_id": base_stmt.excluded.owner_user_id,
                "token_hash": base_stmt.excluded.token_hash,
                "created_at": base_stmt.excluded.created_at,
            },
        )
        await self._db.execute(stmt)
        share = await self.get_by_conversation(conversation_id)
        if share is None:  # pragma: no cover - row exists after successful upsert
            raise RuntimeError("Conversation share missing immediately after rotation")
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

    async def get_by_token_hash(self, token_hash: str) -> ConversationShare | None:
        """Return the active share matching a token digest, if any."""
        row = await self._db.fetch_one(
            select(conversation_shares_table).where(
                conversation_shares_table.c.token_hash == token_hash
            )
        )
        return _share_from_row(row) if row is not None else None

    async def revoke(self, conversation_id: str) -> bool:
        """Remove the active share for a conversation."""
        result = await self._db.execute(
            delete(conversation_shares_table).where(
                conversation_shares_table.c.conversation_id == conversation_id
            )
        )
        return result.rowcount > 0
