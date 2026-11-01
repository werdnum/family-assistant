"""What a Home shows about its threads.

State is derived from facts rather than reported by the model (see
docs/design/one-thread-chat-ux.md, "State"). Every reader -- the API, a thread's
own card and Home's assistant -- goes through :func:`derive_state`, so they
cannot disagree about which state a thread is in.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Literal

if TYPE_CHECKING:
    from collections.abc import Callable, Collection, Sequence

    from family_assistant.storage.database import DatabaseExecutor
    from family_assistant.storage.repositories.conversation_threads import (
        ClosedBy,
        ConversationThread,
        ThreadClose,
    )

# CI control run: this comment only triggers the backend jobs.
ThreadState = Literal["working", "waiting", "done", "failed"]

_ACTIVE_RUN_STATUSES = frozenset({
    "queued",
    "running",
    "awaiting_children",
    "awaiting_remote",
})


def _as_utc(value: datetime) -> datetime:
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


@dataclass(frozen=True, slots=True)
class DerivedState:
    """A thread's state, and the explicit close it comes from, if any."""

    state: ThreadState
    close: ThreadClose | None = None


def _said_since(
    moment: datetime | None,
    *,
    latest_visible_at: datetime | None,
    latest_visible_turn_id: str | None,
    own_turn_id: str | None,
) -> bool:
    """Whether something visible arrived after ``moment``.

    Rows of ``own_turn_id`` -- the turn the moment happened in -- never count:
    a thread's assistant writes its final message after closing the thread, in
    the same turn.
    """
    if moment is None or latest_visible_at is None:
        return False
    if own_turn_id is not None and latest_visible_turn_id == own_turn_id:
        return False
    return _as_utc(latest_visible_at) > _as_utc(moment)


def derive_state(
    *,
    run_status: str | None,
    run_completed_at: datetime | None,
    work_in_progress: bool,
    latest_visible_at: datetime | None,
    latest_visible_turn_id: str | None = None,
    latest_visible_is_error: bool = False,
    close: ThreadClose | None = None,
) -> DerivedState:
    """A thread's state from what is known about it.

    Working while anything is in progress in it: its starting run, a turn, or
    background work a turn handed off. Otherwise the latest terminal event --
    an explicit close, a turn whose error is the last thing in the thread, or
    the starting run failing -- holds until something visible arrives in the
    thread after it, which reopens the thread. Anything else leaves the next
    move with the user.

    A failure after a close wins over it, the closing turn's own failure
    included: the user never got that turn's answer, and an infrastructure
    failure is shown rather than hidden behind "done".
    """
    if work_in_progress or run_status in _ACTIVE_RUN_STATUSES:
        return DerivedState("working")
    run_failed = run_status == "failed"
    close_is_latest = close is not None and (
        not run_failed
        or run_completed_at is None
        or _as_utc(close.closed_at) >= _as_utc(run_completed_at)
    )
    if (
        latest_visible_is_error
        and latest_visible_at is not None
        and (close is None or _as_utc(latest_visible_at) > _as_utc(close.closed_at))
    ):
        # A turn failed and nothing has been said since: an infrastructure
        # failure, which is never presented as a request for input.
        return DerivedState("failed")
    if (
        close is not None
        and close_is_latest
        and not _said_since(
            close.closed_at,
            latest_visible_at=latest_visible_at,
            latest_visible_turn_id=latest_visible_turn_id,
            own_turn_id=close.turn_id,
        )
    ):
        return DerivedState(close.outcome, close)
    if close is not None and close_is_latest:
        return DerivedState("waiting")
    if run_failed:
        if run_completed_at is not None and _said_since(
            run_completed_at,
            latest_visible_at=latest_visible_at,
            latest_visible_turn_id=latest_visible_turn_id,
            own_turn_id=None,
        ):
            return DerivedState("waiting")
        return DerivedState("failed")
    return DerivedState("waiting")


@dataclass(frozen=True, slots=True)
class Viewer:
    """Who is looking at a thread, for their unread count.

    ``user_id`` is canonical; ``keys`` are every id their logins store on the
    messages they write.
    """

    user_id: str
    keys: frozenset[str]


@dataclass(frozen=True, slots=True)
class ThreadSummary:
    """One thread as Home's card, the sidebar and Home's assistant see it.

    ``closed_by``, ``closed_at`` and ``echo_message_id`` describe the explicit
    close the state comes from, and are None while no close is in effect.
    ``unread_count`` is None when the summary was made for no viewer.
    """

    conversation_id: str
    title: str
    profile_id: str
    state: ThreadState
    closed_by: ClosedBy | None
    closed_at: datetime | None
    echo_message_id: int | None
    unread_count: int | None
    source_tool_call_id: str | None
    anchor_message_id: int | None
    seed_request: str | None
    preview: str | None
    created_at: datetime
    updated_at: datetime


async def list_home_threads(
    db: DatabaseExecutor,
    home_conversation_id: str,
    *,
    turn_running: Callable[[str], bool],
    viewer: Viewer | None,
) -> list[ThreadSummary]:
    """The threads of a Home, newest first.

    ``turn_running`` says whether a conversation has a turn in progress, which
    only the live stream hub knows.
    """
    threads = await db.conversation_threads.list_for_home(home_conversation_id)
    return await summarize_threads(
        db, threads, turn_running=turn_running, viewer=viewer
    )


async def summarize_threads(
    db: DatabaseExecutor,
    threads: Sequence[ConversationThread],
    *,
    turn_running: Callable[[str], bool],
    viewer: Viewer | None,
) -> list[ThreadSummary]:
    """What a card shows about each of ``threads``, in the same order."""
    if not threads:
        return []
    conversation_ids: Collection[str] = [thread.conversation_id for thread in threads]
    runs = await db.delegation_runs.get_by_delegation_ids([
        thread.delegation_id for thread in threads if thread.delegation_id
    ])
    latest = await db.message_history.latest_visible_messages(conversation_ids)
    active = await db.delegation_runs.conversations_with_active_runs(conversation_ids)
    seeds = await db.message_history.get_contents_by_internal_ids([
        thread.seed_message_id for thread in threads if thread.seed_message_id
    ])
    unread = (
        await db.conversation_threads.unread_counts(
            conversation_ids, viewer_user_id=viewer.user_id, viewer_keys=viewer.keys
        )
        if viewer is not None
        else None
    )
    summaries: list[ThreadSummary] = []
    for thread in threads:
        run = runs.get(thread.delegation_id) if thread.delegation_id else None
        message = latest.get(thread.conversation_id)
        derived = derive_state(
            run_status=run["status"] if run else None,
            run_completed_at=run["completed_at"] if run else None,
            work_in_progress=thread.conversation_id in active
            or turn_running(thread.conversation_id),
            latest_visible_at=message.timestamp if message else None,
            latest_visible_turn_id=message.turn_id if message else None,
            latest_visible_is_error=message is not None and message.role == "error",
            close=thread.close,
        )
        summaries.append(
            ThreadSummary(
                conversation_id=thread.conversation_id,
                title=thread.title,
                profile_id=thread.profile_id,
                state=derived.state,
                closed_by=derived.close.closed_by if derived.close else None,
                closed_at=_as_utc(derived.close.closed_at) if derived.close else None,
                echo_message_id=(
                    derived.close.echo_message_id if derived.close else None
                ),
                unread_count=(
                    unread.get(thread.conversation_id, 0)
                    if unread is not None
                    else None
                ),
                source_tool_call_id=thread.source_tool_call_id,
                anchor_message_id=thread.anchor_message_id,
                seed_request=(
                    seeds.get(thread.seed_message_id)
                    if thread.seed_message_id
                    else None
                ),
                preview=message.content if message else None,
                created_at=_as_utc(thread.created_at),
                updated_at=max(
                    _as_utc(thread.updated_at),
                    _as_utc(message.timestamp if message else thread.updated_at),
                ),
            )
        )
    return summaries
