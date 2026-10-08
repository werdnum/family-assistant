"""Repository for generated conversation-list summaries."""

from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import ColumnElement, or_, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.sql import functions as func

from family_assistant.storage.conversation_summaries import (
    conversation_summaries_table,
)
from family_assistant.storage.message_history import message_history_table
from family_assistant.storage.repositories.base import BaseRepository


@dataclass(frozen=True, slots=True)
class SummaryDueConversation:
    """A conversation with visible messages its summary has not seen."""

    conversation_id: str
    latest_message_id: int


@dataclass(frozen=True, slots=True)
class SummaryTranscriptMessage:
    """One visible chat message, as the summarizer reads it."""

    internal_id: int
    role: str
    content: str


def _transcript_conditions() -> list[ColumnElement[bool]]:
    """The rows a conversation list shows: the visible top-level chat.

    Kept identical to what ``get_conversation_summaries`` lists, so a
    conversation is summarized exactly when it can appear in the list, and from
    the same messages a reader of it would see.
    """
    return [
        message_history_table.c.is_internal.is_(False),
        message_history_table.c.role.in_(["user", "assistant"]),
        message_history_table.c.content.isnot(None),
        message_history_table.c.subconversation_id.is_(None),
    ]


class ConversationSummariesRepository(BaseRepository):
    """Store one generated summary per conversation, with a watermark."""

    async def select_due(
        self,
        *,
        settled_before: datetime,
        active_since: datetime,
        limit: int,
    ) -> list[SummaryDueConversation]:
        """Conversations with unsummarized messages, most recently active first.

        A conversation is due when its latest visible message is newer than its
        summary's watermark, landed before ``settled_before`` (so a turn still
        in progress is not summarized halfway), and landed after
        ``active_since``. The lower bound keeps the per-sweep scan to recent
        history and stops a fresh deployment from summarizing years of old
        conversations; those keep showing their latest message.
        """
        latest = (
            select(
                message_history_table.c.conversation_id,
                func.max(message_history_table.c.internal_id).label("latest_id"),
                func.max(message_history_table.c.timestamp).label("latest_at"),
            )
            .where(
                *_transcript_conditions(),
                message_history_table.c.timestamp >= active_since,
            )
            .group_by(message_history_table.c.conversation_id)
            .subquery()
        )
        query = (
            select(latest.c.conversation_id, latest.c.latest_id)
            .outerjoin(
                conversation_summaries_table,
                conversation_summaries_table.c.conversation_id
                == latest.c.conversation_id,
            )
            .where(
                latest.c.latest_at < settled_before,
                or_(
                    conversation_summaries_table.c.summarized_through_id.is_(None),
                    conversation_summaries_table.c.summarized_through_id
                    < latest.c.latest_id,
                ),
            )
            .order_by(latest.c.latest_at.desc(), latest.c.conversation_id.desc())
            .limit(limit)
        )
        rows = await self._db.fetch_all(query)
        return [
            SummaryDueConversation(
                conversation_id=str(row["conversation_id"]),
                latest_message_id=int(row["latest_id"]),
            )
            for row in rows
        ]

    async def transcript(
        self,
        conversation_id: str,
        *,
        through_id: int,
        head: int,
        tail: int,
    ) -> list[SummaryTranscriptMessage]:
        """The conversation's opening ``head`` and latest ``tail`` messages.

        The opening says what the conversation set out to do and the tail says
        where it got to; the middle of a long conversation is what a one-line
        summary can afford to lose.
        """
        conditions = [
            *_transcript_conditions(),
            message_history_table.c.conversation_id == conversation_id,
            message_history_table.c.internal_id <= through_id,
        ]
        columns = (
            message_history_table.c.internal_id,
            message_history_table.c.role,
            message_history_table.c.content,
        )
        head_rows = await self._db.fetch_all(
            select(*columns)
            .where(*conditions)
            .order_by(message_history_table.c.internal_id.asc())
            .limit(head)
        )
        tail_rows = await self._db.fetch_all(
            select(*columns)
            .where(*conditions)
            .order_by(message_history_table.c.internal_id.desc())
            .limit(tail)
        )
        by_id = {
            int(row["internal_id"]): SummaryTranscriptMessage(
                internal_id=int(row["internal_id"]),
                role=str(row["role"]),
                content=str(row["content"]),
            )
            for row in [*head_rows, *tail_rows]
        }
        return [by_id[internal_id] for internal_id in sorted(by_id)]

    async def latest_user_id(self, conversation_id: str) -> str | None:
        """Who most recently wrote in the conversation, for the activity ping."""
        row = await self._db.fetch_one(
            select(message_history_table.c.user_id)
            .where(
                *_transcript_conditions(),
                message_history_table.c.conversation_id == conversation_id,
                message_history_table.c.role == "user",
                message_history_table.c.user_id.isnot(None),
            )
            .order_by(message_history_table.c.internal_id.desc())
            .limit(1)
        )
        return str(row["user_id"]) if row is not None else None

    async def record(
        self,
        conversation_id: str,
        *,
        summary: str | None,
        through_id: int,
        now: datetime,
    ) -> None:
        """Store the outcome of summarizing the conversation through ``through_id``.

        ``summary=None`` records an attempt that produced nothing usable and
        keeps the previous summary, so a transient failure does not blank a
        conversation that had one.
        """
        insert_ctor = (
            pg_insert if self._db.dialect_name == "postgresql" else sqlite_insert
        )
        base_stmt = insert_ctor(conversation_summaries_table).values(
            conversation_id=conversation_id,
            summary=summary,
            summarized_through_id=through_id,
            updated_at=now,
        )
        updates: dict[str, object] = {
            "summarized_through_id": base_stmt.excluded.summarized_through_id,
            "updated_at": base_stmt.excluded.updated_at,
        }
        if summary is not None:
            updates["summary"] = base_stmt.excluded.summary
        stmt = base_stmt.on_conflict_do_update(
            index_elements=[conversation_summaries_table.c.conversation_id],
            set_=updates,
        )
        await self._db.execute(stmt)

    async def get_summary(self, conversation_id: str) -> str | None:
        """The stored summary for one conversation, if it has one."""
        row = await self._db.fetch_one(
            select(conversation_summaries_table.c.summary).where(
                conversation_summaries_table.c.conversation_id == conversation_id
            )
        )
        return row["summary"] if row is not None else None
