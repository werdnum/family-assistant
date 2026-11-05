"""Home and its threads, for the web and iOS clients.

See docs/design/one-thread-chat-ux.md.
"""

import uuid
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Annotated

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel, Field
from sqlalchemy.exc import IntegrityError

from family_assistant.config_models import AppConfig
from family_assistant.processing import ProcessingService
from family_assistant.security.taint import TurnTaintState
from family_assistant.storage.database import Database, DatabaseTransaction
from family_assistant.storage.delegation_runs import TERMINAL_DELEGATION_STATUSES
from family_assistant.storage.repositories.conversation_threads import (
    ConversationThread,
)
from family_assistant.storage.types import MessageHistoryRow
from family_assistant.threads.boundary import (
    THREAD_START_MARKER,
    BoundaryDestination,
    CopiedTurns,
    CrossBoundaryCopyRefusedError,
    MovedTurn,
    move_turn_across_boundary,
)
from family_assistant.threads.carry import (
    SuggestionSource,
    carry_candidates,
    carry_window,
    suggest_carried_turns,
)
from family_assistant.threads.creation import (
    CONTINUE_CONTEXT_TURNS,
    NewThread,
    create_thread,
    title_from_text,
)
from family_assistant.threads.echo import announce_home_change
from family_assistant.threads.home import (
    ensure_web_home,
    find_home,
    home_owner_id,
    viewer_for,
)
from family_assistant.threads.listing import list_home_threads, summarize_threads
from family_assistant.web.conversation_stream_hub import ConversationStreamHub
from family_assistant.web.dependencies import (
    get_current_user,
    get_db,
    get_processing_service,
    get_web_chat_interface,
)
from family_assistant.web.models import ChatAttachmentRequest
from family_assistant.web.routers.chat_api import (
    ChatTurnRequest,
    ChatTurnResponse,
    ThreadSummaryResponse,
    ensure_caller_owns_conversation,
    get_stream_hub,
    resolve_requested_model_tier,
    start_chat_turn,
    thread_summary_response,
    turn_running_in,
)

if TYPE_CHECKING:
    from family_assistant.processing.history_relevance import TurnRelevance
    from family_assistant.web.web_chat_interface import WebChatInterface

threads_api_router = APIRouter()

CONTINUED_THREAD_TITLE = "Continued from Home"
"""The title of a thread continued with neither a title nor an opening message."""


class HomeResponse(BaseModel):
    """The caller's Home conversation."""

    enabled: bool = Field(
        ..., description="Whether threads are enabled. When false there is no Home."
    )
    conversation_id: str | None = Field(
        default=None,
        description="The caller's Home conversation, designated on first request",
    )


class HomeThreadsResponse(BaseModel):
    """The threads of the caller's Home, newest first."""

    home_conversation_id: str | None
    threads: list[ThreadSummaryResponse]


class MarkReadRequest(BaseModel):
    """How far the caller has read a conversation."""

    through_message_id: int | None = Field(
        default=None,
        description=(
            "The newest message the caller has read. Omit to mark everything "
            "read. A position behind the caller's current one is ignored."
        ),
    )


class MarkReadResponse(BaseModel):
    """The caller's unread count once the read is recorded."""

    conversation_id: str
    unread_count: int


class StartThreadRequest(BaseModel):
    """Body for starting a thread of the caller's Home.

    One call serves "New thread", send-as-thread and "Continue in a thread":
    name an ``anchor_message_id`` to continue from a Home message.
    """

    profile_id: str = Field(..., description="The profile the thread runs on")
    prompt: str | None = Field(
        default=None,
        description=(
            "The user's opening message, stored as their own words and run as "
            "the thread's first turn. Required unless continuing with context, "
            "where the copied Home turns can stand on their own"
        ),
    )
    turn_id: str | None = Field(
        default=None,
        description=(
            "Client-supplied UUIDv4 identifying this start, and the id of its "
            "first turn, as on POST /v1/chat/turns. A retry with the same "
            "turn_id returns the thread it already started instead of starting "
            "another. Generated when omitted, which gives up that guarantee"
        ),
    )
    title: str | None = Field(
        default=None,
        description=(
            "The thread's title. Derived from the opening message when "
            f"omitted, or '{CONTINUED_THREAD_TITLE}' when there is none"
        ),
    )
    anchor_message_id: int | None = Field(
        default=None,
        description=(
            "Continue in a thread: the Home message the thread grows from. It "
            "becomes the thread's anchor, for the client's 'Started from' quote"
        ),
    )
    include_context: bool = Field(
        default=True,
        description=(
            "With anchor_message_id: copy the anchor's turn and up to "
            f"{CONTINUE_CONTEXT_TURNS - 1} Home turns before it into the thread. "
            "Refused with 409 on a profile that does not receive household "
            "context (see receives_household_context on /v1/profiles); send "
            "false there to start from the opening message alone"
        ),
    )
    carried_turn_ids: list[str] | None = Field(
        default=None,
        description=(
            "With include_context: which of the turns GET "
            "/v1/chat/home/threads/continue-preview offered to copy. Must "
            "include the anchor's turn, and may name only offered turns (422 "
            "otherwise). Omitted, every offered turn is copied"
        ),
    )
    model_tier: str | None = Field(
        default=None,
        description="Optional model tier for the thread, from the profile's model_tiers",
    )
    attachments: list[ChatAttachmentRequest] | None = Field(
        default=None, description="Attachments sent with the opening message"
    )


class StartThreadResponse(BaseModel):
    """The thread that was started, and its first turn if it has one."""

    thread: ThreadSummaryResponse
    turn: ChatTurnResponse | None = Field(
        default=None,
        description=(
            "The first turn, running the opening message. Subscribe to "
            "/v1/chat/conversations/{thread}/stream from turn.first_seq to "
            "watch it, as after POST /v1/chat/turns. None when the thread was "
            "continued with no opening message: it waits for the user"
        ),
    )
    copied_message_ids: list[int] = Field(
        default_factory=list,
        description="The thread's copies of the Home rows it was continued with",
    )


class CarryCandidateResponse(BaseModel):
    """A Home turn "Continue in a thread" can copy."""

    turn_id: str
    started_at: datetime
    user_text: str | None = Field(
        description="What the user wrote, shortened; null for a turn they didn't start"
    )
    reply_text: str | None = Field(description="The turn's final reply, shortened")
    is_anchor: bool = Field(description="The turn of the message being continued")
    is_echo: bool = Field(description="A finished thread's outcome, echoed into Home")
    suggested: bool = Field(description="Ticked to start with")


class ContinuePreviewResponse(BaseModel):
    """What "Continue in a thread" would copy from Home, oldest first."""

    anchor_message_id: int
    turns: list[CarryCandidateResponse]
    suggested_by: SuggestionSource = Field(
        description=(
            "'assistant' when a classifier picked the turns that go with the "
            "anchor; 'default' when every turn is ticked"
        )
    )


class MoveTurnRequest(BaseModel):
    """Which Home turn to move into a thread."""

    message_id: int = Field(
        ...,
        description=(
            "Any message of the Home turn to move. The whole turn moves: the "
            "user's message, its tool rows and the reply"
        ),
    )


class MoveTurnResponse(BaseModel):
    """Where a moved turn went."""

    thread: ThreadSummaryResponse
    marker_message_id: int = Field(
        ...,
        description=(
            "The Home message marking the move. It carries moved_to_thread, "
            "naming the thread the turn is in now"
        ),
    )
    moved_message_ids: list[int] = Field(
        ..., description="The turn's messages, as they are now in the thread"
    )


def _app_config(request: Request) -> AppConfig:
    return request.app.state.config


def _user_id(current_user: dict) -> str:
    user_id = current_user.get("user_identifier")
    if not isinstance(user_id, str) or not user_id:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED)
    return user_id


async def _owned_thread(
    request: Request,
    current_user: dict,
    db: Database,
    conversation_id: str,
) -> ConversationThread:
    """The caller's own thread, or 404 for anything else.

    Ownership is the check every conversation endpoint applies, so marking a
    thread done or read is allowed exactly where reading or sending into it is.
    """
    if not _app_config(request).conversation_threads.enabled:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND)
    thread = await db.conversation_threads.get_thread(conversation_id)
    if thread is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND)
    await ensure_caller_owns_conversation(request, current_user, conversation_id)
    return thread


@threads_api_router.get("/v1/chat/home")
async def get_home(
    request: Request,
    current_user: Annotated[dict, Depends(get_current_user)],
    db_context: Annotated[Database, Depends(get_db)],
) -> HomeResponse:
    """The caller's web Home, designated the first time it is asked for."""
    config = _app_config(request)
    if not config.conversation_threads.enabled:
        return HomeResponse(enabled=False)
    home = await ensure_web_home(
        db_context, config, user_id=_user_id(current_user), now=datetime.now(UTC)
    )
    return HomeResponse(enabled=True, conversation_id=home)


@threads_api_router.get("/v1/chat/home/threads")
async def get_home_threads(
    request: Request,
    current_user: Annotated[dict, Depends(get_current_user)],
    db_context: Annotated[Database, Depends(get_db)],
) -> HomeThreadsResponse:
    """The threads of the caller's web Home, newest first.

    Only the caller's own Home is ever read: it is looked up from their
    identity, never named by the request.
    """
    config = _app_config(request)
    if not config.conversation_threads.enabled:
        return HomeThreadsResponse(home_conversation_id=None, threads=[])
    user_id = _user_id(current_user)
    home = await find_home(db_context, config, user_id=user_id, interface_type="web")
    if home is None:
        return HomeThreadsResponse(home_conversation_id=None, threads=[])
    threads = await list_home_threads(
        db_context,
        home,
        turn_running=turn_running_in(get_stream_hub(request)),
        viewer=viewer_for(config, user_id),
    )
    return HomeThreadsResponse(
        home_conversation_id=home,
        threads=[thread_summary_response(thread, home) for thread in threads],
    )


@threads_api_router.post("/v1/chat/home/threads/{conversation_id}/done")
async def mark_thread_done(
    conversation_id: str,
    request: Request,
    current_user: Annotated[dict, Depends(get_current_user)],
    db_context: Annotated[Database, Depends(get_db)],
) -> ThreadSummaryResponse:
    """Close one of the caller's threads as done.

    Nothing is echoed into Home: the thread's last message may be a question or
    a progress update rather than an outcome, so the card just shows that the
    user closed it. Anything that arrives in the thread later reopens it.
    """
    config = _app_config(request)
    user_id = _user_id(current_user)
    thread = await _owned_thread(request, current_user, db_context, conversation_id)
    await db_context.conversation_threads.close_thread(
        conversation_id,
        outcome="done",
        closed_by="user",
        turn_id=None,
        now=datetime.now(UTC),
    )
    hub = get_stream_hub(request)
    # Under the caller's own login id, which is what their activity stream
    # subscribes under, as every conversation endpoint publishes.
    await announce_home_change(
        hub,
        home_conversation_id=thread.home_conversation_id,
        user_id=user_id,
        reason="thread_closed",
    )
    await hub.publish_activity(conversation_id, user_id=user_id, reason="thread_closed")
    closed = await db_context.conversation_threads.get_thread(conversation_id)
    assert closed is not None
    [summary] = await summarize_threads(
        db_context,
        [closed],
        turn_running=turn_running_in(hub),
        viewer=viewer_for(config, user_id),
    )
    return thread_summary_response(summary, thread.home_conversation_id)


@threads_api_router.post("/v1/chat/conversations/{conversation_id}/read")
async def mark_conversation_read(
    conversation_id: str,
    payload: MarkReadRequest,
    request: Request,
    current_user: Annotated[dict, Depends(get_current_user)],
    db_context: Annotated[Database, Depends(get_db)],
) -> MarkReadResponse:
    """Record how far the caller has read one of their threads, or their Home.

    The marker is the caller's own: another member of the household reading
    the same conversation keeps their own count. Ordinary conversations have
    no unread count, so they have no marker either.
    """
    config = _app_config(request)
    user_id = _user_id(current_user)
    viewer = viewer_for(config, user_id)
    home = (
        await find_home(db_context, config, user_id=user_id, interface_type="web")
        if config.conversation_threads.enabled
        else None
    )
    if home != conversation_id:
        await _owned_thread(request, current_user, db_context, conversation_id)
    latest = (
        await db_context.message_history.latest_visible_messages([conversation_id])
    ).get(conversation_id)
    if latest is not None:
        # Bounded by what exists, so a position ahead of the conversation
        # cannot mark messages read before they arrive.
        through = (
            latest.internal_id
            if payload.through_message_id is None
            else min(payload.through_message_id, latest.internal_id)
        )
        await db_context.conversation_threads.mark_read(
            conversation_id,
            viewer_user_id=viewer.user_id,
            through_message_id=through,
            now=datetime.now(UTC),
        )
        await get_stream_hub(request).publish_activity(
            conversation_id, user_id=user_id, reason="read"
        )
    unread = await db_context.conversation_threads.unread_counts(
        [conversation_id], viewer_user_id=viewer.user_id, viewer_keys=viewer.keys
    )
    return MarkReadResponse(
        conversation_id=conversation_id,
        unread_count=unread.get(conversation_id, 0),
    )


def _local_profile(request: Request, profile_id: str) -> ProcessingService:
    """A profile a thread can run on: local, as direct chat requires."""
    registry = getattr(request.app.state, "processing_services", {})
    service = registry.get(profile_id)
    if not isinstance(service, ProcessingService):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Profile '{profile_id}' is not available for a thread.",
        )
    return service


def _derived_title(prompt: str | None) -> str:
    """A thread's title from the user's opening message: its first line, shortened."""
    if prompt is None:
        return CONTINUED_THREAD_TITLE
    return title_from_text(prompt)


async def _home_turn_row(
    db: Database, home: str, message_id: int, hub: ConversationStreamHub
) -> MessageHistoryRow:
    """A message of a finished, top-level Home turn, or the 4xx saying why not.

    A message anywhere but the caller's Home is 404, however it exists.
    """
    row = await db.message_history.get_row_by_internal_id(message_id)
    if (
        row is None
        or row["conversation_id"] != home
        or row["subconversation_id"] is not None
    ):
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="No such message in your Home.",
        )
    if row["turn_id"] is None or row["moved_to_conversation_id"] is not None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="This message is not part of a turn that can be carried.",
        )
    turn = hub.get_turn(home, row["turn_id"])
    if (turn is not None and turn.status == "running") or not await _turn_has_ended(
        db, row["turn_id"]
    ):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="That reply is still in progress.",
        )
    return row


async def _turn_has_ended(db: Database, turn_id: str) -> bool:
    """Whether a turn has finished: with a reply, or the error row a failed turn leaves.

    A turn interrupted by a restart is in no hub while it waits to be resumed,
    so the stored outcome is what says it is over.
    """
    return await db.message_history.has_terminal_reply_for_turn(turn_id) or any(
        turn_row["role"] == "error"
        for turn_row in await db.message_history.get_rows_by_turn_id(turn_id)
    )


async def _at_rest(
    db: Database, hub: ConversationStreamHub, conversation_id: str
) -> bool:
    """Whether no turn is under way in a conversation, live or waiting to resume.

    A turn interrupted by a restart is in no hub, so the stored record is
    read too: the newest message must belong to a turn that has ended, not be
    the user's unanswered message or an assistant step that called a tool.
    """
    if hub.has_running_turn(conversation_id):
        return False
    latest = (await db.message_history.latest_visible_messages([conversation_id])).get(
        conversation_id
    )
    return (
        latest is None
        or latest.turn_id is None
        or await _turn_has_ended(db, latest.turn_id)
    )


async def _thread_summary(
    db: Database,
    config: AppConfig,
    hub: ConversationStreamHub,
    *,
    user_id: str,
    conversation_id: str,
) -> ThreadSummaryResponse:
    thread = await db.conversation_threads.get_thread(conversation_id)
    assert thread is not None
    [summary] = await summarize_threads(
        db,
        [thread],
        turn_running=turn_running_in(hub),
        viewer=viewer_for(config, user_id),
    )
    return thread_summary_response(summary, thread.home_conversation_id)


def _continue_relevance(request: Request) -> TurnRelevance | None:
    """Jev's turn relevance for picking what a continuation carries, if configured."""
    return getattr(request.app.state, "continue_turn_relevance", None)


@threads_api_router.get("/v1/chat/home/threads/continue-preview")
async def preview_continue(
    anchor_message_id: int,
    request: Request,
    current_user: Annotated[dict, Depends(get_current_user)],
    db_context: Annotated[Database, Depends(get_db)],
) -> ContinuePreviewResponse:
    """What "Continue in a thread" from a Home message would bring along.

    The anchor's turn and the Home turns just before it (``CONTINUE_CONTEXT_TURNS``
    in all), oldest first, each
    marked whether it is ticked to start with: the anchor always, and the
    earlier turns a classifier judges to go with it, or all of them when there
    is no classifier or it gives no answer. Send the ticked ones back as
    ``carried_turn_ids`` on POST /v1/chat/home/threads.

    404 while threads are disabled, and for an anchor outside the caller's
    Home; 409 for an anchor that cannot be continued, as on the start.
    """
    config = _app_config(request)
    if not config.conversation_threads.enabled:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND)
    user_id = _user_id(current_user)
    home = await ensure_web_home(
        db_context, config, user_id=user_id, now=datetime.now(UTC)
    )
    anchor = await _home_turn_row(
        db_context, home, anchor_message_id, get_stream_hub(request)
    )
    assert anchor["turn_id"] is not None
    candidates = await carry_candidates(
        db_context,
        interface_type=anchor["interface_type"],
        home=home,
        anchor_turn_id=anchor["turn_id"],
    )
    suggestion = await suggest_carried_turns(candidates, _continue_relevance(request))
    return ContinuePreviewResponse(
        anchor_message_id=anchor_message_id,
        turns=[
            CarryCandidateResponse(
                turn_id=candidate.turn_id,
                started_at=candidate.started_at,
                user_text=candidate.user_text,
                reply_text=candidate.reply_text,
                is_anchor=candidate.is_anchor,
                is_echo=candidate.is_echo,
                suggested=candidate.turn_id in suggestion.suggested_turn_ids,
            )
            for candidate in suggestion.candidates
        ],
        suggested_by=suggestion.source,
    )


@threads_api_router.post("/v1/chat/home/threads")
async def start_home_thread(
    payload: StartThreadRequest,
    request: Request,
    current_user: Annotated[dict, Depends(get_current_user)],
    db_context: Annotated[Database, Depends(get_db)],
    default_processing_service: Annotated[
        ProcessingService, Depends(get_processing_service)
    ],
    web_chat_interface: Annotated["WebChatInterface", Depends(get_web_chat_interface)],
) -> StartThreadResponse:
    """Start a thread of the caller's Home on a profile they choose.

    "New thread" and send-as-thread name a profile and an opening message.
    "Continue in a thread" also names the Home message to continue from; with
    ``include_context`` the anchor's turn and the Home turns just before it are
    copied in, keeping their taint, which only a profile receiving household
    context accepts (409 otherwise).

    The opening message runs at once as the thread's first turn, as the user's
    own turn: subscribe to the thread's stream from ``turn.first_seq``. Picking
    the profile is the user's choice, as it is in direct chat, so the target's
    delegation policy -- which admits the assistant's ``start_thread`` -- does
    not apply.

    404 while threads are disabled, and for an anchor outside the caller's Home.
    """
    config = _app_config(request)
    if not config.conversation_threads.enabled:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND)
    user_id = _user_id(current_user)
    hub = get_stream_hub(request)
    prompt = (payload.prompt or "").strip() or None
    if prompt is None and (
        payload.anchor_message_id is None or not payload.include_context
    ):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="A thread needs an opening message.",
        )
    if prompt is None and payload.attachments:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="Attachments are sent with an opening message.",
        )
    profile = _local_profile(request, payload.profile_id)
    resolve_requested_model_tier(profile, payload.model_tier)
    home = await ensure_web_home(
        db_context, config, user_id=user_id, now=datetime.now(UTC)
    )
    turn_id = payload.turn_id or str(uuid.uuid4())
    # Named after the request, so a retry finds the thread it started whether
    # or not that start wrote a turn.
    conversation_id = f"web_conv_{uuid.uuid5(uuid.NAMESPACE_URL, f'{home}/{turn_id}')}"

    copied: list[int] = []
    created = await db_context.conversation_threads.get_thread(conversation_id) is None
    if created:
        if await db_context.message_history.get_user_row_by_turn_id(turn_id):
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="That turn_id is already in use.",
            )
        try:
            copied = await _create_user_thread(
                db_context,
                config,
                hub,
                payload=payload,
                prompt=prompt,
                profile=profile,
                user_id=user_id,
                home=home,
                conversation_id=conversation_id,
            )
        except IntegrityError:
            # An overlapping retry of the same start created it first.
            if (
                await db_context.conversation_threads.get_thread(conversation_id)
                is None
            ):
                raise
            created = False

    turn: ChatTurnResponse | None = None
    if prompt is not None:
        turn_request = ChatTurnRequest(
            turn_id=turn_id,
            conversation_id=conversation_id,
            prompt=prompt,
            interface_type="web",
            attachments=payload.attachments,
            model_tier=payload.model_tier,
        )
        try:
            turn = await start_chat_turn(
                turn_request,
                request,
                current_user,
                default_processing_service=default_processing_service,
                web_chat_interface=web_chat_interface,
            )
        except HTTPException as refusal:
            # The turn refuses its request -- an unusable attachment, say --
            # before writing anything, so a thread made for it would be left
            # empty, under an id the caller may never learn.
            if created and refusal.status_code < status.HTTP_500_INTERNAL_SERVER_ERROR:
                await db_context.conversation_threads.discard_thread(conversation_id)
            raise
    if turn is None:
        await hub.publish_activity(
            conversation_id, user_id=user_id, reason="thread_started"
        )
    await announce_home_change(
        hub, home_conversation_id=home, user_id=user_id, reason="thread_started"
    )
    return StartThreadResponse(
        thread=await _thread_summary(
            db_context, config, hub, user_id=user_id, conversation_id=conversation_id
        ),
        turn=turn,
        copied_message_ids=copied,
    )


async def _create_user_thread(
    db: Database,
    config: AppConfig,
    hub: ConversationStreamHub,
    *,
    payload: StartThreadRequest,
    prompt: str | None,
    profile: ProcessingService,
    user_id: str,
    home: str,
    conversation_id: str,
) -> list[int]:
    """Record a thread the user started, with the Home turns it continues."""
    seed: CopiedTurns | None = None
    if payload.anchor_message_id is not None:
        anchor = await _home_turn_row(db, home, payload.anchor_message_id, hub)
        if payload.include_context:
            assert anchor["turn_id"] is not None
            window = await carry_window(
                db,
                interface_type=anchor["interface_type"],
                home=home,
                anchor_turn_id=anchor["turn_id"],
            )
            seed = CopiedTurns(
                source_interface_type=anchor["interface_type"],
                source_conversation_id=home,
                turn_ids=_chosen_turns(
                    window, anchor["turn_id"], payload.carried_turn_ids
                ),
            )
    title = (payload.title or "").strip() or _derived_title(prompt)
    new_thread = NewThread(
        conversation_id=conversation_id,
        home_conversation_id=home,
        interface_type="web",
        owner_user_id=home_owner_id(config, user_id),
        user_id=user_id,
        profile=profile.service_config,
        title=title,
        # The user wrote it, or it is fixed text: either way it is the user's.
        title_taint=TurnTaintState.empty().to_metadata(),
        anchor_message_id=payload.anchor_message_id,
    )
    now = datetime.now(UTC)

    async def _create(txn: DatabaseTransaction) -> list[int]:
        copied_ids = await create_thread(
            txn, thread=new_thread, seed=seed, turn_id=str(uuid.uuid4()), now=now
        )
        await txn.message_history.add_thread_start_marker(
            interface_type=new_thread.interface_type,
            home_conversation_id=home,
            thread_conversation_id=conversation_id,
            content=THREAD_START_MARKER,
            now=now,
        )
        return copied_ids

    try:
        return await db.atomic(_create)
    except CrossBoundaryCopyRefusedError as refusal:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(
                f"Profile '{profile.service_config.id}' does not receive household "
                "context, so Home conversation can't be carried into it. Continue "
                "without context, or choose another profile."
            ),
        ) from refusal


def _chosen_turns(
    window: list[str], anchor_turn_id: str, chosen: list[str] | None
) -> tuple[str, ...]:
    """The turns of ``window`` the caller chose to carry, in window order."""
    if chosen is None:
        return tuple(window)
    if anchor_turn_id not in chosen:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="The exchange you're continuing has to come along.",
        )
    if not set(chosen) <= set(window):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=(
                "Some of the messages you picked can't be carried any more. "
                "Close this and try again."
            ),
        )
    return tuple(turn_id for turn_id in window if turn_id in chosen)


@threads_api_router.post("/v1/chat/home/threads/{conversation_id}/moved-turns")
async def move_turn_to_thread(
    conversation_id: str,
    payload: MoveTurnRequest,
    request: Request,
    current_user: Annotated[dict, Depends(get_current_user)],
    db_context: Annotated[Database, Depends(get_db)],
) -> MoveTurnResponse:
    """Move a whole Home turn into one of the caller's threads.

    The turn -- the user's message, its tool rows and the reply -- leaves Home
    and becomes the newest turn in the thread, keeping its taint. Home keeps a
    marker in its place (``moved_to_thread`` on the message) so its readers see
    where the turn went, and Home's assistant no longer sees the turn.

    404 while threads are disabled, for a thread or message that isn't the
    caller's, and for a message outside their Home. 409 for a thread whose
    profile does not receive household context, and while Home or the thread
    has a reply in progress.
    """
    config = _app_config(request)
    user_id = _user_id(current_user)
    thread = await _owned_thread(request, current_user, db_context, conversation_id)
    home = await find_home(db_context, config, user_id=user_id, interface_type="web")
    if home is None or thread.home_conversation_id != home:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND)
    hub = get_stream_hub(request)
    row = await _home_turn_row(db_context, home, payload.message_id, hub)
    assert row["turn_id"] is not None
    # What moves is an exchange the user started. A turn with no message of
    # theirs -- a thread's echoed outcome, a proactive message -- is a record
    # other things point at, and stays in Home.
    if not any(
        turn_row["role"] == "user"
        and not turn_row["is_internal"]
        and turn_row["conversation_id"] == home
        for turn_row in await db_context.message_history.get_rows_by_turn_id(
            row["turn_id"]
        )
    ):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Only an exchange you started can be moved.",
        )
    # A row landing in either conversation mid-turn would sort into the middle
    # of that turn's own rows.
    # A thread whose starting run has not finished -- queued, or waiting on its
    # own delegations to requeue it -- has a turn under way the hub may not
    # know of.
    starting_run = (
        await db_context.delegation_runs.get_by_delegation_id(thread.delegation_id)
        if thread.delegation_id is not None
        else None
    )
    if (
        not await _at_rest(db_context, hub, home)
        or not await _at_rest(db_context, hub, conversation_id)
        or (
            starting_run is not None
            and starting_run["status"] not in TERMINAL_DELEGATION_STATUSES
        )
    ):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Wait for the reply in progress to finish, then move the turn.",
        )
    profile = _local_profile(request, thread.profile_id)
    destination = BoundaryDestination(
        interface_type=thread.interface_type,
        conversation_id=conversation_id,
        profile=profile.service_config,
        user_id=user_id,
    )
    try:
        moved = await move_turn_across_boundary(
            db_context,
            destination=destination,
            turn=MovedTurn(
                source_interface_type=row["interface_type"],
                source_conversation_id=home,
                turn_id=row["turn_id"],
            ),
            now=datetime.now(UTC),
        )
    except CrossBoundaryCopyRefusedError as refusal:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(
                f"This thread runs on '{thread.profile_id}', which does not "
                "receive household context, so a Home turn can't be moved into it."
            ),
        ) from refusal
    except LookupError as gone:
        # Moved by a request that got there first.
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="No such message in your Home.",
        ) from gone

    await announce_home_change(
        hub, home_conversation_id=home, user_id=user_id, reason="turn_moved"
    )
    await announce_home_change(
        hub, home_conversation_id=conversation_id, user_id=user_id, reason="turn_moved"
    )
    return MoveTurnResponse(
        thread=await _thread_summary(
            db_context, config, hub, user_id=user_id, conversation_id=conversation_id
        ),
        marker_message_id=moved.marker_id,
        moved_message_ids=list(moved.copied_ids),
    )
