"""Client sync: one request that brings a client up to date from any cursor.

A client holds a local copy of its conversations, the messages of the
conversations it has open (its *focus*), its pending confirmations and
delegations, and the progress of turns running in its focus. ``GET /v1/sync``
returns everything that changed since the client's cursor, so one short request
converges the client from any state; ``wait`` makes the same request a long
poll, and ``GET /v1/sync/stream`` repeats it over one connection.

Database state changes are found through the commit-ordered ``sync_cursor``
every synced row carries (``storage/sync_tracking.py``). Two things live only in
the stream hub and have no cursor: which conversations have a turn running, and
what a running turn has produced so far. The client says what it last saw of
both (``running`` and ``focus_seq``) and the server answers with whatever
differs.

See docs/design/mobile-state-sync.md.
"""

import asyncio
import json
import logging
from collections.abc import AsyncIterator, Collection
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from family_assistant.config_models import AppConfig
from family_assistant.storage.database import Database
from family_assistant.storage.sync_tracking import sync_wakeup_for
from family_assistant.storage.types import MessageHistoryRow
from family_assistant.threads.home import find_home, viewer_for
from family_assistant.threads.listing import summarize_threads
from family_assistant.web.conversation_stream_hub import ConversationStreamHub
from family_assistant.web.dependencies import (
    get_attachment_registry,
    get_current_user,
    get_db,
    get_user_identity_resolver,
)
from family_assistant.web.routers.chat_api import (
    ConversationMessage,
    PendingDelegation,
    PendingToolConfirmation,
    ThreadSummaryResponse,
    caller_owner_user_ids,
    enrich_persisted_attachments,
    get_stream_hub,
    pending_confirmation_response,
    pending_delegation_response,
    serialize_conversation_messages,
    thread_summary_response,
    turn_running_in,
)

if TYPE_CHECKING:
    from family_assistant.services.attachment_registry import AttachmentRegistry

logger = logging.getLogger(__name__)

sync_api_router = APIRouter()

BOOTSTRAP_CONVERSATIONS = 100
FRESH_HISTORY_MESSAGES = 50
DEFAULT_MESSAGE_LIMIT = 500
MAX_WAIT_SECONDS = 30.0
STREAM_HEARTBEAT_SECONDS = 10.0
# A running turn publishes many small deltas; a waiting request that wakes on
# one waits this long for more, so a slow link gets fewer, larger responses.
PROGRESS_COALESCE_SECONDS = 0.25


class TurnProgress(BaseModel):
    """What a running turn has produced so far, for a client that joins mid-turn."""

    conversation_id: str
    turn_id: str
    started_at: datetime
    progress_seq: int = Field(
        ..., description="The conversation's stream seq this snapshot is current to"
    )
    # ast-grep-ignore: no-dict-any - stream event payloads are heterogeneous JSON, typed per event by the clients that render them
    parts: list[dict[str, Any]] = Field(
        ...,
        description=(
            "The turn's events in order, each with a 'type' and its payload; "
            "consecutive text deltas are merged into one"
        ),
    )


class ConversationRow(BaseModel):
    """A conversation as the client's lists show it."""

    conversation_id: str
    kind: str = Field(..., description="'conversation', 'home' or 'thread'")
    last_message: str | None
    summary: str | None
    last_timestamp: datetime | None
    message_count: int
    unread_count: int | None = Field(
        default=None, description="Unread messages, for a Home or a thread"
    )
    thread: ThreadSummaryResponse | None = None


class ConversationMessages(BaseModel):
    """Message changes in one focus conversation."""

    conversation_id: str
    messages: list[ConversationMessage]
    removed_message_ids: list[int]
    replaces_history: bool = Field(
        default=False,
        description=(
            "True when ``messages`` is the conversation's latest page, sent because "
            "the client asked for fresh history: drop what you held"
        ),
    )
    has_more_before: bool = False


class SyncResponse(BaseModel):
    cursor: int = Field(..., description="Pass this back as ``cursor`` next time")
    has_more: bool = Field(
        ..., description="More changes are waiting; ask again straight away"
    )
    bootstrap: bool = Field(
        ...,
        description=(
            "The conversation list is a full snapshot: drop rows not in it. Sent "
            "when the client had no cursor, or one this server cannot continue"
        ),
    )
    server_time: datetime
    home_conversation_id: str | None
    conversations: list[ConversationRow]
    removed_conversation_ids: list[str]
    messages: list[ConversationMessages]
    confirmations: list[PendingToolConfirmation] | None = Field(
        ...,
        description="Every pending confirmation, or null when none changed",
    )
    delegations: dict[str, list[PendingDelegation]] = Field(
        ...,
        description=(
            "Pending delegations by conversation, each list replacing the one "
            "held; conversations not named are unchanged"
        ),
    )
    running_conversation_ids: list[str]
    active_turns: list[TurnProgress]
    focus_seq: dict[str, int] = Field(
        ..., description="Pass back as ``focus_seq``: the progress the client has seen"
    )


@dataclass(frozen=True)
class SyncQuery:
    # None asks for a bootstrap. Zero is a real position: a database with no
    # tracked writes yet.
    cursor: int | None
    focus: frozenset[str]
    fresh: frozenset[str]
    running: frozenset[str]
    focus_seq: dict[str, int] = field(default_factory=dict)
    limit: int = DEFAULT_MESSAGE_LIMIT


@dataclass(frozen=True)
class SyncContext:
    db: Database
    hub: ConversationStreamHub
    config: AppConfig
    attachment_registry: "AttachmentRegistry"
    user_id: str
    owner_user_ids: set[str]


def _split(value: str | None) -> frozenset[str]:
    if not value:
        return frozenset()
    return frozenset(part for part in value.split(",") if part)


def _parse_focus_seq(value: str | None) -> dict[str, int]:
    seqs: dict[str, int] = {}
    for pair in _split(value):
        conversation_id, _, seq = pair.rpartition(":")
        if not conversation_id:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"focus_seq entry {pair!r} is not conversation_id:seq",
            )
        try:
            seqs[conversation_id] = int(seq)
        except ValueError as error:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"focus_seq entry {pair!r} has a non-integer seq",
            ) from error
    return seqs


async def _context(
    request: Request,
    current_user: dict,
    db: Database,
    attachment_registry: "AttachmentRegistry",
) -> SyncContext:
    user_id = current_user.get("user_identifier")
    if not isinstance(user_id, str) or not user_id:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED)
    return SyncContext(
        db=db,
        hub=get_stream_hub(request),
        config=request.app.state.config,
        attachment_registry=attachment_registry,
        user_id=user_id,
        owner_user_ids=await caller_owner_user_ids(
            get_user_identity_resolver(request), db, user_id
        ),
    )


async def _serialize_messages(
    ctx: SyncContext, rows: list[MessageHistoryRow]
) -> list[ConversationMessage]:
    await enrich_persisted_attachments(
        rows,
        db_context=ctx.db,
        attachment_registry=ctx.attachment_registry,
        acting_user_id=ctx.user_id,
    )
    feedback = await ctx.db.turn_feedback.for_turns(
        turn_ids={turn_id for row in rows if (turn_id := row.get("turn_id"))},
        user_id=ctx.user_id,
    )
    return serialize_conversation_messages(rows, feedback)


async def _changed_messages(
    ctx: SyncContext,
    conversation_ids: Collection[str],
    after: int,
    through: int,
    limit: int,
) -> tuple[list[MessageHistoryRow], int, bool]:
    """Changed rows of ``conversation_ids``, cut at a transaction boundary.

    Returns the rows, the cursor they are complete through, and whether more
    are waiting. Rows written by one transaction share a cursor and are never
    split across responses, so a single transaction larger than ``limit`` is
    returned whole.
    """
    rows = await ctx.db.message_history.rows_changed_between(
        conversation_ids, after_cursor=after, through_cursor=through, limit=limit + 1
    )
    if len(rows) <= limit:
        return rows, through, False
    cut = rows[limit].get("sync_cursor", 0)
    kept = [row for row in rows if row.get("sync_cursor", 0) < cut]
    if kept:
        return kept, cut - 1, True
    whole = await ctx.db.message_history.rows_changed_between(
        conversation_ids, after_cursor=after, through_cursor=cut, limit=1_000_000
    )
    return whole, cut, cut < through


async def _focus_messages(
    ctx: SyncContext,
    query: SyncQuery,
    focus: set[str],
    after: int,
    through: int,
) -> tuple[list[ConversationMessages], int, bool]:
    fresh = focus & query.fresh
    incremental = focus - fresh
    rows, through, has_more = await _changed_messages(
        ctx, incremental, after, through, query.limit
    )
    removed = await ctx.db.sync.removed_message_ids(incremental, after, through)
    # A rating is shown on its turn's messages, so a rating change resends them.
    rated = await ctx.db.message_history.visible_rows_for_turns(
        incremental,
        await ctx.db.sync.feedback_turn_ids(ctx.owner_user_ids, after, through),
    )
    by_conversation: dict[str, list[MessageHistoryRow]] = {}
    sent: set[int] = set()
    for row in [*rows, *rated]:
        if row["internal_id"] in sent:
            continue
        sent.add(row["internal_id"])
        if row.get("is_internal"):
            removed.setdefault(row["conversation_id"], []).append(row["internal_id"])
        else:
            by_conversation.setdefault(row["conversation_id"], []).append(row)

    changes: list[ConversationMessages] = []
    for conversation_id in sorted(set(by_conversation) | set(removed)):
        changes.append(
            ConversationMessages(
                conversation_id=conversation_id,
                messages=await _serialize_messages(
                    ctx, by_conversation.get(conversation_id, [])
                ),
                removed_message_ids=removed.get(conversation_id, []),
            )
        )
    for conversation_id in sorted(fresh):
        (
            page,
            has_more_before,
            _,
        ) = await ctx.db.message_history.get_conversation_messages_paginated(
            conversation_id=conversation_id,
            limit=FRESH_HISTORY_MESSAGES,
            include_subconversations=False,
        )
        changes.append(
            ConversationMessages(
                conversation_id=conversation_id,
                messages=await _serialize_messages(ctx, page),
                removed_message_ids=[],
                replaces_history=True,
                has_more_before=has_more_before,
            )
        )
    return changes, through, has_more


async def _conversation_rows(
    ctx: SyncContext, conversation_ids: Collection[str] | None
) -> list[ConversationRow]:
    """List rows for ``conversation_ids`` the caller owns; None means the newest."""
    summaries, _ = await ctx.db.message_history.get_conversation_summaries(
        limit=BOOTSTRAP_CONVERSATIONS
        if conversation_ids is None
        else max(len(conversation_ids), 1),
        conversation_ids=conversation_ids,
        include_subconversations=False,
        owner_user_ids=ctx.owner_user_ids,
    )
    ids = [summary["conversation_id"] for summary in summaries]
    kinds = await ctx.db.conversation_threads.kinds_for(ids)
    viewer = viewer_for(ctx.config, ctx.user_id)
    threads = [
        thread
        for conversation_id in ids
        if kinds.get(conversation_id) == "thread"
        and (thread := await ctx.db.conversation_threads.get_thread(conversation_id))
        is not None
    ]
    thread_summaries = {
        summary.conversation_id: thread_summary_response(
            summary, thread.home_conversation_id
        )
        for thread, summary in zip(
            threads,
            await summarize_threads(
                ctx.db, threads, turn_running=turn_running_in(ctx.hub), viewer=viewer
            ),
            strict=True,
        )
    }
    unread_ids = [i for i in ids if kinds.get(i) in {"home", "thread"}]
    unread = (
        await ctx.db.conversation_threads.unread_counts(
            unread_ids, viewer_user_id=viewer.user_id, viewer_keys=viewer.keys
        )
        if unread_ids
        else {}
    )
    return [
        ConversationRow(
            conversation_id=summary["conversation_id"],
            kind=kinds.get(summary["conversation_id"], "conversation"),
            last_message=summary["last_message"],
            summary=summary["summary"],
            last_timestamp=summary["last_timestamp"],
            message_count=summary["message_count"],
            unread_count=unread.get(summary["conversation_id"])
            if summary["conversation_id"] in unread_ids
            else None,
            thread=thread_summaries.get(summary["conversation_id"]),
        )
        for summary in summaries
    ]


async def _pending_delegations(
    ctx: SyncContext, conversation_ids: Collection[str]
) -> dict[str, list[PendingDelegation]]:
    return {
        conversation_id: [
            pending_delegation_response(entry)
            for entry in await ctx.db.delegation_runs.list_pending_for_conversation(
                conversation_id=conversation_id
            )
        ]
        for conversation_id in sorted(conversation_ids)
    }


def _running_in_hub(ctx: SyncContext) -> set[str]:
    return set(ctx.hub.running_conversation_ids(ctx.owner_user_ids))


def _turn_progress(
    ctx: SyncContext, focus: Collection[str]
) -> tuple[list[TurnProgress], dict[str, int]]:
    progress: list[TurnProgress] = []
    seqs: dict[str, int] = {}
    for conversation_id in sorted(focus):
        seqs[conversation_id] = ctx.hub.latest_seq(conversation_id)
        progress.extend(
            TurnProgress(
                conversation_id=conversation_id,
                turn_id=turn.turn_id,
                started_at=turn.started_at,
                progress_seq=turn.latest_seq,
                parts=list(turn.progress),
            )
            for turn in ctx.hub.active_turns(conversation_id)
            if turn.status == "running" and turn.user_id == ctx.user_id
        )
    return progress, seqs


async def build_sync_response(ctx: SyncContext, query: SyncQuery) -> SyncResponse:
    """Everything the client lacks, given what its query says it holds."""
    through = await ctx.db.sync.current_cursor()
    # A cursor above the newest commit came from another database (a restored
    # backup, a test reset): continuing from it would silently skip changes.
    bootstrap = query.cursor is None or query.cursor > through
    after = 0 if query.cursor is None or bootstrap else query.cursor

    focus = await ctx.db.message_history.owned_conversation_ids(
        query.focus, ctx.owner_user_ids
    )
    messages, through, has_more = await _focus_messages(
        ctx,
        replace(query, fresh=query.fresh | (query.focus if bootstrap else frozenset())),
        focus,
        after,
        through,
    )

    running = await ctx.db.message_history.owned_conversation_ids(
        _running_in_hub(ctx), ctx.owner_user_ids
    )
    if bootstrap:
        rows = await _conversation_rows(ctx, None)
        removed: set[str] = set()
        confirmations_changed = True
        delegation_ids = await ctx.db.delegation_runs.conversation_ids_with_pending()
    else:
        viewer = viewer_for(ctx.config, ctx.user_id)
        touched = (
            await ctx.db.sync.changed_conversation_ids(after, through)
            | await ctx.db.sync.read_marker_conversation_ids(
                {viewer.user_id, *viewer.keys}, after, through
            )
            | (running ^ query.running)
        )
        rows = await _conversation_rows(ctx, touched) if touched else []
        removed = (
            await ctx.db.sync.removed_conversation_ids(
                ctx.owner_user_ids, after, through
            )
        ) - {row.conversation_id for row in rows}
        confirmations_changed = await ctx.db.sync.confirmations_changed(
            ctx.owner_user_ids, after, through
        )
        delegation_ids = await ctx.db.sync.delegation_conversation_ids(after, through)

    owned_delegation_ids = await ctx.db.message_history.owned_conversation_ids(
        delegation_ids, ctx.owner_user_ids
    )
    now = datetime.now(UTC)
    confirmations = (
        [
            pending_confirmation_response(row, now)
            for row in await ctx.db.confirmation_requests.list_pending_for_user(
                ctx.user_id
            )
            if row["expires_at"] > now
        ]
        if confirmations_changed
        else None
    )
    progress, focus_seq = _turn_progress(ctx, focus)
    home = (
        await find_home(ctx.db, ctx.config, user_id=ctx.user_id, interface_type="web")
        if ctx.config.conversation_threads.enabled
        else None
    )
    return SyncResponse(
        cursor=through,
        has_more=has_more,
        bootstrap=bootstrap,
        server_time=now,
        home_conversation_id=home,
        conversations=rows,
        removed_conversation_ids=sorted(removed),
        messages=messages,
        confirmations=confirmations,
        delegations=await _pending_delegations(ctx, owned_delegation_ids),
        running_conversation_ids=sorted(running),
        active_turns=progress,
        focus_seq=focus_seq,
    )


def has_news(response: SyncResponse, query: SyncQuery) -> bool:
    """Whether ``response`` tells the client anything its query says it lacks."""
    return bool(
        response.bootstrap
        or response.has_more
        or response.cursor != query.cursor
        or response.messages
        or response.conversations
        or response.removed_conversation_ids
        or response.confirmations is not None
        or response.delegations
        or set(response.running_conversation_ids) != query.running
        or any(
            seq != query.focus_seq.get(conversation_id, -1)
            for conversation_id, seq in response.focus_seq.items()
        )
    )


def _hub_moved(ctx: SyncContext, query: SyncQuery) -> bool:
    """Whether the hub now differs from what the client has seen of it.

    Cheap and in-memory, so a wait woken by an unrelated conversation's
    progress does not rebuild a response from the database.
    """
    if any(
        ctx.hub.latest_seq(conversation_id) != query.focus_seq.get(conversation_id, -1)
        for conversation_id in query.focus
    ):
        return True
    running = _running_in_hub(ctx)
    return bool((running ^ query.running) & (running | query.running))


async def _wait_for_change(
    ctx: SyncContext,
    query: SyncQuery,
    *,
    db_generation: int,
    hub_generation: int,
    timeout: float,
) -> bool:
    """Wait until a commit, or until the hub moves for this client.

    Returns False when ``timeout`` passed with nothing to report.
    """
    db_wakeup = sync_wakeup_for(ctx.db.engine)
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while True:
        remaining = deadline - loop.time()
        if remaining <= 0:
            return False
        db_wait = asyncio.create_task(db_wakeup.wait_beyond(db_generation, remaining))
        hub_wait = asyncio.create_task(
            ctx.hub.changes.wait_beyond(hub_generation, remaining)
        )
        try:
            await asyncio.wait({db_wait, hub_wait}, return_when=asyncio.FIRST_COMPLETED)
        finally:
            db_wait.cancel()
            hub_wait.cancel()
        if db_wakeup.generation != db_generation:
            return True
        if ctx.hub.changes.generation == hub_generation:
            return False
        hub_generation = ctx.hub.changes.generation
        if _hub_moved(ctx, query):
            # Let a burst of deltas land before answering.
            await asyncio.sleep(PROGRESS_COALESCE_SECONDS)
            return True


async def long_poll(ctx: SyncContext, query: SyncQuery, wait: float) -> SyncResponse:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + wait
    while True:
        db_generation = sync_wakeup_for(ctx.db.engine).generation
        hub_generation = ctx.hub.changes.generation
        response = await build_sync_response(ctx, query)
        remaining = deadline - loop.time()
        if remaining <= 0 or has_news(response, query):
            return response
        if not await _wait_for_change(
            ctx,
            query,
            db_generation=db_generation,
            hub_generation=hub_generation,
            timeout=remaining,
        ):
            return response


def _query(
    cursor: int | None,
    focus: str | None,
    fresh: str | None,
    running: str | None,
    focus_seq: str | None,
    limit: int,
) -> SyncQuery:
    return SyncQuery(
        cursor=cursor,
        focus=_split(focus),
        fresh=_split(fresh) & _split(focus),
        running=_split(running),
        focus_seq=_parse_focus_seq(focus_seq),
        limit=limit,
    )


@sync_api_router.get("/v1/sync")
async def sync(
    request: Request,
    current_user: Annotated[dict, Depends(get_current_user)],
    db: Annotated[Database, Depends(get_db)],
    attachment_registry: Annotated[
        "AttachmentRegistry", Depends(get_attachment_registry)
    ],
    cursor: Annotated[
        int | None,
        Query(description="The cursor of the last response; omit to bootstrap"),
    ] = None,
    focus: Annotated[
        str | None, Query(description="Comma-separated open conversation ids")
    ] = None,
    fresh: Annotated[
        str | None,
        Query(description="Focus conversations to send the latest page of history for"),
    ] = None,
    running: Annotated[
        str | None,
        Query(description="Conversation ids the last response said were running"),
    ] = None,
    focus_seq: Annotated[
        str | None,
        Query(description="conversation_id:seq pairs from the last response"),
    ] = None,
    wait: Annotated[
        float,
        Query(ge=0, le=MAX_WAIT_SECONDS, description="Seconds to hold for a change"),
    ] = 0,
    limit: Annotated[int, Query(ge=1, le=5_000)] = DEFAULT_MESSAGE_LIMIT,
) -> SyncResponse:
    """Bring the client up to date; with ``wait``, hold until there is news."""
    ctx = await _context(request, current_user, db, attachment_registry)
    query = _query(cursor, focus, fresh, running, focus_seq, limit)
    if wait <= 0:
        return await build_sync_response(ctx, query)
    return await long_poll(ctx, query, wait)


def _next_query(query: SyncQuery, response: SyncResponse) -> SyncQuery:
    return SyncQuery(
        cursor=response.cursor,
        focus=query.focus,
        fresh=frozenset(),
        running=frozenset(response.running_conversation_ids),
        focus_seq=dict(response.focus_seq),
        limit=query.limit,
    )


async def _stream_frames(
    request: Request, ctx: SyncContext, query: SyncQuery
) -> AsyncIterator[str]:
    shutdown_event: asyncio.Event | None = getattr(
        request.app.state, "shutdown_event", None
    )
    while shutdown_event is None or not shutdown_event.is_set():
        response = await long_poll(ctx, query, STREAM_HEARTBEAT_SECONDS)
        if await request.is_disconnected():
            return
        if has_news(response, query):
            yield f"event: sync\ndata: {response.model_dump_json()}\n\n"
            query = _next_query(query, response)
        else:
            yield f"event: heartbeat\ndata: {json.dumps({'cursor': response.cursor})}\n\n"


@sync_api_router.get("/v1/sync/stream")
async def sync_stream(
    request: Request,
    current_user: Annotated[dict, Depends(get_current_user)],
    db: Annotated[Database, Depends(get_db)],
    attachment_registry: Annotated[
        "AttachmentRegistry", Depends(get_attachment_registry)
    ],
    cursor: int | None = None,
    focus: str | None = None,
    fresh: str | None = None,
    running: str | None = None,
    focus_seq: str | None = None,
    limit: Annotated[int, Query(ge=1, le=5_000)] = DEFAULT_MESSAGE_LIMIT,
) -> StreamingResponse:
    """The sync request repeated over one connection.

    Each ``sync`` frame is a full sync response, sent as soon as there is news;
    a ``heartbeat`` frame follows every quiet ``STREAM_HEARTBEAT_SECONDS``. The
    stream never replays: a client that loses it resumes with a sync request
    from the last cursor it applied.
    """
    ctx = await _context(request, current_user, db, attachment_registry)
    query = _query(cursor, focus, fresh, running, focus_seq, limit)

    async def frames() -> AsyncIterator[str]:
        # Flush the response head at once, so a front door that waits for the
        # first byte does not hold the connection open silently.
        yield "event: heartbeat\ndata: {}\n\n"
        async for frame in _stream_frames(request, ctx, query):
            yield frame

    return StreamingResponse(
        frames(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )
