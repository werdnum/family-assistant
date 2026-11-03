"""Repository for Home conversations and the threads spun off them."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Literal, cast

from sqlalchemy import or_, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.sql import functions as func

from family_assistant.storage.conversation_threads import (
    conversation_homes_table,
    conversation_read_markers_table,
    conversation_threads_table,
)
from family_assistant.storage.message_history import message_history_table
from family_assistant.storage.repositories.base import BaseRepository
from family_assistant.storage.repositories.message_history import (
    THREAD_VISIBLE_ROLES,
)

if TYPE_CHECKING:
    from collections.abc import Collection, Mapping
    from datetime import datetime

    from family_assistant.security.taint import TaintMetadata
    from family_assistant.storage.database import DatabaseTransaction

CloseOutcome = Literal["done", "failed"]
ClosedBy = Literal["assistant", "user"]


@dataclass(frozen=True, slots=True)
class ThreadClose:
    """The latest explicit close of a thread.

    ``turn_id`` is the turn the thread's assistant closed it in; None when the
    user closed it.
    """

    outcome: CloseOutcome
    closed_by: ClosedBy
    closed_at: datetime
    turn_id: str | None
    echo_message_id: int | None


@dataclass(frozen=True, slots=True)
class ConversationThread:
    """A conversation recorded as a thread of a user's Home."""

    conversation_id: str
    home_conversation_id: str
    interface_type: str
    owner_user_id: str
    profile_id: str
    title: str
    title_taint: TaintMetadata | None
    anchor_message_id: int | None
    seed_message_id: int | None
    source_tool_call_id: str | None
    delegation_id: str | None
    close: ThreadClose | None
    created_at: datetime
    updated_at: datetime


def _close_from_row(row: Mapping[str, Any]) -> ThreadClose | None:
    if row["closed_outcome"] is None:
        return None
    return ThreadClose(
        outcome=cast("CloseOutcome", row["closed_outcome"]),
        closed_by=cast("ClosedBy", row["closed_by"]),
        closed_at=row["closed_at"],
        turn_id=row["closed_turn_id"],
        echo_message_id=row["echo_message_id"],
    )


def _thread_from_row(row: Mapping[str, Any]) -> ConversationThread:
    return ConversationThread(
        conversation_id=row["conversation_id"],
        home_conversation_id=row["home_conversation_id"],
        interface_type=row["interface_type"],
        owner_user_id=row["owner_user_id"],
        profile_id=row["profile_id"],
        title=row["title"],
        title_taint=row["title_taint_json"],
        anchor_message_id=row["anchor_message_id"],
        seed_message_id=row["seed_message_id"],
        source_tool_call_id=row["source_tool_call_id"],
        delegation_id=row["delegation_id"],
        close=_close_from_row(row),
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


class ConversationThreadsRepository(BaseRepository):
    """Designate Homes and record the threads that hang off them."""

    async def get_home(self, user_id: str, interface_type: str) -> str | None:
        """Return the user's Home conversation on this interface, if designated."""
        row = await self._db.fetch_one(
            select(conversation_homes_table.c.conversation_id).where(
                conversation_homes_table.c.user_id == user_id,
                conversation_homes_table.c.interface_type == interface_type,
            )
        )
        return row["conversation_id"] if row is not None else None

    async def find_home_among(
        self, user_ids: Collection[str], interface_type: str
    ) -> str | None:
        """A Home stored under any of ``user_ids`` on this interface, if one exists."""
        if not user_ids:
            return None
        rows = await self._db.fetch_all(
            select(conversation_homes_table.c.conversation_id)
            .where(
                conversation_homes_table.c.user_id.in_(list(user_ids)),
                conversation_homes_table.c.interface_type == interface_type,
            )
            .order_by(conversation_homes_table.c.created_at)
            .limit(1)
        )
        return rows[0]["conversation_id"] if rows else None

    async def designate_home(
        self,
        *,
        user_id: str,
        interface_type: str,
        conversation_id: str,
        now: datetime,
    ) -> str:
        """Make ``conversation_id`` the user's Home unless one already exists.

        Returns the Home that is in effect afterwards, which is the existing
        one when two callers race.
        """
        insert_ctor = (
            pg_insert if self._db.dialect_name == "postgresql" else sqlite_insert
        )
        await self._db.execute(
            insert_ctor(conversation_homes_table)
            .values(
                user_id=user_id,
                interface_type=interface_type,
                conversation_id=conversation_id,
                created_at=now,
            )
            .on_conflict_do_nothing()
        )
        home = await self.get_home(user_id, interface_type)
        if home is None:
            raise RuntimeError(
                f"No Home for {user_id!r} on {interface_type!r} after designating "
                f"{conversation_id!r}; the conversation is already another user's Home"
            )
        return home

    async def is_home(self, conversation_id: str) -> bool:
        """Whether a conversation is some user's Home."""
        row = await self._db.fetch_one(
            select(conversation_homes_table.c.conversation_id).where(
                conversation_homes_table.c.conversation_id == conversation_id
            )
        )
        return row is not None

    async def kinds_for(
        self, conversation_ids: Collection[str]
    ) -> dict[str, Literal["home", "thread"]]:
        """Which of ``conversation_ids`` are Homes or threads; others are absent."""
        if not conversation_ids:
            return {}
        ids = list(conversation_ids)
        homes = await self._db.fetch_all(
            select(conversation_homes_table.c.conversation_id).where(
                conversation_homes_table.c.conversation_id.in_(ids)
            )
        )
        threads = await self._db.fetch_all(
            select(conversation_threads_table.c.conversation_id).where(
                conversation_threads_table.c.conversation_id.in_(ids)
            )
        )
        kinds: dict[str, Literal["home", "thread"]] = {
            row["conversation_id"]: "thread" for row in threads
        }
        kinds.update({row["conversation_id"]: "home" for row in homes})
        return kinds

    async def create_thread(
        self,
        *,
        conversation_id: str,
        home_conversation_id: str,
        interface_type: str,
        owner_user_id: str,
        profile_id: str,
        title: str,
        title_taint: TaintMetadata | None,
        anchor_message_id: int | None,
        delegation_id: str | None,
        now: datetime,
        source_tool_call_id: str | None = None,
    ) -> None:
        """Record a new thread of ``home_conversation_id``."""
        await self._db.execute(
            conversation_threads_table.insert().values(
                conversation_id=conversation_id,
                home_conversation_id=home_conversation_id,
                interface_type=interface_type,
                owner_user_id=owner_user_id,
                profile_id=profile_id,
                title=title,
                title_taint_json=title_taint,
                anchor_message_id=anchor_message_id,
                seed_message_id=None,
                source_tool_call_id=source_tool_call_id,
                delegation_id=delegation_id,
                created_at=now,
                updated_at=now,
            )
        )

    async def discard_thread(self, conversation_id: str) -> None:
        """Remove a thread that never started: its record, what was copied in, its marker.

        For a thread whose creation is abandoned before its first turn, so
        nothing the user wrote is lost.
        """

        async def _discard(txn: DatabaseTransaction) -> None:
            await txn.execute(
                message_history_table.delete().where(
                    or_(
                        message_history_table.c.conversation_id == conversation_id,
                        message_history_table.c.started_thread_conversation_id
                        == conversation_id,
                    )
                )
            )
            await txn.execute(
                conversation_threads_table.delete().where(
                    conversation_threads_table.c.conversation_id == conversation_id
                )
            )

        await self._db.atomic(_discard)

    async def discard_thread_of_turn(self, conversation_id: str, turn_id: str) -> bool:
        """Remove a thread that holds nothing but ``turn_id``'s rows. Returns whether it did.

        Anything else in it -- a reply the user sent while that turn ran -- keeps
        the thread and every row. Only ``turn_id``'s rows are ever deleted, and
        the record only while no row is left, so a reply landing mid-discard is
        never lost: at worst it stays in a conversation that is no longer a
        thread.
        """

        async def _discard(txn: DatabaseTransaction) -> bool:
            other = await txn.fetch_one(
                select(message_history_table.c.internal_id)
                .where(
                    message_history_table.c.conversation_id == conversation_id,
                    or_(
                        message_history_table.c.turn_id.is_(None),
                        message_history_table.c.turn_id != turn_id,
                    ),
                )
                .limit(1)
            )
            if other is not None:
                return False
            await txn.execute(
                message_history_table.delete().where(
                    message_history_table.c.conversation_id == conversation_id,
                    message_history_table.c.turn_id == turn_id,
                )
            )
            removed = await txn.execute(
                conversation_threads_table.delete().where(
                    conversation_threads_table.c.conversation_id == conversation_id,
                    ~(
                        select(message_history_table.c.internal_id)
                        .where(
                            message_history_table.c.conversation_id == conversation_id
                        )
                        .exists()
                    ),
                )
            )
            return removed.rowcount != 0

        return await self._db.atomic(_discard)

    async def set_seed_message(
        self, conversation_id: str, seed_message_id: int, now: datetime
    ) -> None:
        """Record the row that opened the thread."""
        await self._db.execute(
            conversation_threads_table
            .update()
            .where(conversation_threads_table.c.conversation_id == conversation_id)
            .values(seed_message_id=seed_message_id, updated_at=now)
        )

    async def get_thread(self, conversation_id: str) -> ConversationThread | None:
        """Return the thread record for a conversation, or None if it is not one."""
        row = await self._db.fetch_one(
            select(conversation_threads_table).where(
                conversation_threads_table.c.conversation_id == conversation_id
            )
        )
        return _thread_from_row(row) if row is not None else None

    async def list_for_home(
        self, home_conversation_id: str
    ) -> list[ConversationThread]:
        """Threads of a Home, newest first."""
        rows = await self._db.fetch_all(
            select(conversation_threads_table)
            .where(
                conversation_threads_table.c.home_conversation_id
                == home_conversation_id
            )
            .order_by(conversation_threads_table.c.created_at.desc())
        )
        return [_thread_from_row(row) for row in rows]

    async def close_thread(
        self,
        conversation_id: str,
        *,
        outcome: CloseOutcome,
        closed_by: ClosedBy,
        turn_id: str | None,
        now: datetime,
    ) -> None:
        """Record an explicit close, replacing any earlier one.

        The close starts with no echo: one is written when the assistant's
        closing turn ends, and never for a close the user made.
        """
        await self._db.execute(
            conversation_threads_table
            .update()
            .where(conversation_threads_table.c.conversation_id == conversation_id)
            .values(
                closed_outcome=outcome,
                closed_by=closed_by,
                closed_at=now,
                closed_turn_id=turn_id,
                echo_message_id=None,
                updated_at=now,
            )
        )

    async def record_echo(
        self, conversation_id: str, *, turn_id: str, echo_message_id: int
    ) -> bool:
        """Record the echo of the close made in ``turn_id``, if it has none yet.

        Returns whether it was recorded: false when the thread has since been
        closed again, or this close was already echoed.
        """
        result = await self._db.execute(
            conversation_threads_table
            .update()
            .where(
                conversation_threads_table.c.conversation_id == conversation_id,
                conversation_threads_table.c.closed_by == "assistant",
                conversation_threads_table.c.closed_turn_id == turn_id,
                conversation_threads_table.c.echo_message_id.is_(None),
            )
            .values(echo_message_id=echo_message_id)
        )
        return result.rowcount > 0

    async def mark_read(
        self,
        conversation_id: str,
        *,
        viewer_user_id: str,
        through_message_id: int,
        now: datetime,
    ) -> None:
        """Move a viewer's read marker forward to ``through_message_id``.

        Never backward: a client reporting an older position, or two clients
        racing, cannot make read messages unread again.
        """
        insert_ctor = (
            pg_insert if self._db.dialect_name == "postgresql" else sqlite_insert
        )
        statement = insert_ctor(conversation_read_markers_table).values(
            conversation_id=conversation_id,
            viewer_user_id=viewer_user_id,
            last_read_message_id=through_message_id,
            updated_at=now,
        )
        await self._db.execute(
            statement.on_conflict_do_update(
                index_elements=["conversation_id", "viewer_user_id"],
                set_={
                    "last_read_message_id": statement.excluded.last_read_message_id,
                    "updated_at": statement.excluded.updated_at,
                },
                where=(
                    conversation_read_markers_table.c.last_read_message_id
                    < statement.excluded.last_read_message_id
                ),
            )
        )

    async def unread_counts(
        self,
        conversation_ids: Collection[str],
        *,
        viewer_user_id: str,
        viewer_keys: Collection[str],
    ) -> dict[str, int]:
        """How many visible messages each conversation has that the viewer has not read.

        Visible is what a reader of the conversation sees: non-internal rows of
        ``THREAD_VISIBLE_ROLES`` with content, at its top level, a failed
        turn's error included. A message the viewer
        wrote themselves -- a user row under any of ``viewer_keys``, the ids
        their logins store -- is never unread to them. Conversations with
        nothing unread are absent.
        """
        if not conversation_ids:
            return {}
        history = message_history_table
        markers = conversation_read_markers_table
        # The furthest the viewer has read under any of their ids: a marker
        # written under a login's own id before an operator mapped it to its
        # owner still counts.
        read_through = (
            select(
                markers.c.conversation_id,
                func.max(markers.c.last_read_message_id).label("last_read"),
            )
            .where(
                markers.c.conversation_id.in_(list(conversation_ids)),
                markers.c.viewer_user_id.in_([viewer_user_id, *viewer_keys]),
            )
            .group_by(markers.c.conversation_id)
            .subquery()
        )
        rows = await self._db.fetch_all(
            select(
                history.c.conversation_id,
                func.count(history.c.internal_id).label("count"),
            )
            .select_from(
                history.outerjoin(
                    read_through,
                    read_through.c.conversation_id == history.c.conversation_id,
                )
            )
            .where(
                history.c.conversation_id.in_(list(conversation_ids)),
                history.c.is_internal.is_(False),
                history.c.role.in_(THREAD_VISIBLE_ROLES),
                history.c.content.isnot(None),
                history.c.subconversation_id.is_(None),
                # The viewer's own doing, like a message they wrote.
                history.c.started_thread_conversation_id.is_(None),
                or_(
                    read_through.c.last_read.is_(None),
                    history.c.internal_id > read_through.c.last_read,
                ),
                or_(
                    history.c.role != "user",
                    history.c.user_id.is_(None),
                    history.c.user_id.not_in(list(viewer_keys)),
                ),
            )
            .group_by(history.c.conversation_id)
        )
        return {row["conversation_id"]: row["count"] for row in rows}
