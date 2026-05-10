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
    assert batcher.media_group_pending_downloads[123] == 1
    loop = asyncio.get_running_loop()
    timer = batcher.media_group_timers[123]
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

    assert 123 not in batcher.media_group_ids
    assert 123 not in batcher.media_group_pending_downloads
    assert 123 not in batcher.media_group_timers

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
async def test_no_batch_batcher_flushes_when_new_media_group_arrives() -> None:
    """Two albums arriving back-to-back must be delivered as two separate
    batches, not mixed into one.
    """
    processor = AsyncMock()
    batcher = NoBatchMessageBatcher(
        batch_processor=processor,
        media_group_quiet_seconds=10.0,
        media_group_max_wait_seconds=60.0,
    )
    context = _make_context()

    # Album A: two photos already in the buffer.
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

    # Album A should have been flushed as its own batch when B arrived.
    assert processor.process_batch.await_count == 1
    _, batch_a, _ = processor.process_batch.await_args_list[0].args
    assert [u.update_id for u, _ in batch_a] == [1, 2]

    # Album B's first message is now buffered alone.
    assert batcher.media_group_ids[123] == "album-B"
    assert len(batcher.media_group_buffers[123]) == 1
    assert batcher.media_group_buffers[123][0][0].update_id == 10


@pytest.mark.asyncio
async def test_no_batch_batcher_flushes_on_notify_pending_for_different_group() -> None:
    """Album B's notify_pending arriving while album A is buffered must flush A
    as its own batch, not merge it with B (which would happen if notify simply
    overwrote the active media_group_id).
    """
    processor = AsyncMock()
    batcher = NoBatchMessageBatcher(
        batch_processor=processor,
        media_group_quiet_seconds=10.0,
        media_group_max_wait_seconds=60.0,
    )
    context = _make_context()

    # Album A: two photos buffered via the standard notify+add pairing.
    for i in range(2):
        update = _make_photo_update(
            update_id=i + 1, message_id=100 + i, media_group_id="album-A"
        )
        await batcher.notify_pending_media_group(
            chat_id=123, media_group_id="album-A", context=context
        )
        await batcher.add_to_batch(update, context, attachments=None)

    # Album B's notify_pending arrives before any of B's downloads finish.
    await batcher.notify_pending_media_group(
        chat_id=123, media_group_id="album-B", context=context
    )

    # Album A must have been flushed as its own batch — the new pre-arm for
    # B should not have been able to merge it into the same buffer.
    assert processor.process_batch.await_count == 1
    _, batch_a, _ = processor.process_batch.await_args_list[0].args
    assert [u.update_id for u, _ in batch_a] == [1, 2]

    # The chat is now pre-armed for album B with a fresh, empty buffer.
    assert batcher.media_group_ids[123] == "album-B"
    assert not batcher.media_group_buffers.get(123)
    assert batcher.media_group_pending_downloads[123] == 1

    # When B's first message lands it joins B's buffer (not the flushed A's).
    update_b1 = _make_photo_update(
        update_id=10, message_id=200, media_group_id="album-B"
    )
    await batcher.add_to_batch(update_b1, context, attachments=None)
    assert batcher.media_group_buffers[123][0][0].update_id == 10
