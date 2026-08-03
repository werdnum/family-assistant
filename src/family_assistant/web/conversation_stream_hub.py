"""In-memory event broker for resumable conversation streaming.

The hub buffers typed SSE events per conversation so that multiple subscribers
(initial sender, reconnects after tab refresh, second tab, iOS+web pair) all
see the same monotonic event sequence. Producers (LLM processing tasks) publish
events; subscribers (HTTP SSE generators) read from per-subscriber asyncio
queues. The hub is the only owner of producer task strong-references so the
turn keeps running after the originating HTTP request is gone.

Memory model: events live in a bounded ring buffer per conversation. Late
subscribers can replay from any ``from_seq`` >= ``min_available_seq``. Once
events fall off the buffer they are gone; the resume endpoint returns 410 in
that case and the client falls back to history reload.

This is an in-process broker only. The deployment assumes a single FastAPI
worker (see docs/design/resumable_streaming.md). If/when the deployment grows,
the broker needs to be promoted to Redis Streams or Postgres LISTEN/NOTIFY.
"""

import asyncio
import logging
from collections import OrderedDict, deque
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, Literal

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from family_assistant.processing.types import MidTurnInputProvider

logger = logging.getLogger(__name__)


# Maximum events buffered per conversation. Combined with the typical
# size of stream events (token deltas ~10 bytes, tool results larger),
# this caps per-conversation memory at roughly 1-5 MB.
DEFAULT_BUFFER_MAX_EVENTS = 5000

# Maximum events buffered per subscriber queue before backpressure handling
# kicks in. M0 keeps this large so we don't drop subscribers in normal
# operation; M2 adds explicit stream_dropped semantics.
DEFAULT_SUBSCRIBER_QUEUE_MAX = 1000

# Maximum TurnRecords retained per conversation. Completed turns linger so the
# messages endpoint can report recently-active turns and a late resume can
# still find them, but they are pruned (oldest non-running first) once this cap
# is exceeded so the registry doesn't grow without bound.
DEFAULT_MAX_RETAINED_TURNS = 50

# Maximum conversations retained in the hub. The hub is purely a UX cache, so
# idle conversations (no live subscribers, no running turn) are evicted
# oldest-first once this cap is exceeded — bounding memory even if many
# distinct conversation_ids are touched over the process lifetime. Evicting a
# conversation just drops its buffer; a later resume gets a 410 and the client
# falls back to history reload.
DEFAULT_MAX_CONVERSATIONS = 200


TurnStatus = Literal["running", "complete", "failed", "cancelled"]


@dataclass(slots=True, frozen=True)
class StreamEvent:
    """A single event in a conversation's event stream.

    Fields:
        seq: Monotonic per-conversation sequence number assigned by the hub.
        type: Discriminator. One of ``turn_started``, ``text``, ``tool_call``,
            ``tool_result``, ``tool_confirmation_request``,
            ``tool_confirmation_result``, ``attachment``, ``user_input``,
            ``error``, ``turn_ended``, ``heartbeat``, ``stream_dropped``.
        turn_id: Owning turn UUID. ``None`` for connection-level events
            (heartbeat, stream_dropped).
        payload: Type-specific JSON-serializable data. The wire-layer
            serializes this verbatim as the SSE ``data:`` line.
    """

    seq: int
    type: str
    turn_id: str | None
    # ast-grep-ignore: no-dict-any - StreamEvent payloads are heterogeneous across event types (text/tool/confirmation/etc.) and serialized verbatim to the SSE wire format
    payload: dict[str, Any]


@dataclass(slots=True, frozen=True)
class ConversationActivity:
    """A lightweight "a conversation changed" ping for the account-global
    activity stream.

    Carries no message content — only the conversation id, a coarse reason, and
    a timestamp. Clients react by re-fetching the authoritative,
    ownership-filtered conversation list, so the ping is purely a refresh nudge
    (a missed one degrades to the client's other refresh triggers; a spurious
    one only costs a redundant list fetch).
    """

    conversation_id: str
    reason: str
    timestamp: datetime


@dataclass(slots=True)
class _ActivitySubscription:
    """A live account-global activity subscriber's queue plus its user filter."""

    queue: asyncio.Queue[ConversationActivity]
    user_id: str


@dataclass(slots=True, frozen=True)
class ActivitySubscriptionHandle:
    """Returned by ``subscribe_activity``. The caller drains ``queue`` and must
    call ``hub.unsubscribe_activity(queue)`` when done (try/finally).

    Unlike per-conversation subscriptions there is no replay: activity pings are
    ephemeral, and a client refreshes the whole list once on connect anyway.
    """

    queue: asyncio.Queue[ConversationActivity]


@dataclass(slots=True)
class TurnRecord:
    """Bookkeeping for a single turn produced through the hub."""

    turn_id: str
    user_id: str
    started_at: datetime
    first_seq: int
    status: TurnStatus = "running"
    latest_seq: int = 0  # seq of the most recent event published for this turn
    ended_seq: int | None = None  # seq of the turn_ended event, set when complete
    delivered: bool = False  # True once any subscriber has acked turn_ended.seq
    delivered_event: asyncio.Event = field(default_factory=asyncio.Event)
    task: asyncio.Task[None] | None = None  # producer task (strong-ref)
    # Cooperative interrupt/steer handle for a running turn. The cancel/steer
    # endpoints look this up to request a graceful interrupt or inject a
    # mid-turn user message. Cleared alongside ``task`` when the producer ends.
    mid_turn_controller: "MidTurnInputProvider | None" = None
    # Steering messages this turn has accepted, ``input_id`` -> the stream head
    # reported when it was first queued. Held on the record rather than only on
    # the controller because the controller is dropped the moment the producer
    # finishes, while the record lingers: a client retrying a steer whose
    # response was lost can arrive after the turn ended, and answering that 409
    # would have it resend an instruction the turn already acted on. The stored
    # head is what a retry must be told -- the current head has moved past the
    # message's own echo, and a client replaying from it would never see the
    # echo it is waiting for. Write-once per id, and read only once the turn is
    # no longer running: while it runs, the controller's own lock-guarded set is
    # what makes accept-or-drop atomic.
    accepted_steer_inputs: dict[str, int] = field(default_factory=dict)
    # Invoked by the done-callback safety net if the producer task finished while
    # the turn was still 'running' AND it was cancelled (a Stop before the
    # producer's first slice). Lets the web layer persist a durable stopped
    # marker the never-run producer couldn't. Cleared with ``task``.
    on_orphan_cancel: "Callable[[], Awaitable[None]] | None" = None


@dataclass(slots=True)
class _Subscription:
    """A live subscriber's queue plus its latest ack."""

    queue: asyncio.Queue[StreamEvent]
    last_ack_seq: int  # highest seq the client has acknowledged receiving


@dataclass(slots=True)
class _ConversationState:
    """Per-conversation hub state."""

    conversation_id: str
    # Bounded ring buffer: appending past ``maxlen`` evicts the oldest event
    # automatically, so the buffer floor is always ``buffer[0].seq``.
    buffer: deque[StreamEvent]
    next_seq: int = 0
    subscribers: dict[asyncio.Queue[StreamEvent], _Subscription] = field(
        default_factory=dict
    )
    # turn_id -> TurnRecord. Holds both running and recently-completed turns;
    # completed turns linger until evicted (TTL handled in M2).
    turns: dict[str, TurnRecord] = field(default_factory=dict)
    # Per-conversation lock guards next_seq, buffer, subscribers, turns.
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)


class TurnAlreadyExistsError(Exception):
    """Raised by ``start_turn`` if the ``turn_id`` is already registered.

    Callers handle this by returning the existing turn's identity to the
    client (the idempotency contract for ``POST /turns``).
    """

    def __init__(self, turn: TurnRecord) -> None:
        super().__init__(f"Turn {turn.turn_id} already exists")
        self.turn = turn


class ConversationTurnRunningError(Exception):
    """Raised by ``start_turn(reject_if_running=True)`` when the conversation
    already has a different turn running.

    Callers hand the running turn back to the client so it can steer that turn
    instead of starting a rival one.
    """

    def __init__(self, turn: TurnRecord) -> None:
        super().__init__(f"Conversation already has running turn {turn.turn_id}")
        self.turn = turn


@dataclass(slots=True, frozen=True)
class SubscriptionHandle:
    """Returned by ``subscribe``. The caller iterates ``queue`` and must call
    ``hub.unsubscribe(conversation_id, queue)`` when done (try/finally).
    """

    queue: asyncio.Queue[StreamEvent]
    replayed_events: list[StreamEvent]
    """Events from the ring buffer that were already published at subscribe
    time. The caller should yield these before draining ``queue``."""


class OutOfBufferError(Exception):
    """Raised by ``subscribe`` if ``from_seq`` is older than the oldest event
    still in the ring buffer.

    The caller should return 410 Gone with ``active_turns`` populated from
    ``hub.active_turns(conversation_id)`` so the client can choose between
    history reload and a fresh subscribe (``from_seq=min_available_seq``).
    """

    def __init__(self, *, requested_from_seq: int, min_available_seq: int) -> None:
        super().__init__(
            f"from_seq={requested_from_seq} below min_available_seq={min_available_seq}"
        )
        self.requested_from_seq = requested_from_seq
        self.min_available_seq = min_available_seq


class ConversationStreamHub:
    """In-memory broker for conversation event streaming.

    Thread-safety: all public methods are async and use per-conversation
    locks. Safe to call from any task running on the same event loop.
    Not thread-safe across threads (asyncio.Queue.put_nowait is not).
    """

    def __init__(
        self,
        *,
        buffer_max_events: int = DEFAULT_BUFFER_MAX_EVENTS,
        subscriber_queue_max: int = DEFAULT_SUBSCRIBER_QUEUE_MAX,
        max_retained_turns: int = DEFAULT_MAX_RETAINED_TURNS,
        max_conversations: int = DEFAULT_MAX_CONVERSATIONS,
    ) -> None:
        self._conversations: OrderedDict[str, _ConversationState] = OrderedDict()
        # Account-global activity subscribers (one per open
        # /v1/chat/activity/stream connection). Keyed by queue; mutated only on
        # the event loop thread (subscribe/unsubscribe/fan-out), so no lock —
        # mirroring per-conversation ``subscribers``.
        self._activity_subscribers: dict[
            asyncio.Queue[ConversationActivity], _ActivitySubscription
        ] = {}
        # Guards self._conversations dict mutations (registering a new
        # conversation). Per-conversation locks guard everything inside.
        self._registry_lock = asyncio.Lock()
        self._buffer_max_events = buffer_max_events
        self._subscriber_queue_max = subscriber_queue_max
        self._max_retained_turns = max_retained_turns
        self._max_conversations = max_conversations
        # Strong refs to fire-and-forget tasks that end a wedged turn from a
        # producer-task done-callback (see attach_producer_task), so they aren't
        # garbage-collected before they run.
        self._safety_net_tasks: set[asyncio.Task[None]] = set()

    # ------------------------------------------------------------------ #
    # Registry / lookups
    # ------------------------------------------------------------------ #

    async def _get_or_create_state(self, conversation_id: str) -> _ConversationState:
        async with self._registry_lock:
            state = self._conversations.get(conversation_id)
            if state is None:
                state = _ConversationState(
                    conversation_id=conversation_id,
                    buffer=deque(maxlen=self._buffer_max_events),
                )
                self._conversations[conversation_id] = state
                self._evict_idle_conversations()
            else:
                # Mark as most-recently-used (insertion-ordered dict → move to
                # end) so eviction prefers genuinely idle conversations.
                self._conversations.move_to_end(conversation_id)
            return state

    def _evict_idle_conversations(self) -> None:
        """Bound the number of retained conversations. Evict the oldest
        conversations that are idle — no live subscribers, no running turn, and
        no live producer task — once the cap is exceeded. Caller holds
        ``self._registry_lock``.
        """
        if len(self._conversations) <= self._max_conversations:
            return
        excess = len(self._conversations) - self._max_conversations
        # Oldest first (insertion / last-touched order).
        for conversation_id, state in list(self._conversations.items()):
            if excess <= 0:
                break
            # A turn whose producer task is still alive (e.g. a just-completed
            # turn inside its ack-grace / disconnect-push window) must keep its
            # conversation: the hub holds the task's only strong reference, and
            # wait_for_delivery still needs the TurnRecord. Such a turn may
            # already be status!="running", so check the task explicitly.
            has_live_turn = any(
                t.status == "running" or (t.task is not None and not t.task.done())
                for t in state.turns.values()
            )
            if state.subscribers or has_live_turn:
                continue
            del self._conversations[conversation_id]
            excess -= 1

    def _get_state(self, conversation_id: str) -> _ConversationState | None:
        return self._conversations.get(conversation_id)

    def _prune_completed_turns(self, state: _ConversationState) -> None:
        """Drop the oldest completed/failed turns once the per-conversation cap
        is exceeded. Caller holds ``state.lock``.

        A turn is only prunable once it is both not ``running`` AND its producer
        task has finished. Pruning drops the TurnRecord, which holds the hub's
        only strong reference to the producer task (see ``attach_producer_task``)
        — discarding a record whose task is still live (e.g. a just-completed
        turn inside its ack-grace / disconnect-push window) would let that task
        be garbage-collected mid-flight. This mirrors the ``has_live_turn`` guard
        in ``_evict_idle_conversations``.
        """
        if len(state.turns) <= self._max_retained_turns:
            return
        prunable = sorted(
            (
                t
                for t in state.turns.values()
                if t.status != "running" and (t.task is None or t.task.done())
            ),
            key=lambda t: t.first_seq,
        )
        excess = len(state.turns) - self._max_retained_turns
        for turn in prunable[:excess]:
            state.turns.pop(turn.turn_id, None)

    def get_turn(self, conversation_id: str, turn_id: str) -> TurnRecord | None:
        """Return a turn record by id, or None if not in the hub."""
        state = self._get_state(conversation_id)
        if state is None:
            return None
        return state.turns.get(turn_id)

    def get_mid_turn_controller(
        self, conversation_id: str, turn_id: str
    ) -> "MidTurnInputProvider | None":
        """Return the cooperative interrupt/steer controller for a turn.

        ``None`` if the turn is unknown or already finished (the producer's
        done-callback clears the controller). The cancel/steer endpoints use
        this to request an interrupt or inject a mid-turn user message.
        """
        turn = self.get_turn(conversation_id, turn_id)
        return turn.mid_turn_controller if turn is not None else None

    def active_turns(self, conversation_id: str) -> list[TurnRecord]:
        """Return turns currently in the hub for a conversation (running or
        recently completed). Used by the messages endpoint to surface
        ``active_turns`` and by the 410 fallback response."""
        state = self._get_state(conversation_id)
        if state is None:
            return []
        return list(state.turns.values())

    def latest_seq(self, conversation_id: str) -> int:
        """Return the seq of the most recently published event, or -1 if the
        conversation has published none.

        Read as a floor: every event published after this call carries a
        strictly greater seq. The steer endpoint hands it to the client so a
        replayed historical event can't be mistaken for the echo of the steer
        that was just queued.
        """
        state = self._get_state(conversation_id)
        if state is None:
            return -1
        return state.next_seq - 1

    # ------------------------------------------------------------------ #
    # Publishing
    # ------------------------------------------------------------------ #

    async def publish(
        self,
        conversation_id: str,
        event_type: str,
        *,
        turn_id: str | None,
        # ast-grep-ignore: no-dict-any - publish forwards heterogeneous JSON payloads to StreamEvent; per-event-type typing happens at the chat_api translation layer
        payload: dict[str, Any],
    ) -> StreamEvent:
        """Publish an event into ``conversation_id``'s stream.

        Atomically assigns a seq, appends to the ring buffer, and fans out to
        every active subscriber. Returns the published event with seq filled
        in so the producer can record ``latest_seq``/``ended_seq``.
        """
        state = await self._get_or_create_state(conversation_id)
        async with state.lock:
            turn = state.turns.get(turn_id) if turn_id is not None else None
            event = self._append_and_fanout_under_lock(
                state,
                event_type=event_type,
                turn_id=turn_id,
                payload=payload,
            )
            if turn is not None:
                turn.latest_seq = event.seq

        return event

    def _append_and_fanout_under_lock(
        self,
        state: _ConversationState,
        *,
        event_type: str,
        turn_id: str | None,
        # ast-grep-ignore: no-dict-any - heterogeneous StreamEvent payloads serialized verbatim to the SSE wire format
        payload: dict[str, Any],
    ) -> StreamEvent:
        """Assign the next seq, build the event, append it to the ring buffer,
        and fan it out to every subscriber. Returns the published event with
        its seq filled in. Caller holds ``state.lock``.

        The ring buffer is a ``deque`` with a fixed ``maxlen`` so the oldest
        event is evicted automatically on append; the buffer floor is always
        ``buffer[0].seq``.
        """
        seq = state.next_seq
        state.next_seq += 1
        event = StreamEvent(
            seq=seq,
            type=event_type,
            turn_id=turn_id,
            payload=payload,
        )
        state.buffer.append(event)
        self._fan_out(state, event)
        return event

    def _fan_out(self, state: _ConversationState, event: StreamEvent) -> None:
        """Deliver ``event`` to every active subscriber. Caller holds the lock.

        A conversation is single-user on this hub — the only authorization
        boundary is ``_ensure_user_owns_conversation`` at the HTTP layer, which
        rejects any subscriber who isn't a persisted owner — so every registered
        subscriber is entitled to every event. M0 uses ``put_nowait`` with a
        permissive cap; a subscriber that fills its queue is treated as broken
        and dropped.
        """
        dead: list[asyncio.Queue[StreamEvent]] = []
        for queue in list(state.subscribers):
            try:
                queue.put_nowait(event)
            except asyncio.QueueFull:
                logger.warning(
                    "Subscriber queue full for conv=%s seq=%d type=%s; "
                    "dropping subscriber",
                    state.conversation_id,
                    event.seq,
                    event.type,
                )
                dead.append(queue)
        for queue in dead:
            state.subscribers.pop(queue, None)

    # ------------------------------------------------------------------ #
    # Account-global activity channel
    # ------------------------------------------------------------------ #

    def subscribe_activity(self, user_id: str) -> ActivitySubscriptionHandle:
        """Register an account-global activity subscriber for ``user_id``.

        The returned handle's queue receives a ``ConversationActivity`` every
        time a conversation owned by ``user_id`` changes. There is no replay
        (activity pings are ephemeral); the caller refreshes the conversation
        list once on connect and then on each ping. Synchronous + lock-free to
        match ``subscribe``'s queue handout / ``unsubscribe``'s teardown.
        """
        queue: asyncio.Queue[ConversationActivity] = asyncio.Queue(
            maxsize=self._subscriber_queue_max
        )
        self._activity_subscribers[queue] = _ActivitySubscription(
            queue=queue, user_id=user_id
        )
        return ActivitySubscriptionHandle(queue=queue)

    def unsubscribe_activity(self, queue: asyncio.Queue[ConversationActivity]) -> None:
        """Remove an activity subscriber. Idempotent; synchronous + lock-free so
        it runs from a generator ``finally`` during cancellation (see
        ``unsubscribe``)."""
        self._activity_subscribers.pop(queue, None)

    def is_activity_subscribed(
        self, queue: asyncio.Queue[ConversationActivity]
    ) -> bool:
        """Return True if ``queue`` is still a registered activity subscriber.

        Fan-out drops a subscriber whose queue overflowed; the SSE generator
        polls this so it can emit ``stream_dropped`` and close instead of
        heartbeating into a discarded subscription."""
        return queue in self._activity_subscribers

    def has_activity_subscribers(self, user_id: str) -> bool:
        """Return True if at least one activity subscriber is scoped to
        ``user_id``. Lets a caller confirm an activity stream has attached before
        publishing a ping (avoiding a lost-wakeup race)."""
        return any(
            sub.user_id == user_id for sub in self._activity_subscribers.values()
        )

    async def publish_activity(
        self,
        conversation_id: str,
        *,
        user_id: str,
        reason: str,
        timestamp: datetime | None = None,
    ) -> None:
        """Broadcast a conversation-list change for ``user_id``.

        Public entry point for producers that change a conversation outside the
        turn lifecycle (e.g. a delegated/scheduled completion persisted by the
        task worker). Turn-driven activity is emitted automatically by
        ``start_turn``/``end_turn``.
        """
        self._broadcast_activity(
            conversation_id,
            user_id=user_id,
            reason=reason,
            timestamp=timestamp or datetime.now(UTC),
        )

    def _broadcast_activity(
        self,
        conversation_id: str,
        *,
        user_id: str,
        reason: str,
        timestamp: datetime,
    ) -> None:
        """Fan a ``ConversationActivity`` out to every activity subscriber whose
        ``user_id`` matches. Synchronous + lock-free (event-loop thread only),
        iterating a snapshot so a concurrent unsubscribe can't corrupt it. A
        subscriber whose queue is full is dropped (it reconnects)."""
        if not self._activity_subscribers:
            return
        activity = ConversationActivity(
            conversation_id=conversation_id,
            reason=reason,
            timestamp=timestamp,
        )
        dead: list[asyncio.Queue[ConversationActivity]] = []
        for queue, sub in list(self._activity_subscribers.items()):
            if sub.user_id != user_id:
                continue
            try:
                queue.put_nowait(activity)
            except asyncio.QueueFull:
                logger.warning(
                    "Activity subscriber queue full for user=%s; dropping",
                    user_id,
                )
                dead.append(queue)
        for queue in dead:
            self._activity_subscribers.pop(queue, None)

    async def start_turn(
        self,
        conversation_id: str,
        *,
        turn_id: str,
        user_id: str,
        started_at: datetime,
        mid_turn_controller: "MidTurnInputProvider | None" = None,
        reject_if_running: bool = False,
    ) -> TurnRecord:
        """Register a new turn and publish ``turn_started`` synchronously.

        Returns the ``TurnRecord`` with ``first_seq`` and ``latest_seq`` set.
        Raises ``TurnAlreadyExistsError`` if ``turn_id`` is already known,
        with the existing record attached so the caller can short-circuit
        the idempotent ``POST /turns`` to return the same identity.

        ``mid_turn_controller`` is the cooperative interrupt/steer handle for
        this turn; it is stored on the record atomically so the cancel/steer
        endpoints can never observe a running turn without its controller.

        ``reject_if_running`` enforces one turn at a time per conversation:
        registration is refused with ``ConversationTurnRunningError`` if ANY turn
        on the conversation is still running. The check happens under the same
        lock as the registration, so two concurrent kickoffs with different turn
        ids cannot both find the conversation idle and both be admitted.

        Deliberately not scoped to ``user_id``. A conversation the hub serves has
        exactly one canonical owner (the endpoint's sole-owner check refuses the
        rest), but one person can reach it through several raw identities — web
        and API tokens resolve to the same human, and turn records carry the raw
        id. Comparing raw ids would let that person's second identity register a
        rival turn, which is the interleaved history this guard exists to stop.
        """
        # Check-then-act idempotency: grab the per-conversation lock once we
        # know the conversation exists.
        state = await self._get_or_create_state(conversation_id)
        async with state.lock:
            existing = state.turns.get(turn_id)
            if existing is not None:
                raise TurnAlreadyExistsError(existing)

            if reject_if_running:
                running = next(
                    (turn for turn in state.turns.values() if turn.status == "running"),
                    None,
                )
                if running is not None:
                    raise ConversationTurnRunningError(running)

            event = self._append_and_fanout_under_lock(
                state,
                event_type="turn_started",
                turn_id=turn_id,
                payload={
                    "turn_id": turn_id,
                    "started_at": started_at.isoformat(),
                },
            )
            turn = TurnRecord(
                turn_id=turn_id,
                user_id=user_id,
                started_at=started_at,
                first_seq=event.seq,
                latest_seq=event.seq,
                mid_turn_controller=mid_turn_controller,
            )
            state.turns[turn_id] = turn
            self._prune_completed_turns(state)

        # NB: no activity broadcast here. ``start_turn`` runs before the caller
        # persists the user message, and the conversation-list endpoint only
        # lists persisted messages — so a ping now would make a client refetch a
        # list that doesn't yet include this conversation (and clobber an
        # optimistic row). The ``/turns`` endpoint emits ``publish_activity``
        # itself, after the user-message commit. ``end_turn`` (reply persisted)
        # still broadcasts directly.
        return turn

    async def end_turn(
        self,
        conversation_id: str,
        *,
        turn_id: str,
        status: TurnStatus,
        # ast-grep-ignore: no-dict-any - reasoning_info is a forward-compatible LLM provider blob (token counts, model id, optional vendor-specific fields); chat_api passes through MessageReasoningInfo.model_dump()
        reasoning_info: dict[str, Any] | None = None,
        error: str | None = None,
    ) -> StreamEvent:
        """Publish ``turn_ended`` and mark the turn as complete/failed.

        After this returns, ``turn.delivered`` may still be False; that's the
        ack-based signal used by the push-suppression logic and is updated
        whenever an active subscriber acks an ``seq >= ended_seq``.
        """
        state = await self._get_or_create_state(conversation_id)
        async with state.lock:
            turn = state.turns.get(turn_id)
            if turn is not None and turn.ended_seq is not None:
                # Idempotent: the turn already ended. This happens when the
                # producer's success path publishes turn_ended(complete) and
                # then its post-completion code (wait_for_delivery, DB teardown)
                # raises, sending control into the except handler which calls
                # end_turn(failed). Don't publish a second, contradictory
                # turn_ended; return a representation of the existing one
                # without re-fanning-out or bumping the sequence.
                logger.debug(
                    "end_turn ignored; turn %s already ended at seq %s",
                    turn_id,
                    turn.ended_seq,
                )
                return StreamEvent(
                    seq=turn.ended_seq,
                    type="turn_ended",
                    turn_id=turn_id,
                    payload={"turn_id": turn_id, "status": turn.status},
                )
            if turn is None:
                # Defensive: end_turn for an unknown turn shouldn't happen,
                # but if it does, synthesize a record so the event still goes
                # out (the conversation may have a stale subscriber).
                logger.warning(
                    "end_turn for unknown turn_id=%s in conv=%s",
                    turn_id,
                    conversation_id,
                )

            # ast-grep-ignore: no-dict-any - turn_ended payload aggregates fields from heterogeneous sources (turn_id, status, optional reasoning_info, optional error); the wire layer serializes it verbatim
            payload: dict[str, Any] = {
                "turn_id": turn_id,
                "status": status,
            }
            if reasoning_info is not None:
                payload["reasoning_info"] = reasoning_info
            if error is not None:
                payload["error"] = error

            event = self._append_and_fanout_under_lock(
                state,
                event_type="turn_ended",
                turn_id=turn_id,
                payload=payload,
            )

            if turn is not None:
                turn.status = status
                turn.latest_seq = event.seq
                turn.ended_seq = event.seq

            # Re-check delivered under the same lock: a subscriber may already
            # have acked past this seq before turn_ended was published.
            max_ack = max(
                (sub.last_ack_seq for sub in state.subscribers.values()),
                default=-1,
            )
            self._refresh_delivered_under_lock(state, ack_seq=max_ack)

        # Nudge the owner's conversation list to refresh now the reply landed
        # (e.g. a turn that finished while the user was on another thread). Only
        # when the turn is known — an unknown-turn defensive end has no user to
        # scope the activity to. The idempotent already-ended path returns above
        # without reaching here, so this fires once per turn.
        if turn is not None:
            self._broadcast_activity(
                conversation_id,
                user_id=turn.user_id,
                reason="turn_ended",
                timestamp=datetime.now(UTC),
            )

        return event

    # ------------------------------------------------------------------ #
    # Subscription
    # ------------------------------------------------------------------ #

    async def subscribe(
        self,
        conversation_id: str,
        *,
        from_seq: int,
        ack_seq: int = -1,
    ) -> SubscriptionHandle:
        """Subscribe to the conversation's event stream from ``from_seq``.

        The returned handle contains:
        * ``replayed_events``: events with seq >= from_seq that were already
          in the buffer at subscribe time. Yield these first.
        * ``queue``: an ``asyncio.Queue`` that will receive every event
          published after subscribe time. Drain it until the caller is done
          (typically until the HTTP connection closes or a stop signal).

        ``ack_seq`` records the highest seq the client has already received
        (e.g. on reconnect after a network blip). Used for push suppression.

        A negative ``from_seq`` means "tail from the current head": replay
        nothing, just receive events published from now on. This never raises
        ``OutOfBufferError`` and is what always-on live-update (``follow=true``)
        clients use so a rotated buffer can't 410 them into a reconnect loop.

        Otherwise raises ``OutOfBufferError`` if ``from_seq`` is older than the
        oldest event still in the buffer. The caller should return 410 Gone
        with the active turn metadata.
        """
        state = await self._get_or_create_state(conversation_id)
        async with state.lock:
            tail_only = from_seq < 0
            if tail_only:
                # Subscribe at the current head: no replay, just future events.
                from_seq = state.next_seq
            else:
                # The serveable non-tail range is [min_available, next_seq]:
                # events still in the buffer can be replayed, and from_seq ==
                # next_seq is "subscribe at the head, nothing to replay". A
                # cursor below the buffer floor (events evicted) OR above
                # next_seq (the client is ahead of a counter that reset after
                # eviction+recreate) can't be served — 410 so the client reloads
                # history instead of silently tailing renumbered events.
                min_available = state.buffer[0].seq if state.buffer else state.next_seq
                if from_seq < min_available or from_seq > state.next_seq:
                    raise OutOfBufferError(
                        requested_from_seq=from_seq,
                        min_available_seq=min_available,
                    )

            # Snapshot the relevant slice of the buffer. The caller will
            # yield these synthetically before tailing the live queue.
            replayed = (
                []
                if tail_only
                else [event for event in state.buffer if event.seq >= from_seq]
            )

            # Fresh queue. Cap is generous; we only drop on truly slow
            # consumers. Anything in replayed_events does NOT go through the
            # queue (it's yielded directly), so the queue is purely for
            # future events.
            queue: asyncio.Queue[StreamEvent] = asyncio.Queue(
                maxsize=self._subscriber_queue_max
            )
            # Delivery is recorded ONLY on an explicit client ack, never
            # implied by from_seq. from_seq controls solely WHERE replay
            # starts: a send-and-watch client subscribes at a new turn's
            # server-assigned first_seq without having received the prior
            # turn's events, so treating from_seq-1 as an implicit ack would
            # falsely mark a just-ended, never-delivered turn as delivered and
            # suppress its disconnect push.
            state.subscribers[queue] = _Subscription(queue=queue, last_ack_seq=ack_seq)

            # The explicit ack at subscribe time may already be enough to mark
            # a turn as delivered (e.g. resume-with-ack after a clean
            # round-trip).
            self._refresh_delivered_under_lock(state, ack_seq=ack_seq)

        return SubscriptionHandle(queue=queue, replayed_events=replayed)

    def unsubscribe(
        self, conversation_id: str, queue: asyncio.Queue[StreamEvent]
    ) -> None:
        """Remove a subscriber. Idempotent; safe to call twice.

        Intentionally synchronous (no lock / no await) so it works from a
        generator's ``finally`` block during cancellation: an ASGI server
        cancels the response task on client disconnect, and any ``await`` in
        cleanup (e.g. acquiring ``state.lock``) would immediately re-raise
        ``CancelledError`` and leak the subscriber queue. Dict ``pop`` is
        atomic on the event-loop thread; ``publish``'s fan-out iterates a
        snapshot so a concurrent removal cannot corrupt it.
        """
        state = self._get_state(conversation_id)
        if state is None:
            return
        state.subscribers.pop(queue, None)

    def is_subscribed(
        self, conversation_id: str, queue: asyncio.Queue[StreamEvent]
    ) -> bool:
        """Return True if ``queue`` is still a registered subscriber.

        ``_fan_out`` drops a subscriber whose queue is full (a broken/too-slow
        client). The SSE generator polls this so it can surface the documented
        ``stream_dropped`` event and close, rather than emitting heartbeats into
        a subscription the hub has already discarded. Synchronous + lock-free to
        match ``unsubscribe`` (safe to call from a generator)."""
        state = self._get_state(conversation_id)
        if state is None:
            return False
        return queue in state.subscribers

    async def ack_conversation(self, conversation_id: str, ack_seq: int) -> None:
        """Record a conversation-wide acknowledgement up to ``ack_seq``.

        Used by the ``POST /ack`` endpoint for clients that acknowledge receipt
        out-of-band (e.g. after handling a push) rather than over an open SSE
        subscription. Bumps every current subscriber's ack and flips any turn
        whose ``ended_seq`` is now covered. The conversation is single-user
        (the HTTP ownership check is the boundary), so no per-user scoping is
        needed here.
        """
        state = self._get_state(conversation_id)
        if state is None:
            return
        async with state.lock:
            for sub in state.subscribers.values():
                sub.last_ack_seq = max(sub.last_ack_seq, ack_seq)
            self._refresh_delivered_under_lock(state, ack_seq=ack_seq)

    def _refresh_delivered_under_lock(
        self, state: _ConversationState, *, ack_seq: int
    ) -> None:
        """Mark every turn ``delivered`` whose ``ended_seq`` is covered by
        ``ack_seq``. Caller holds ``state.lock``."""
        for turn in state.turns.values():
            if turn.ended_seq is None or ack_seq < turn.ended_seq:
                continue
            turn.delivered = True
            turn.delivered_event.set()

    async def wait_for_delivery(
        self,
        conversation_id: str,
        turn_id: str,
        *,
        timeout: float,
    ) -> bool:
        """Wait up to ``timeout`` seconds for any subscriber to ack a seq at
        or beyond the turn's ``ended_seq``.

        Returns True if the turn was acknowledged before the timeout. Used by
        the producer to decide whether to suppress the disconnect push: an
        attached subscriber who consumed turn_ended will ack promptly, and we
        want to skip the redundant push in that case.
        """
        turn = self.get_turn(conversation_id, turn_id)
        if turn is None:
            return False
        if turn.delivered:
            return True
        try:
            await asyncio.wait_for(turn.delivered_event.wait(), timeout=timeout)
        except TimeoutError:
            return False
        return turn.delivered

    # ------------------------------------------------------------------ #
    # Producer task ownership
    # ------------------------------------------------------------------ #

    def attach_producer_task(
        self,
        conversation_id: str,
        turn_id: str,
        task: asyncio.Task[None],
        on_orphan_cancel: "Callable[[], Awaitable[None]] | None" = None,
    ) -> None:
        """Store a strong reference to the producer task on the TurnRecord so
        the asyncio loop won't garbage-collect it after the originating HTTP
        request closes. The hub releases the reference when the task is
        evicted along with the turn record.

        ``on_orphan_cancel`` is invoked by the safety net if the task is
        cancelled before its coroutine runs (so the producer never persisted a
        stopped marker)."""
        state = self._get_state(conversation_id)
        if state is None:
            logger.error(
                "attach_producer_task: no conversation state for %s",
                conversation_id,
            )
            return
        turn = state.turns.get(turn_id)
        if turn is None:
            logger.error(
                "attach_producer_task: no turn record for turn_id=%s in conv=%s",
                turn_id,
                conversation_id,
            )
            return
        turn.task = task
        turn.on_orphan_cancel = on_orphan_cancel

        # Release the strong reference (and the task's coroutine frame, which
        # retains the DB context and accumulated strings) as soon as the
        # producer finishes. Without this, completed tasks pile up in
        # ``state.turns`` and leak memory with every turn.
        def _release(_completed: asyncio.Task[None], turn_id: str = turn_id) -> None:
            record = state.turns.get(turn_id)
            if record is None:
                return
            record.task = None
            # The controller is only meaningful for a running turn; drop it
            # so a finished turn can't be steered/interrupted and so its
            # queued inputs don't linger.
            controller = record.mid_turn_controller
            record.mid_turn_controller = None
            orphan_cancel = record.on_orphan_cancel
            record.on_orphan_cancel = None
            # Safety net: if the producer task finished without the turn ever
            # reaching a terminal status, the TurnRecord would wedge at 'running'
            # (pruning/eviction skip running turns). This happens when the task
            # is cancelled before its coroutine's first slice — e.g. Stop arrives
            # in the window after attach_producer_task but before run_turn_producer
            # runs — so its try/except never executes. End the turn now.
            if record.status == "running":
                # Classify like the producer's own cancellation path: a user Stop
                # set the controller's interrupt flag -> 'cancelled'; any other
                # cancellation (app shutdown, supervisor teardown) -> 'failed'.
                user_requested_stop = (
                    controller is not None and controller.should_interrupt()
                )
                end_status: TurnStatus = (
                    "cancelled" if user_requested_stop else "failed"
                )
                # Only persist a stopped marker for a user-cancelled orphan, not
                # a teardown 'failed'.
                persist = orphan_cancel if user_requested_stop else None
                cleanup = asyncio.ensure_future(
                    self._end_unfinished_turn(
                        conversation_id, turn_id, end_status, persist
                    )
                )
                self._safety_net_tasks.add(cleanup)
                cleanup.add_done_callback(self._safety_net_tasks.discard)

        task.add_done_callback(_release)

    async def _end_unfinished_turn(
        self,
        conversation_id: str,
        turn_id: str,
        status: TurnStatus,
        on_orphan_cancel: "Callable[[], Awaitable[None]] | None" = None,
    ) -> None:
        """End a turn whose producer task finished without ending it.

        Persists a durable stopped marker first (via ``on_orphan_cancel``) so a
        refresh sees the stopped turn, then ends it. ``end_turn`` is idempotent,
        so if the producer did end the turn in a race this is a no-op.
        """
        if on_orphan_cancel is not None:
            try:
                await on_orphan_cancel()
            except Exception:
                logger.exception(
                    "Failed to persist stopped marker for orphaned turn "
                    "conv=%s turn=%s",
                    conversation_id,
                    turn_id,
                )
        try:
            await self.end_turn(
                conversation_id,
                turn_id=turn_id,
                status=status,
                error="cancelled",
            )
        except Exception:
            logger.exception(
                "Failed to end unfinished turn conv=%s turn=%s",
                conversation_id,
                turn_id,
            )

    def get_active_producer_tasks(
        self, conversation_id: str | None = None
    ) -> list[asyncio.Task[None]]:
        """Return active producer tasks for testing/teardown."""
        tasks: list[asyncio.Task[None]] = []
        states = (
            [self._conversations.get(conversation_id)]
            if conversation_id is not None
            else list(self._conversations.values())
        )
        for state in states:
            if state is None:
                continue
            for turn in state.turns.values():
                if turn.task is not None and not turn.task.done():
                    tasks.append(turn.task)
        return tasks

    # ------------------------------------------------------------------ #
    # Test/debug helpers
    # ------------------------------------------------------------------ #

    def buffer_size(self, conversation_id: str) -> int:
        state = self._get_state(conversation_id)
        return 0 if state is None else len(state.buffer)

    def subscriber_count(self, conversation_id: str) -> int:
        state = self._get_state(conversation_id)
        return 0 if state is None else len(state.subscribers)

    def min_available_seq(self, conversation_id: str) -> int | None:
        """Lowest seq still in the ring buffer (None if the buffer is empty)."""
        state = self._get_state(conversation_id)
        if state is None or not state.buffer:
            return None
        return state.buffer[0].seq
