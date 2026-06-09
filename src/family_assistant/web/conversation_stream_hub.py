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
from datetime import datetime
from typing import Any, Literal

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


TurnStatus = Literal["running", "complete", "failed"]


@dataclass(slots=True, frozen=True)
class StreamEvent:
    """A single event in a conversation's event stream.

    Fields:
        seq: Monotonic per-conversation sequence number assigned by the hub.
        type: Discriminator. One of ``turn_started``, ``text``, ``tool_call``,
            ``tool_result``, ``tool_confirmation_request``,
            ``tool_confirmation_result``, ``attachment``, ``error``,
            ``turn_ended``, ``heartbeat``, ``stream_dropped``.
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


@dataclass(slots=True)
class TurnRecord:
    """Bookkeeping for a single turn produced through the hub."""

    turn_id: str
    conversation_id: str
    user_id: str
    started_at: datetime
    first_seq: int
    status: TurnStatus = "running"
    latest_seq: int = 0  # seq of the most recent event published for this turn
    ended_seq: int | None = None  # seq of the turn_ended event, set when complete
    delivered: bool = False  # True once any subscriber has acked turn_ended.seq
    delivered_event: asyncio.Event = field(default_factory=asyncio.Event)
    task: asyncio.Task[None] | None = None  # producer task (strong-ref)


@dataclass(slots=True)
class _Subscription:
    """A live subscriber's queue plus its latest ack."""

    queue: asyncio.Queue[StreamEvent]
    last_ack_seq: int  # highest seq the client has acknowledged receiving


@dataclass(slots=True)
class _ConversationState:
    """Per-conversation hub state."""

    conversation_id: str
    next_seq: int = 0
    buffer: deque[StreamEvent] = field(default_factory=deque)
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
        # Guards self._conversations dict mutations (registering a new
        # conversation). Per-conversation locks guard everything inside.
        self._registry_lock = asyncio.Lock()
        self._buffer_max_events = buffer_max_events
        self._subscriber_queue_max = subscriber_queue_max
        self._max_retained_turns = max_retained_turns
        self._max_conversations = max_conversations

    # ------------------------------------------------------------------ #
    # Registry / lookups
    # ------------------------------------------------------------------ #

    async def _get_or_create_state(self, conversation_id: str) -> _ConversationState:
        async with self._registry_lock:
            state = self._conversations.get(conversation_id)
            if state is None:
                state = _ConversationState(conversation_id=conversation_id)
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
        is exceeded. Running turns are never pruned. Caller holds ``state.lock``.
        """
        if len(state.turns) <= self._max_retained_turns:
            return
        prunable = sorted(
            (t for t in state.turns.values() if t.status != "running"),
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

    def active_turns(self, conversation_id: str) -> list[TurnRecord]:
        """Return turns currently in the hub for a conversation (running or
        recently completed). Used by the messages endpoint to surface
        ``active_turns`` and by the 410 fallback response."""
        state = self._get_state(conversation_id)
        if state is None:
            return []
        return list(state.turns.values())

    def conversations_with_running_turn_for_user(
        self, user_id: str
    ) -> list[tuple[str, TurnRecord]]:
        """Return ``(conversation_id, turn)`` for every running turn owned by
        ``user_id``. Used for debugging/admin only; not on the hot path."""
        result: list[tuple[str, TurnRecord]] = []
        for conversation_id, state in self._conversations.items():
            for turn in state.turns.values():
                if turn.status == "running" and turn.user_id == user_id:
                    result.append((conversation_id, turn))
        return result

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
            seq = state.next_seq
            state.next_seq += 1
            turn = state.turns.get(turn_id) if turn_id is not None else None
            event = StreamEvent(
                seq=seq,
                type=event_type,
                turn_id=turn_id,
                payload=payload,
            )

            # Ring buffer: drop oldest when full.
            if len(state.buffer) >= self._buffer_max_events:
                state.buffer.popleft()
            state.buffer.append(event)

            if turn is not None:
                turn.latest_seq = seq

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

    async def start_turn(
        self,
        conversation_id: str,
        *,
        turn_id: str,
        user_id: str,
        started_at: datetime,
    ) -> TurnRecord:
        """Register a new turn and publish ``turn_started`` synchronously.

        Returns the ``TurnRecord`` with ``first_seq`` and ``latest_seq`` set.
        Raises ``TurnAlreadyExistsError`` if ``turn_id`` is already known,
        with the existing record attached so the caller can short-circuit
        the idempotent ``POST /turns`` to return the same identity.
        """
        # Check-then-act idempotency: grab the per-conversation lock once we
        # know the conversation exists.
        state = await self._get_or_create_state(conversation_id)
        async with state.lock:
            existing = state.turns.get(turn_id)
            if existing is not None:
                raise TurnAlreadyExistsError(existing)

            seq = state.next_seq
            state.next_seq += 1
            turn = TurnRecord(
                turn_id=turn_id,
                conversation_id=conversation_id,
                user_id=user_id,
                started_at=started_at,
                first_seq=seq,
                latest_seq=seq,
            )
            state.turns[turn_id] = turn
            self._prune_completed_turns(state)

            event = StreamEvent(
                seq=seq,
                type="turn_started",
                turn_id=turn_id,
                payload={
                    "turn_id": turn_id,
                    "started_at": started_at.isoformat(),
                },
            )
            if len(state.buffer) >= self._buffer_max_events:
                state.buffer.popleft()
            state.buffer.append(event)

            self._fan_out(state, event)

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

            seq = state.next_seq
            state.next_seq += 1
            event = StreamEvent(
                seq=seq,
                type="turn_ended",
                turn_id=turn_id,
                payload=payload,
            )

            if len(state.buffer) >= self._buffer_max_events:
                state.buffer.popleft()
            state.buffer.append(event)

            if turn is not None:
                turn.status = status
                turn.latest_seq = seq
                turn.ended_seq = seq

            self._fan_out(state, event)

            # Re-check delivered under the same lock: a subscriber may already
            # have acked past this seq before turn_ended was published.
            max_ack = max(
                (sub.last_ack_seq for sub in state.subscribers.values()),
                default=-1,
            )
            self._refresh_delivered_under_lock(state, ack_seq=max_ack)

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
            # A positive from_seq is a resume: the client genuinely already has
            # everything below it, so from_seq-1 is an implicit ack. A tail
            # subscription (from_seq<0) has received NOTHING and replays nothing,
            # so it must NOT implicitly ack the current head — doing so would
            # mark just-ended, never-delivered turns as delivered and suppress
            # their disconnect push. Tail subscribers only carry the explicit
            # ack_seq (default -1 = nothing acked).
            initial_ack = ack_seq if tail_only else max(ack_seq, from_seq - 1)
            state.subscribers[queue] = _Subscription(
                queue=queue, last_ack_seq=initial_ack
            )

            # The ack at subscribe time may already be enough to mark a
            # turn as delivered (e.g. resume-with-ack after a clean
            # round-trip).
            self._refresh_delivered_under_lock(state, ack_seq=initial_ack)

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

    async def ack(
        self,
        conversation_id: str,
        queue: asyncio.Queue[StreamEvent],
        ack_seq: int,
    ) -> None:
        """Record that this subscriber has received events up to ``ack_seq``.

        If the ack covers a ``turn_ended`` seq, the turn's ``delivered`` flag
        is flipped so the disconnect-push logic knows not to fire.
        """
        state = self._get_state(conversation_id)
        if state is None:
            return
        async with state.lock:
            sub = state.subscribers.get(queue)
            if sub is None:
                return
            sub.last_ack_seq = max(sub.last_ack_seq, ack_seq)
            self._refresh_delivered_under_lock(state, ack_seq=ack_seq)

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
    ) -> None:
        """Store a strong reference to the producer task on the TurnRecord so
        the asyncio loop won't garbage-collect it after the originating HTTP
        request closes. The hub releases the reference when the task is
        evicted along with the turn record."""
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

        # Release the strong reference (and the task's coroutine frame, which
        # retains the DB context and accumulated strings) as soon as the
        # producer finishes. Without this, completed tasks pile up in
        # ``state.turns`` and leak memory with every turn.
        def _release(_completed: asyncio.Task[None], turn_id: str = turn_id) -> None:
            record = state.turns.get(turn_id)
            if record is not None:
                record.task = None

        task.add_done_callback(_release)

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
