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
