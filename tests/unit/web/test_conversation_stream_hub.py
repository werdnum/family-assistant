"""Unit tests for ConversationStreamHub.

The hub is the load-bearing primitive behind resumable streaming. These tests
exercise it in isolation: publish/subscribe ordering, ring buffer eviction,
turn idempotency, and ack-based delivery tracking. Integration with the LLM
producer task lives in tests/functional/web/api/test_chat_streaming.py.
"""

import asyncio
from datetime import UTC, datetime

import pytest

from family_assistant.web.conversation_stream_hub import (
    ConversationStreamHub,
    ConversationTurnRunningError,
    OutOfBufferError,
    TurnAlreadyExistsError,
)


def _now() -> datetime:
    return datetime.now(UTC)


@pytest.mark.asyncio
@pytest.mark.no_db
async def test_publish_assigns_monotonic_seq() -> None:
    """Each publish on the same conversation produces a strictly increasing
    seq."""
    hub = ConversationStreamHub()
    e1 = await hub.publish("conv", "text", turn_id="t1", payload={"content": "a"})
    e2 = await hub.publish("conv", "text", turn_id="t1", payload={"content": "b"})
    e3 = await hub.publish("conv", "text", turn_id="t1", payload={"content": "c"})

    assert e1.seq == 0
    assert e2.seq == 1
    assert e3.seq == 2


@pytest.mark.asyncio
@pytest.mark.no_db
async def test_publish_is_per_conversation() -> None:
    """Two conversations have independent seq counters and buffers."""
    hub = ConversationStreamHub()
    a = await hub.publish("conv_a", "text", turn_id="t1", payload={"content": "x"})
    b = await hub.publish("conv_b", "text", turn_id="t1", payload={"content": "y"})

    assert a.seq == 0
    assert b.seq == 0
    assert hub.buffer_size("conv_a") == 1
    assert hub.buffer_size("conv_b") == 1


@pytest.mark.asyncio
@pytest.mark.no_db
async def test_subscribe_from_zero_replays_full_buffer() -> None:
    """A late subscriber starting from seq=0 sees every event in the buffer."""
    hub = ConversationStreamHub()
    await hub.start_turn("conv", turn_id="t1", user_id="u1", started_at=_now())
    await hub.publish("conv", "text", turn_id="t1", payload={"content": "a"})
    await hub.publish("conv", "text", turn_id="t1", payload={"content": "b"})

    handle = await hub.subscribe("conv", from_seq=0)
    assert [e.type for e in handle.replayed_events] == ["turn_started", "text", "text"]
    assert [e.seq for e in handle.replayed_events] == [0, 1, 2]


@pytest.mark.asyncio
@pytest.mark.no_db
async def test_subscribe_from_midstream_skips_old_events() -> None:
    """Subscribing with from_seq>0 replays only events at or after that seq."""
    hub = ConversationStreamHub()
    await hub.publish("conv", "text", turn_id="t1", payload={"content": "a"})
    await hub.publish("conv", "text", turn_id="t1", payload={"content": "b"})
    await hub.publish("conv", "text", turn_id="t1", payload={"content": "c"})

    handle = await hub.subscribe("conv", from_seq=2)
    assert [e.seq for e in handle.replayed_events] == [2]


@pytest.mark.asyncio
@pytest.mark.no_db
async def test_subscriber_receives_live_events() -> None:
    """Events published after subscription land in the subscriber's queue."""
    hub = ConversationStreamHub()
    handle = await hub.subscribe("conv", from_seq=0)
    assert handle.replayed_events == []

    await hub.publish("conv", "text", turn_id="t1", payload={"content": "live"})
    event = await asyncio.wait_for(handle.queue.get(), timeout=1.0)
    assert event.type == "text"
    assert event.payload == {"content": "live"}


@pytest.mark.asyncio
@pytest.mark.no_db
async def test_multiple_subscribers_see_identical_stream() -> None:
    """Two subscribers attached at different points still see the same
    ordering for events they both observe."""
    hub = ConversationStreamHub()
    handle_a = await hub.subscribe("conv", from_seq=0)
    await hub.publish("conv", "text", turn_id="t1", payload={"content": "1"})
    handle_b = await hub.subscribe("conv", from_seq=0)
    await hub.publish("conv", "text", turn_id="t1", payload={"content": "2"})

    # A reads live events (no replay since it was subscribed at seq=0 before
    # any publish).
    a_first = await asyncio.wait_for(handle_a.queue.get(), timeout=1.0)
    a_second = await asyncio.wait_for(handle_a.queue.get(), timeout=1.0)
    assert [a_first.seq, a_second.seq] == [0, 1]

    # B sees seq=0 in replayed and seq=1 live.
    assert [e.seq for e in handle_b.replayed_events] == [0]
    b_second = await asyncio.wait_for(handle_b.queue.get(), timeout=1.0)
    assert b_second.seq == 1


@pytest.mark.asyncio
@pytest.mark.no_db
async def test_ring_buffer_evicts_oldest_events() -> None:
    """When the buffer is full, the oldest event drops off and resubscribing
    from below the new floor raises OutOfBufferError."""
    hub = ConversationStreamHub(buffer_max_events=3)
    for i in range(5):
        await hub.publish("conv", "text", turn_id="t1", payload={"i": i})

    assert hub.buffer_size("conv") == 3
    assert hub.min_available_seq("conv") == 2

    with pytest.raises(OutOfBufferError) as excinfo:
        await hub.subscribe("conv", from_seq=0)
    assert excinfo.value.requested_from_seq == 0
    assert excinfo.value.min_available_seq == 2

    # Subscribing at the new floor works.
    handle = await hub.subscribe("conv", from_seq=2)
    assert [e.payload["i"] for e in handle.replayed_events] == [2, 3, 4]


@pytest.mark.asyncio
@pytest.mark.no_db
async def test_tail_subscribe_replays_nothing_and_never_410s() -> None:
    """A negative from_seq tails from the current head: no replay, and it
    never raises OutOfBufferError even after the buffer has rotated. This is
    what follow=true live-update clients use so a rotated buffer can't trap
    them in a 410 reconnect loop."""
    hub = ConversationStreamHub(buffer_max_events=2)
    for i in range(5):
        await hub.publish("conv", "text", turn_id="t1", payload={"i": i})

    # Buffer has rotated well past 0; a from_seq=0 subscribe would 410.
    assert hub.min_available_seq("conv") == 3
    with pytest.raises(OutOfBufferError):
        await hub.subscribe("conv", from_seq=0)

    # Tail subscription replays nothing and does not raise.
    handle = await hub.subscribe("conv", from_seq=-1)
    assert handle.replayed_events == []

    # It receives only events published after it attached.
    await hub.publish("conv", "text", turn_id="t1", payload={"i": 99})
    event = await asyncio.wait_for(handle.queue.get(), timeout=1.0)
    assert event.payload == {"i": 99}


@pytest.mark.asyncio
@pytest.mark.no_db
async def test_empty_buffer_positive_from_seq_410s_unless_at_head() -> None:
    """On an empty buffer (fresh, or evicted+recreated with next_seq reset), the
    only valid non-tail cursor is exactly next_seq. A cursor below it (events
    gone) OR above it (client ahead of a reset counter) must 410 so the client
    reloads history instead of silently tailing renumbered events. from_seq ==
    next_seq is the legitimate 'subscribe at head' case."""
    hub = ConversationStreamHub()  # fresh: next_seq == 0, empty buffer

    # from_seq == next_seq (0): valid, subscribe at head, no replay.
    handle = await hub.subscribe("conv", from_seq=0)
    assert handle.replayed_events == []
    hub.unsubscribe("conv", handle.queue)

    # from_seq above the head (e.g. a stale cursor after eviction reset): 410.
    with pytest.raises(OutOfBufferError):
        await hub.subscribe("conv", from_seq=5)


@pytest.mark.asyncio
@pytest.mark.no_db
async def test_unsubscribe_removes_subscriber() -> None:
    """After unsubscribe, no further events land in the queue."""
    hub = ConversationStreamHub()
    handle = await hub.subscribe("conv", from_seq=0)
    assert hub.subscriber_count("conv") == 1

    hub.unsubscribe("conv", handle.queue)
    assert hub.subscriber_count("conv") == 0

    await hub.publish("conv", "text", turn_id="t1", payload={"content": "x"})
    assert handle.queue.empty()


@pytest.mark.asyncio
@pytest.mark.no_db
async def test_start_turn_publishes_turn_started_and_records_metadata() -> None:
    """start_turn registers the turn, publishes turn_started, and exposes the
    record via active_turns."""
    hub = ConversationStreamHub()
    started_at = _now()
    turn = await hub.start_turn(
        "conv", turn_id="t1", user_id="u1", started_at=started_at
    )

    assert turn.first_seq == 0
    assert turn.latest_seq == 0
    assert turn.status == "running"
    active = hub.active_turns("conv")
    assert len(active) == 1
    assert active[0].turn_id == "t1"

    # turn_started landed in the buffer.
    handle = await hub.subscribe("conv", from_seq=0)
    assert handle.replayed_events[0].type == "turn_started"
    assert handle.replayed_events[0].payload["turn_id"] == "t1"


@pytest.mark.asyncio
@pytest.mark.no_db
async def test_start_turn_is_idempotent_by_turn_id() -> None:
    """A second start_turn with the same turn_id raises TurnAlreadyExistsError
    carrying the existing record (chat_api uses this for the idempotent
    POST /turns retry path)."""
    hub = ConversationStreamHub()
    first = await hub.start_turn("conv", turn_id="t1", user_id="u1", started_at=_now())

    with pytest.raises(TurnAlreadyExistsError) as excinfo:
        await hub.start_turn("conv", turn_id="t1", user_id="u1", started_at=_now())
    assert excinfo.value.turn is first


@pytest.mark.asyncio
@pytest.mark.no_db
async def test_end_turn_publishes_and_marks_complete() -> None:
    """end_turn flips status and publishes the turn_ended event with the
    declared status payload."""
    hub = ConversationStreamHub()
    await hub.start_turn("conv", turn_id="t1", user_id="u1", started_at=_now())
    await hub.publish("conv", "text", turn_id="t1", payload={"content": "hi"})
    await hub.end_turn(
        "conv",
        turn_id="t1",
        status="complete",
        reasoning_info={"total_tokens": 12},
    )

    turn = hub.get_turn("conv", "t1")
    assert turn is not None
    assert turn.status == "complete"
    assert turn.ended_seq == 2

    handle = await hub.subscribe("conv", from_seq=0)
    types = [e.type for e in handle.replayed_events]
    assert types == ["turn_started", "text", "turn_ended"]
    end_event = handle.replayed_events[-1]
    assert end_event.payload["status"] == "complete"
    assert end_event.payload["reasoning_info"] == {"total_tokens": 12}


@pytest.mark.asyncio
@pytest.mark.no_db
async def test_ack_marks_turn_delivered_when_covering_end_seq() -> None:
    """An ack at or beyond turn_ended.seq flips turn.delivered, which the
    chat_api layer reads to decide whether to suppress the disconnect push."""
    hub = ConversationStreamHub()
    await hub.start_turn("conv", turn_id="t1", user_id="u1", started_at=_now())
    await hub.publish("conv", "text", turn_id="t1", payload={"content": "x"})
    await hub.end_turn("conv", turn_id="t1", status="complete")

    handle = await hub.subscribe("conv", from_seq=0, ack_seq=2)

    turn = hub.get_turn("conv", "t1")
    assert turn is not None
    assert turn.delivered is True

    # Free the handle warning.
    hub.unsubscribe("conv", handle.queue)


@pytest.mark.asyncio
@pytest.mark.no_db
async def test_ack_below_end_seq_keeps_undelivered() -> None:
    """A subscriber that hasn't acked past turn_ended.seq leaves delivered
    False, so the push path still fires."""
    hub = ConversationStreamHub()
    await hub.start_turn("conv", turn_id="t1", user_id="u1", started_at=_now())
    await hub.publish("conv", "text", turn_id="t1", payload={"content": "x"})
    await hub.end_turn("conv", turn_id="t1", status="complete")

    handle = await hub.subscribe("conv", from_seq=0, ack_seq=1)

    turn = hub.get_turn("conv", "t1")
    assert turn is not None
    assert turn.delivered is False

    # Ack catches up and flips the flag.
    await hub.ack_conversation("conv", ack_seq=2)
    assert turn.delivered is True

    hub.unsubscribe("conv", handle.queue)


@pytest.mark.asyncio
@pytest.mark.no_db
async def test_subscribe_at_new_turn_does_not_ack_prior_turn() -> None:
    """A send-and-watch client subscribes at a new turn's server-assigned
    first_seq without having received the prior turn's events. The hub must NOT
    treat from_seq-1 as an implicit ack: doing so would falsely mark the just-
    ended prior turn as delivered and suppress its disconnect push. Delivery is
    recorded only on an explicit client ack."""
    hub = ConversationStreamHub()
    await hub.start_turn("conv", turn_id="t1", user_id="u1", started_at=_now())
    await hub.publish("conv", "text", turn_id="t1", payload={"content": "x"})
    ended = await hub.end_turn("conv", turn_id="t1", status="complete")

    # A second turn starts; its turn_started seq is end_seq + 1. The new
    # subscriber resumes at that first_seq but has never received turn t1.
    second = await hub.start_turn("conv", turn_id="t2", user_id="u1", started_at=_now())
    assert second.first_seq == ended.seq + 1

    handle = await hub.subscribe("conv", from_seq=second.first_seq)

    prior = hub.get_turn("conv", "t1")
    assert prior is not None
    assert prior.delivered is False

    hub.unsubscribe("conv", handle.queue)


@pytest.mark.asyncio
@pytest.mark.no_db
async def test_subscribe_on_unknown_conversation_returns_empty() -> None:
    """Subscribing to a conversation that has never been published to yields
    no replayed events and a fresh queue. This is what fresh page loads see
    before the user sends anything."""
    hub = ConversationStreamHub()
    handle = await hub.subscribe("brand_new_conv", from_seq=0)
    assert handle.replayed_events == []
    assert handle.queue.empty()


@pytest.mark.asyncio
@pytest.mark.no_db
async def test_attach_producer_task_holds_strong_reference() -> None:
    """The hub keeps the producer task alive after the originating request
    closes. Without this, a detached SSE client would let the GC drop the
    task and the background turn would never finish."""
    hub = ConversationStreamHub()
    await hub.start_turn("conv", turn_id="t1", user_id="u1", started_at=_now())

    done = asyncio.Event()

    async def fake_producer() -> None:
        await done.wait()

    task = asyncio.create_task(fake_producer())
    hub.attach_producer_task("conv", "t1", task)

    assert task in hub.get_active_producer_tasks("conv")

    done.set()
    await task
    assert task not in hub.get_active_producer_tasks("conv")


@pytest.mark.asyncio
@pytest.mark.no_db
async def test_producer_task_reference_released_on_completion() -> None:
    """When the producer task finishes, the hub drops its strong reference so
    the task (and its coroutine frame: DB context, buffers) can be GC'd. The
    lightweight TurnRecord itself remains for active_turns/resume lookups."""
    hub = ConversationStreamHub()
    await hub.start_turn("conv", turn_id="t1", user_id="u1", started_at=_now())

    done = asyncio.Event()

    async def fake_producer() -> None:
        await done.wait()

    task = asyncio.create_task(fake_producer())
    hub.attach_producer_task("conv", "t1", task)
    attached = hub.get_turn("conv", "t1")
    assert attached is not None
    assert attached.task is task

    done.set()
    await task

    # The done_callback that clears the reference runs on a subsequent loop
    # tick after the task completes; poll until it has fired.
    record = hub.get_turn("conv", "t1")
    assert record is not None
    deadline = 100
    while record.task is not None and deadline > 0:
        # ast-grep-ignore: no-asyncio-sleep-in-tests - yields to the loop so the task's done_callback can run; bounded poll, not an arbitrary wait
        await asyncio.sleep(0)
        deadline -= 1
    assert record.task is None


@pytest.mark.asyncio
@pytest.mark.no_db
async def test_completed_turns_pruned_beyond_cap() -> None:
    """Completed turns are evicted oldest-first once the retention cap is hit,
    so the registry doesn't grow without bound; running turns are kept."""
    hub = ConversationStreamHub(max_retained_turns=3)
    for i in range(6):
        await hub.start_turn("conv", turn_id=f"t{i}", user_id="u1", started_at=_now())
        await hub.end_turn("conv", turn_id=f"t{i}", status="complete")

    remaining = {t.turn_id for t in hub.active_turns("conv")}
    assert len(remaining) == 3
    # The three most recent turns survive; the oldest were pruned.
    assert remaining == {"t3", "t4", "t5"}


@pytest.mark.asyncio
@pytest.mark.no_db
async def test_running_turns_not_pruned() -> None:
    """A still-running turn is never pruned even past the cap."""
    hub = ConversationStreamHub(max_retained_turns=2)
    # One long-running turn that never ends.
    await hub.start_turn("conv", turn_id="running", user_id="u1", started_at=_now())
    for i in range(5):
        await hub.start_turn(
            "conv", turn_id=f"done{i}", user_id="u1", started_at=_now()
        )
        await hub.end_turn("conv", turn_id=f"done{i}", status="complete")

    remaining = {t.turn_id for t in hub.active_turns("conv")}
    assert "running" in remaining


@pytest.mark.asyncio
@pytest.mark.no_db
async def test_completed_turn_with_live_task_not_pruned() -> None:
    """A turn that has ended but whose producer task is still running (e.g.
    inside its ack-grace / disconnect-push window) must not be pruned past the
    cap: the TurnRecord holds the hub's only strong reference to that task, so
    discarding it would let the task be garbage-collected mid-flight."""
    hub = ConversationStreamHub(max_retained_turns=1)

    blocker = asyncio.Event()

    async def fake_producer() -> None:
        await blocker.wait()

    await hub.start_turn("conv", turn_id="lingering", user_id="u1", started_at=_now())
    await hub.end_turn("conv", turn_id="lingering", status="complete")
    task = asyncio.create_task(fake_producer())
    hub.attach_producer_task("conv", "lingering", task)

    # Blow well past the retention cap with fully-finished turns.
    for i in range(3):
        await hub.start_turn(
            "conv", turn_id=f"done{i}", user_id="u1", started_at=_now()
        )
        await hub.end_turn("conv", turn_id=f"done{i}", status="complete")

    remaining = {t.turn_id for t in hub.active_turns("conv")}
    assert "lingering" in remaining
    lingering = hub.get_turn("conv", "lingering")
    assert lingering is not None
    assert lingering.task is task

    # Cleanup: let the task finish.
    blocker.set()
    await task


@pytest.mark.asyncio
@pytest.mark.no_db
async def test_is_subscribed_tracks_drop_on_queue_overflow() -> None:
    """``is_subscribed`` flips to False once the hub drops a subscriber whose
    queue overflowed, so the SSE generator can emit ``stream_dropped`` and close
    instead of heartbeating into a discarded subscription."""
    hub = ConversationStreamHub(subscriber_queue_max=2)
    handle = await hub.subscribe("conv", from_seq=0)
    assert hub.is_subscribed("conv", handle.queue) is True

    # Publish more than the queue can hold without anyone draining it.
    for i in range(5):
        await hub.publish("conv", "text", turn_id=None, payload={"content": str(i)})

    assert hub.is_subscribed("conv", handle.queue) is False


@pytest.mark.asyncio
@pytest.mark.no_db
async def test_idle_conversations_evicted_beyond_cap() -> None:
    """The hub bounds the number of retained conversations, evicting idle ones
    (no subscribers, no running turn) oldest-first so it can't grow without
    bound when many distinct conversation_ids are touched."""
    hub = ConversationStreamHub(max_conversations=3)
    for i in range(6):
        await hub.start_turn(f"conv{i}", turn_id="t", user_id="u1", started_at=_now())
        await hub.end_turn(f"conv{i}", turn_id="t", status="complete")

    # Only the most recent few conversations survive.
    surviving = [f"conv{i}" for i in range(6) if hub.buffer_size(f"conv{i}") > 0]
    assert len(surviving) <= 3
    assert "conv5" in surviving


@pytest.mark.asyncio
@pytest.mark.no_db
async def test_conversation_with_subscriber_not_evicted() -> None:
    """A conversation with a live subscriber is never evicted even past the
    cap, so an active watcher's buffer survives."""
    hub = ConversationStreamHub(max_conversations=2)
    # Keep a live subscriber on conv_keep.
    handle = await hub.subscribe("conv_keep", from_seq=0)
    await hub.publish("conv_keep", "text", turn_id="t", payload={"content": "x"})

    for i in range(5):
        await hub.start_turn(f"other{i}", turn_id="t", user_id="u1", started_at=_now())
        await hub.end_turn(f"other{i}", turn_id="t", status="complete")

    assert hub.subscriber_count("conv_keep") == 1
    assert hub.buffer_size("conv_keep") > 0
    hub.unsubscribe("conv_keep", handle.queue)


@pytest.mark.asyncio
@pytest.mark.no_db
async def test_publish_to_turn_updates_latest_seq() -> None:
    """Each publish under a turn updates that turn's latest_seq so the hub's
    active_turns surface reflects the current progression."""
    hub = ConversationStreamHub()
    turn = await hub.start_turn("conv", turn_id="t1", user_id="u1", started_at=_now())
    assert turn.latest_seq == 0

    await hub.publish("conv", "text", turn_id="t1", payload={"content": "a"})
    assert turn.latest_seq == 1

    await hub.publish("conv", "text", turn_id="t1", payload={"content": "b"})
    assert turn.latest_seq == 2


# ---------------------------------------------------------------------------- #
# Account-global activity channel
# ---------------------------------------------------------------------------- #


@pytest.mark.asyncio
@pytest.mark.no_db
async def test_start_turn_does_not_broadcast_activity() -> None:
    """``start_turn`` must NOT ping the activity stream: it runs before the
    caller persists the user message, and the list endpoint only lists persisted
    messages. The ``/turns`` endpoint emits ``publish_activity`` after the
    commit instead (see chat_api)."""
    hub = ConversationStreamHub()
    handle = hub.subscribe_activity("u1")

    await hub.start_turn("conv", turn_id="t1", user_id="u1", started_at=_now())

    assert handle.queue.empty()


@pytest.mark.asyncio
@pytest.mark.no_db
async def test_activity_scoped_to_owning_user() -> None:
    """Activity for one user's conversation is not delivered to another user's
    subscriber (no cross-user conversation-id leak)."""
    hub = ConversationStreamHub()
    other = hub.subscribe_activity("u2")

    await hub.publish_activity("conv", user_id="u1", reason="turn_started")

    assert other.queue.empty()


@pytest.mark.asyncio
@pytest.mark.no_db
async def test_end_turn_broadcasts_activity() -> None:
    """Ending a turn pings the owner's activity subscriber so a reply that
    finished on another thread refreshes the list."""
    hub = ConversationStreamHub()
    await hub.start_turn("conv", turn_id="t1", user_id="u1", started_at=_now())
    handle = hub.subscribe_activity("u1")

    await hub.end_turn("conv", turn_id="t1", status="complete")

    activity = handle.queue.get_nowait()
    assert activity.conversation_id == "conv"
    assert activity.reason == "turn_ended"


@pytest.mark.asyncio
@pytest.mark.no_db
async def test_publish_activity_broadcasts_to_matching_user() -> None:
    """The public publish_activity entry point (used by the task worker for
    delegated/scheduled replies) reaches the owner's subscriber."""
    hub = ConversationStreamHub()
    handle = hub.subscribe_activity("u1")

    await hub.publish_activity("conv", user_id="u1", reason="delegation")

    activity = handle.queue.get_nowait()
    assert activity.conversation_id == "conv"
    assert activity.reason == "delegation"


@pytest.mark.asyncio
@pytest.mark.no_db
async def test_unsubscribe_activity_stops_delivery() -> None:
    """An unsubscribed activity subscriber receives no further pings."""
    hub = ConversationStreamHub()
    handle = hub.subscribe_activity("u1")
    hub.unsubscribe_activity(handle.queue)

    await hub.publish_activity("conv", user_id="u1", reason="delegation")

    assert handle.queue.empty()
    assert not hub.is_activity_subscribed(handle.queue)


@pytest.mark.asyncio
@pytest.mark.no_db
async def test_activity_overflow_drops_subscriber() -> None:
    """A subscriber whose queue overflows is dropped (it reconnects), rather
    than blocking the broadcast for everyone else."""
    hub = ConversationStreamHub(subscriber_queue_max=2)
    handle = hub.subscribe_activity("u1")

    for _ in range(3):
        await hub.publish_activity("conv", user_id="u1", reason="delegation")

    assert not hub.is_activity_subscribed(handle.queue)


@pytest.mark.asyncio
@pytest.mark.no_db
async def test_reject_if_running_refuses_a_rival_turn() -> None:
    """``reject_if_running`` enforces one turn at a time per conversation.

    The check runs under the same lock as the registration, so it is the
    authoritative guard: a caller doing awaited setup between its own check and
    ``start_turn`` still cannot admit a second turn.
    """
    hub = ConversationStreamHub()
    first = await hub.start_turn("conv", turn_id="t1", user_id="u1", started_at=_now())

    with pytest.raises(ConversationTurnRunningError) as exc_info:
        await hub.start_turn(
            "conv",
            turn_id="t2",
            user_id="u1",
            started_at=_now(),
            reject_if_running=True,
        )

    assert exc_info.value.turn.turn_id == first.turn_id
    assert hub.get_turn("conv", "t2") is None


@pytest.mark.asyncio
@pytest.mark.no_db
async def test_reject_if_running_admits_a_turn_once_the_previous_ended() -> None:
    """Only a RUNNING turn blocks. A completed one lingers in the hub for replay
    and must not wedge the conversation shut."""
    hub = ConversationStreamHub()
    await hub.start_turn("conv", turn_id="t1", user_id="u1", started_at=_now())
    await hub.end_turn("conv", turn_id="t1", status="complete")

    second = await hub.start_turn(
        "conv",
        turn_id="t2",
        user_id="u1",
        started_at=_now(),
        reject_if_running=True,
    )
    assert second.turn_id == "t2"


@pytest.mark.asyncio
@pytest.mark.no_db
async def test_reject_if_running_is_scoped_to_the_user() -> None:
    """The guard exists to stop one user's two clients racing one history. It is
    scoped to that user, matching the ownership check the endpoint applies."""
    hub = ConversationStreamHub()
    await hub.start_turn("conv", turn_id="t1", user_id="u1", started_at=_now())

    other = await hub.start_turn(
        "conv",
        turn_id="t2",
        user_id="u2",
        started_at=_now(),
        reject_if_running=True,
    )
    assert other.turn_id == "t2"


@pytest.mark.asyncio
@pytest.mark.no_db
async def test_reject_if_running_still_reports_a_duplicate_turn_id() -> None:
    """A retried kickoff is one turn resent, not a rival: it must keep getting
    the idempotent ``TurnAlreadyExistsError`` rather than the conflict."""
    hub = ConversationStreamHub()
    await hub.start_turn("conv", turn_id="t1", user_id="u1", started_at=_now())

    with pytest.raises(TurnAlreadyExistsError):
        await hub.start_turn(
            "conv",
            turn_id="t1",
            user_id="u1",
            started_at=_now(),
            reject_if_running=True,
        )


@pytest.mark.asyncio
@pytest.mark.no_db
async def test_latest_seq_tracks_the_stream_head() -> None:
    """``latest_seq`` is the floor the steer endpoint hands clients: every event
    published after reading it carries a strictly greater seq."""
    hub = ConversationStreamHub()
    assert hub.latest_seq("conv") == -1

    await hub.start_turn("conv", turn_id="t1", user_id="u1", started_at=_now())
    floor = hub.latest_seq("conv")

    published = await hub.publish(
        "conv", "text", turn_id="t1", payload={"content": "x"}
    )
    assert published.seq > floor
    assert hub.latest_seq("conv") == published.seq
