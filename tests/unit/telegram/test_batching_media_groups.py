"""Tests for Telegram media group batching behavior.

These tests verify that ``DefaultMessageBatcher`` and ``NoBatchMessageBatcher``
correctly group Telegram album messages (those sharing a ``media_group_id``)
into a single batch, so the assistant receives them as one combined message
rather than several fragmented messages.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import TYPE_CHECKING, cast
from unittest.mock import AsyncMock

import pytest
from telegram import Chat, Message, PhotoSize, Update, User

from family_assistant.telegram.batching import (
    DefaultMessageBatcher,
    NoBatchMessageBatcher,
)
from tests.helpers import wait_for_condition

if TYPE_CHECKING:
    from telegram.ext import ContextTypes

    from family_assistant.telegram.types import AttachmentData


def _make_context() -> ContextTypes.DEFAULT_TYPE:
    return cast("ContextTypes.DEFAULT_TYPE", SimpleNamespace())


def _make_photo_update(
    update_id: int,
    message_id: int,
    media_group_id: str | None,
    chat_id: int = 123,
) -> Update:
    user = User(id=12345, first_name="TestUser", is_bot=False)
    chat = Chat(id=chat_id, type="private")
    photo = PhotoSize(
        file_id=f"photo_{message_id}",
        file_unique_id=f"photo_{message_id}_uniq",
        width=10,
        height=10,
        file_size=64,
    )
    message = Message(
        message_id=message_id,
        date=datetime.now(UTC),
        chat=chat,
        from_user=user,
        photo=[photo],
        media_group_id=media_group_id,
    )
    return Update(update_id=update_id, message=message)


def _make_text_update(
    update_id: int,
    message_id: int,
    text: str,
    chat_id: int = 123,
) -> Update:
    user = User(id=12345, first_name="TestUser", is_bot=False)
    chat = Chat(id=chat_id, type="private")
    message = Message(
        message_id=message_id,
        date=datetime.now(UTC),
        chat=chat,
        from_user=user,
        text=text,
    )
    return Update(update_id=update_id, message=message)


@pytest.mark.asyncio
async def test_no_batch_batcher_flushes_quickly_when_downloads_complete() -> None:
    """When notify_pending and add_to_batch are paired, the album flushes
    on the short quiet delay rather than waiting for the long max-wait."""
    processor = AsyncMock()
    batcher = NoBatchMessageBatcher(
        batch_processor=processor,
        media_group_quiet_seconds=0.1,
        media_group_max_wait_seconds=30.0,
    )
    context = _make_context()

    # Each message handler notifies first, then adds to batch (pairs balance out).
    for i in range(4):
        update = _make_photo_update(
            update_id=i + 1, message_id=100 + i, media_group_id="group-A"
        )
        await batcher.notify_pending_media_group(
            chat_id=123, media_group_id="group-A", context=context
        )
        await batcher.add_to_batch(update, context, attachments=None)

    await wait_for_condition(
        lambda: processor.process_batch.await_count >= 1,
        timeout=2.0,
        description="album flushes via quiet delay",
    )

    assert processor.process_batch.await_count == 1
    chat_id, batch, _ctx = processor.process_batch.await_args.args
    assert chat_id == 123
    assert len(batch) == 4
    assert [u.update_id for u, _ in batch] == [1, 2, 3, 4]


@pytest.mark.asyncio
async def test_no_batch_batcher_waits_for_outstanding_downloads() -> None:
    """While downloads are outstanding (notify without matching add_to_batch),
    the batcher uses the long max-wait, not the short quiet delay."""
    processor = AsyncMock()
    batcher = NoBatchMessageBatcher(
        batch_processor=processor,
        media_group_quiet_seconds=0.001,
        media_group_max_wait_seconds=10.0,
    )
    context = _make_context()

    # Notify for two messages, only deliver one (the second is "still downloading").
    await batcher.notify_pending_media_group(
        chat_id=123, media_group_id="group-B", context=context
    )
    await batcher.notify_pending_media_group(
        chat_id=123, media_group_id="group-B", context=context
    )

    update = _make_photo_update(update_id=1, message_id=100, media_group_id="group-B")
    await batcher.add_to_batch(update, context, attachments=None)

    # One outstanding download remains, so the batcher must keep waiting on the
    # long max-wait timer rather than firing on the (essentially zero) quiet delay.
    # Inspect the scheduled deadline directly to confirm we're on max-wait.
    key = (123, "group-B")
    assert batcher.media_group_pending_downloads[key] == 1
    loop = asyncio.get_running_loop()
    timer = batcher.media_group_timers[key]
    remaining = timer.when() - loop.time()
    assert remaining > batcher.media_group_quiet_seconds + 1.0
    assert processor.process_batch.await_count == 0

    # Deliver the slow message; this drops outstanding downloads to zero,
    # which switches the timer to the quiet delay.
    update_2 = _make_photo_update(update_id=2, message_id=101, media_group_id="group-B")
    await batcher.add_to_batch(update_2, context, attachments=None)

    await wait_for_condition(
        lambda: processor.process_batch.await_count >= 1,
        timeout=2.0,
        description="album flushes once last download arrives",
    )
    _, batch, _ = processor.process_batch.await_args.args
    assert len(batch) == 2
    assert [u.update_id for u, _ in batch] == [1, 2]


@pytest.mark.asyncio
async def test_no_batch_batcher_max_wait_flushes_when_download_never_arrives() -> None:
    """If a download never arrives, the album flushes after max_wait elapses."""
    processor = AsyncMock()
    batcher = NoBatchMessageBatcher(
        batch_processor=processor,
        media_group_quiet_seconds=10.0,
        media_group_max_wait_seconds=0.3,
    )
    context = _make_context()

    await batcher.notify_pending_media_group(
        chat_id=123, media_group_id="group-C", context=context
    )
    await batcher.notify_pending_media_group(
        chat_id=123, media_group_id="group-C", context=context
    )

    update = _make_photo_update(update_id=1, message_id=100, media_group_id="group-C")
    await batcher.add_to_batch(update, context, attachments=None)

    await wait_for_condition(
        lambda: processor.process_batch.await_count >= 1,
        timeout=3.0,
        description="album flushes via max-wait safety net",
    )
    _, batch, _ = processor.process_batch.await_args.args
    assert len(batch) == 1


@pytest.mark.asyncio
async def test_no_batch_batcher_processes_non_album_messages_immediately() -> None:
    """Plain text messages (no media_group_id) skip buffering."""
    processor = AsyncMock()
    batcher = NoBatchMessageBatcher(
        batch_processor=processor,
        media_group_quiet_seconds=5.0,
        media_group_max_wait_seconds=30.0,
    )
    context = _make_context()

    update = _make_text_update(update_id=1, message_id=101, text="hello")
    await batcher.add_to_batch(update, context, attachments=None)

    assert processor.process_batch.await_count == 1
    chat_id, batch, _ctx = processor.process_batch.await_args.args
    assert chat_id == 123
    assert len(batch) == 1
    assert batch[0][0].update_id == 1


@pytest.mark.asyncio
async def test_no_batch_batcher_flushes_album_when_text_arrives() -> None:
    """A non-album message arriving mid-album flushes the album first."""
    processor = AsyncMock()
    batcher = NoBatchMessageBatcher(
        batch_processor=processor,
        media_group_quiet_seconds=5.0,
        media_group_max_wait_seconds=30.0,
    )
    context = _make_context()

    for i in range(3):
        photo = _make_photo_update(
            update_id=i + 1, message_id=100 + i, media_group_id="group-D"
        )
        await batcher.add_to_batch(photo, context, attachments=None)

    text = _make_text_update(update_id=10, message_id=200, text="caption follow-up")
    await batcher.add_to_batch(text, context, attachments=None)

    assert processor.process_batch.await_count == 2
    first_call = processor.process_batch.await_args_list[0]
    second_call = processor.process_batch.await_args_list[1]
    assert len(first_call.args[1]) == 3
    assert [u.update_id for u, _ in first_call.args[1]] == [1, 2, 3]
    assert len(second_call.args[1]) == 1
    assert second_call.args[1][0][0].update_id == 10


@pytest.mark.asyncio
async def test_default_batcher_uses_quiet_delay_when_no_pending_downloads() -> None:
    """For album messages with no outstanding downloads, the quiet delay is used."""
    processor = AsyncMock()
    batcher = DefaultMessageBatcher(
        batch_processor=processor,
        batch_delay_seconds=0.05,
        media_group_quiet_seconds=0.3,
        media_group_max_wait_seconds=30.0,
    )
    context = _make_context()

    update_album = _make_photo_update(
        update_id=1, message_id=100, media_group_id="group-E"
    )
    await batcher.add_to_batch(update_album, context, attachments=None)

    # The active timer's deadline must reflect the quiet-delay (0.3), not
    # batch_delay_seconds (0.05). Check directly against the running loop.
    loop = asyncio.get_running_loop()
    timer = batcher.batch_timers[123]
    remaining = timer.when() - loop.time()
    assert remaining > batcher.batch_delay_seconds + 0.05
    assert remaining <= batcher.media_group_quiet_seconds + 0.05

    await wait_for_condition(
        lambda: processor.process_batch.await_count >= 1,
        timeout=2.0,
        description="album batch flushes via quiet delay",
    )
    assert processor.process_batch.await_count == 1


@pytest.mark.asyncio
async def test_default_batcher_uses_max_wait_when_downloads_outstanding() -> None:
    """While outstanding downloads exist, the long max-wait is used."""
    processor = AsyncMock()
    batcher = DefaultMessageBatcher(
        batch_processor=processor,
        batch_delay_seconds=0.05,
        media_group_quiet_seconds=0.05,
        media_group_max_wait_seconds=10.0,
    )
    context = _make_context()

    # Two notify_pending calls means two outstanding downloads.
    await batcher.notify_pending_media_group(
        chat_id=123, media_group_id="group-F", context=context
    )
    await batcher.notify_pending_media_group(
        chat_id=123, media_group_id="group-F", context=context
    )

    update_album = _make_photo_update(
        update_id=1, message_id=100, media_group_id="group-F"
    )
    await batcher.add_to_batch(update_album, context, attachments=None)

    # One outstanding download remains; the timer must reflect the max-wait,
    # not the quiet delay (0.05). Inspect the scheduled deadline directly.
    assert batcher.pending_media_group_downloads[123] == 1
    loop = asyncio.get_running_loop()
    timer = batcher.batch_timers[123]
    remaining = timer.when() - loop.time()
    assert remaining > batcher.media_group_quiet_seconds + 1.0


@pytest.mark.asyncio
async def test_default_batcher_uses_short_delay_for_non_group_messages() -> None:
    """Plain text messages still flush quickly using ``batch_delay_seconds``."""
    processor = AsyncMock()
    batcher = DefaultMessageBatcher(
        batch_processor=processor,
        batch_delay_seconds=0.05,
        media_group_quiet_seconds=2.0,
        media_group_max_wait_seconds=30.0,
    )
    context = _make_context()

    update = _make_text_update(update_id=1, message_id=200, text="hi")
    await batcher.add_to_batch(update, context, attachments=None)

    await wait_for_condition(
        lambda: processor.process_batch.await_count >= 1,
        timeout=1.0,
        description="text message flushes using short delay",
    )
    assert processor.process_batch.await_count == 1


@pytest.mark.asyncio
async def test_no_batch_batcher_attachments_preserved() -> None:
    """Per-message attachments survive media-group buffering."""
    processor = AsyncMock()
    batcher = NoBatchMessageBatcher(
        batch_processor=processor,
        media_group_quiet_seconds=0.1,
        media_group_max_wait_seconds=30.0,
    )
    context = _make_context()

    expected_attachments: list[list[AttachmentData] | None] = []
    for i in range(3):
        update = _make_photo_update(
            update_id=i + 1, message_id=100 + i, media_group_id="group-G"
        )
        attachments = [
            cast(
                "AttachmentData",
                SimpleNamespace(
                    content=b"data",
                    filename=f"photo_{i}.jpg",
                    mime_type="image/jpeg",
                    description=None,
                ),
            )
        ]
        expected_attachments.append(attachments)
        await batcher.notify_pending_media_group(
            chat_id=123, media_group_id="group-G", context=context
        )
        await batcher.add_to_batch(update, context, attachments=attachments)

    await wait_for_condition(
        lambda: processor.process_batch.await_count >= 1,
        timeout=2.0,
        description="album with attachments flushes",
    )

    _, batch, _ = processor.process_batch.await_args.args
    actual_attachments = [a for _, a in batch]
    assert actual_attachments == expected_attachments


@pytest.mark.asyncio
async def test_default_batcher_cancel_clears_pending_state_when_no_buffer() -> None:
    """If notify_pending fires but no add_to_batch follows (e.g. download
    failed), cancel must clear the chat's pending album state so the next
    plain-text message uses ``batch_delay_seconds`` instead of the long
    ``media_group_max_wait_seconds``.
    """
    processor = AsyncMock()
    batcher = DefaultMessageBatcher(
        batch_processor=processor,
        batch_delay_seconds=0.05,
        media_group_quiet_seconds=10.0,
        media_group_max_wait_seconds=60.0,
    )
    context = _make_context()

    await batcher.notify_pending_media_group(
        chat_id=123, media_group_id="group-cancel-A", context=context
    )
    await batcher.cancel_pending_media_group(
        chat_id=123, media_group_id="group-cancel-A", context=context
    )

    assert 123 not in batcher.pending_media_groups
    assert 123 not in batcher.pending_media_group_downloads
    assert 123 not in batcher.batch_timers

    text = _make_text_update(update_id=1, message_id=200, text="hi")
    await batcher.add_to_batch(text, context, attachments=None)

    await wait_for_condition(
        lambda: processor.process_batch.await_count >= 1,
        timeout=1.0,
        description="plain text after cancel flushes via short delay",
    )
    assert processor.process_batch.await_count == 1


@pytest.mark.asyncio
async def test_no_batch_batcher_cancel_clears_pending_state_when_no_buffer() -> None:
    """Same as above but for ``NoBatchMessageBatcher``: a cancel with no
    buffered album messages must release the chat back to pass-through mode.
    """
    processor = AsyncMock()
    batcher = NoBatchMessageBatcher(
        batch_processor=processor,
        media_group_quiet_seconds=10.0,
        media_group_max_wait_seconds=60.0,
    )
    context = _make_context()

    await batcher.notify_pending_media_group(
        chat_id=123, media_group_id="group-cancel-B", context=context
    )
    await batcher.cancel_pending_media_group(
        chat_id=123, media_group_id="group-cancel-B", context=context
    )

    key = (123, "group-cancel-B")
    assert 123 not in batcher.active_media_group_ids
    assert key not in batcher.media_group_pending_downloads
    assert key not in batcher.media_group_timers

    text = _make_text_update(update_id=1, message_id=200, text="hi")
    await batcher.add_to_batch(text, context, attachments=None)

    # Plain text takes the immediate-process path, so no waiting is needed.
    assert processor.process_batch.await_count == 1


@pytest.mark.asyncio
async def test_default_batcher_cancel_preserves_buffered_album_messages() -> None:
    """When some album messages have already been added to the batch, cancel
    must NOT discard them — but the timer should fall back to the quiet delay
    once outstanding downloads reach zero.
    """
    processor = AsyncMock()
    batcher = DefaultMessageBatcher(
        batch_processor=processor,
        batch_delay_seconds=0.05,
        media_group_quiet_seconds=0.2,
        media_group_max_wait_seconds=60.0,
    )
    context = _make_context()

    # Two photos notified, only one downloads successfully.
    await batcher.notify_pending_media_group(
        chat_id=123, media_group_id="group-partial", context=context
    )
    await batcher.notify_pending_media_group(
        chat_id=123, media_group_id="group-partial", context=context
    )

    update = _make_photo_update(
        update_id=1, message_id=100, media_group_id="group-partial"
    )
    await batcher.add_to_batch(update, context, attachments=None)

    # Second download fails; handler cancels its notify.
    await batcher.cancel_pending_media_group(
        chat_id=123, media_group_id="group-partial", context=context
    )

    await wait_for_condition(
        lambda: processor.process_batch.await_count >= 1,
        timeout=2.0,
        description="partial album flushes via quiet delay after cancel",
    )
    _, batch, _ = processor.process_batch.await_args.args
    assert len(batch) == 1
    assert batch[0][0].update_id == 1


@pytest.mark.asyncio
async def test_no_batch_batcher_keeps_overlapping_albums_isolated() -> None:
    """Each album lives in its own buffer keyed by (chat_id, media_group_id),
    so two albums arriving back-to-back are NOT merged into one batch and
    are NOT flushed prematurely just because the other started.
    """
    processor = AsyncMock()
    batcher = NoBatchMessageBatcher(
        batch_processor=processor,
        media_group_quiet_seconds=0.1,
        media_group_max_wait_seconds=60.0,
    )
    context = _make_context()

    # Album A: two photos.
    for i in range(2):
        update = _make_photo_update(
            update_id=i + 1, message_id=100 + i, media_group_id="album-A"
        )
        await batcher.add_to_batch(update, context, attachments=None)

    # Album B's first photo arrives before album A's quiet timer fires.
    update_b1 = _make_photo_update(
        update_id=10, message_id=200, media_group_id="album-B"
    )
    await batcher.add_to_batch(update_b1, context, attachments=None)

    # Both albums are now buffered in their own per-album slots; nothing has
    # been flushed yet.
    assert processor.process_batch.await_count == 0
    assert sorted(batcher.active_media_group_ids[123]) == ["album-A", "album-B"]
    assert len(batcher.media_group_buffers[(123, "album-A")]) == 2
    assert len(batcher.media_group_buffers[(123, "album-B")]) == 1

    # Wait for both timers to fire — each album flushes as its own batch.
    await wait_for_condition(
        lambda: processor.process_batch.await_count >= 2,
        timeout=2.0,
        description="both albums flush as separate batches",
    )

    # Group the two flushed batches by their album to keep the assertions
    # independent of timer-fire order.
    batches_by_album: dict[str, list[int]] = {}
    for call in processor.process_batch.await_args_list:
        batch = call.args[1]
        group_id = batch[0][0].message.media_group_id
        batches_by_album[group_id] = [u.update_id for u, _ in batch]
    assert batches_by_album["album-A"] == [1, 2]
    assert batches_by_album["album-B"] == [10]


@pytest.mark.asyncio
async def test_no_batch_batcher_preserves_album_a_when_album_b_starts_mid_download() -> (
    None
):
    """If album A still has outstanding downloads when album B starts, A's
    partial buffer must NOT be flushed as a fragment — A keeps accumulating
    until its own pending downloads complete (or the max-wait fires), and B
    accumulates independently.
    """
    processor = AsyncMock()
    batcher = NoBatchMessageBatcher(
        batch_processor=processor,
        media_group_quiet_seconds=0.1,
        media_group_max_wait_seconds=10.0,
    )
    context = _make_context()

    # Album A has 3 messages pre-armed; only 2 of them have finished
    # downloading (so 1 is still outstanding).
    for _ in range(3):
        await batcher.notify_pending_media_group(
            chat_id=123, media_group_id="album-A", context=context
        )
    for i in range(2):
        update = _make_photo_update(
            update_id=i + 1, message_id=100 + i, media_group_id="album-A"
        )
        await batcher.add_to_batch(update, context, attachments=None)

    # Album B's first message arrives before A's third item finishes.
    update_b1 = _make_photo_update(
        update_id=10, message_id=200, media_group_id="album-B"
    )
    await batcher.add_to_batch(update_b1, context, attachments=None)

    # Album A is preserved (NOT partially flushed). Its outstanding-download
    # counter is still 1 so its timer is the long max-wait.
    assert processor.process_batch.await_count == 0
    a_key = (123, "album-A")
    b_key = (123, "album-B")
    assert batcher.media_group_pending_downloads[a_key] == 1
    assert len(batcher.media_group_buffers[a_key]) == 2
    assert len(batcher.media_group_buffers[b_key]) == 1
    assert sorted(batcher.active_media_group_ids[123]) == ["album-A", "album-B"]

    # A's third item finally arrives; its counter drops to 0 and the quiet
    # timer takes over.
    update_a3 = _make_photo_update(
        update_id=3, message_id=102, media_group_id="album-A"
    )
    await batcher.add_to_batch(update_a3, context, attachments=None)

    await wait_for_condition(
        lambda: processor.process_batch.await_count >= 2,
        timeout=2.0,
        description="both albums flush as separate batches",
    )

    batches_by_album: dict[str, list[int]] = {}
    for call in processor.process_batch.await_args_list:
        batch = call.args[1]
        group_id = batch[0][0].message.media_group_id
        batches_by_album[group_id] = [u.update_id for u, _ in batch]
    # Album A is delivered as a single, complete batch — not fragmented.
    assert batches_by_album["album-A"] == [1, 2, 3]
    assert batches_by_album["album-B"] == [10]


@pytest.mark.asyncio
async def test_no_batch_batcher_text_preserves_pre_armed_album_with_no_buffer() -> None:
    """A plain text message arriving while an album has been pre-armed but
    none of its items have arrived yet must NOT abandon the album. Its
    pending-download counter and timer must remain intact so the album is
    still delivered as a single batch when its items finally land — instead
    of fragmenting into individual quiet-delay flushes.
    """
    processor = AsyncMock()
    batcher = NoBatchMessageBatcher(
        batch_processor=processor,
        media_group_quiet_seconds=0.1,
        media_group_max_wait_seconds=10.0,
    )
    context = _make_context()

    # Album A pre-armed for three items; none have downloaded yet.
    for _ in range(3):
        await batcher.notify_pending_media_group(
            chat_id=123, media_group_id="album-A", context=context
        )

    # Text arrives before any of A's downloads complete.
    text = _make_text_update(update_id=99, message_id=300, text="caption")
    await batcher.add_to_batch(text, context, attachments=None)

    # Text was processed immediately as its own batch; album A's pre-arm
    # state is preserved.
    assert processor.process_batch.await_count == 1
    _, text_batch, _ = processor.process_batch.await_args_list[0].args
    assert [u.update_id for u, _ in text_batch] == [99]

    a_key = (123, "album-A")
    assert batcher.media_group_pending_downloads[a_key] == 3
    assert "album-A" in batcher.active_media_group_ids[123]

    # A's three items finally download; they all land in A's slot and the
    # counter drops to 0, so A flushes as a single batch via quiet delay.
    for i in range(3):
        update = _make_photo_update(
            update_id=i + 1, message_id=100 + i, media_group_id="album-A"
        )
        await batcher.add_to_batch(update, context, attachments=None)

    await wait_for_condition(
        lambda: processor.process_batch.await_count >= 2,
        timeout=2.0,
        description="album A flushes as a complete batch",
    )

    assert processor.process_batch.await_count == 2
    _, batch_a, _ = processor.process_batch.await_args_list[1].args
    assert [u.update_id for u, _ in batch_a] == [1, 2, 3]


@pytest.mark.asyncio
async def test_no_batch_batcher_text_preserves_album_with_partial_buffer_and_pending() -> (
    None
):
    """A plain text message arriving while an album has SOME buffered items
    AND outstanding downloads must NOT flush the partial buffer. The album
    must keep its existing items, counter, and timer so when the remaining
    items download they all flush together as one batch.
    """
    processor = AsyncMock()
    batcher = NoBatchMessageBatcher(
        batch_processor=processor,
        media_group_quiet_seconds=0.1,
        media_group_max_wait_seconds=10.0,
    )
    context = _make_context()

    # Album A has 3 items pre-armed; only the first 2 have downloaded so far.
    for _ in range(3):
        await batcher.notify_pending_media_group(
            chat_id=123, media_group_id="album-A", context=context
        )
    for i in range(2):
        update = _make_photo_update(
            update_id=i + 1, message_id=100 + i, media_group_id="album-A"
        )
        await batcher.add_to_batch(update, context, attachments=None)

    # Text arrives before A's third item finishes downloading.
    text = _make_text_update(update_id=99, message_id=300, text="caption")
    await batcher.add_to_batch(text, context, attachments=None)

    # Text was processed immediately as its own batch; A's partial buffer
    # is preserved (NOT flushed as a fragment) along with its pending
    # counter.
    assert processor.process_batch.await_count == 1
    _, text_batch, _ = processor.process_batch.await_args_list[0].args
    assert [u.update_id for u, _ in text_batch] == [99]

    a_key = (123, "album-A")
    assert batcher.media_group_pending_downloads[a_key] == 1
    assert len(batcher.media_group_buffers[a_key]) == 2
    assert "album-A" in batcher.active_media_group_ids[123]

    # A's third item finally downloads; counter drops to 0 and the album
    # flushes as a single batch via the quiet timer.
    update_a3 = _make_photo_update(
        update_id=3, message_id=102, media_group_id="album-A"
    )
    await batcher.add_to_batch(update_a3, context, attachments=None)

    await wait_for_condition(
        lambda: processor.process_batch.await_count >= 2,
        timeout=2.0,
        description="album A flushes as a complete batch after text",
    )

    assert processor.process_batch.await_count == 2
    _, batch_a, _ = processor.process_batch.await_args_list[1].args
    assert [u.update_id for u, _ in batch_a] == [1, 2, 3]


@pytest.mark.asyncio
async def test_no_batch_batcher_text_flushes_active_albums_in_arrival_order() -> None:
    """A non-album message arriving while two albums are buffered must flush
    BOTH albums as separate batches in the order they were started, before
    the non-album message is processed.
    """
    processor = AsyncMock()
    batcher = NoBatchMessageBatcher(
        batch_processor=processor,
        media_group_quiet_seconds=10.0,
        media_group_max_wait_seconds=60.0,
    )
    context = _make_context()

    # Album A first, then album B, then a plain-text message.
    for i in range(2):
        update = _make_photo_update(
            update_id=i + 1, message_id=100 + i, media_group_id="album-A"
        )
        await batcher.add_to_batch(update, context, attachments=None)
    update_b1 = _make_photo_update(
        update_id=10, message_id=200, media_group_id="album-B"
    )
    await batcher.add_to_batch(update_b1, context, attachments=None)

    text = _make_text_update(update_id=99, message_id=300, text="that's all")
    await batcher.add_to_batch(text, context, attachments=None)

    # Three batches: album A, album B, text — in that order.
    assert processor.process_batch.await_count == 3
    batches = [call.args[1] for call in processor.process_batch.await_args_list]
    assert [u.update_id for u, _ in batches[0]] == [1, 2]
    assert [u.update_id for u, _ in batches[1]] == [10]
    assert [u.update_id for u, _ in batches[2]] == [99]
