from __future__ import annotations

import asyncio
import logging
from collections import defaultdict
from typing import TYPE_CHECKING

from family_assistant.telegram.protocols import BatchProcessor, MessageBatcher

if TYPE_CHECKING:
    from telegram import Update
    from telegram.ext import ContextTypes

    from family_assistant.telegram.types import AttachmentData


logger = logging.getLogger(__name__)


def _message_media_group_id(update: Update) -> str | None:
    """Extract the media_group_id from an update's message, if any."""
    return update.message.media_group_id if update.message else None


class DefaultMessageBatcher(MessageBatcher):
    """Buffers messages and processes them in batches to avoid race conditions.

    Telegram's Bot API does not expose an "album complete" marker, so the batcher
    decides when to flush an album using two delays:

    * ``media_group_quiet_seconds`` — how long to wait after the most recent
      activity, *when every album message we have been told to expect has
      already been added to the batch*. This keeps the user-visible delay short
      in the common case.
    * ``media_group_max_wait_seconds`` — how long to wait after the most recent
      activity *while we are still waiting for an attachment download to
      finish*. This is intentionally generous so videos and large files have
      time to download before the album is flushed.
    """

    def __init__(
        self,
        batch_processor: BatchProcessor,
        batch_delay_seconds: float = 0.5,
        media_group_quiet_seconds: float = 1.0,
        media_group_max_wait_seconds: float = 60.0,
    ) -> None:
        self.batch_processor = batch_processor
        self.batch_delay_seconds = batch_delay_seconds
        self.media_group_quiet_seconds = media_group_quiet_seconds
        self.media_group_max_wait_seconds = media_group_max_wait_seconds
        self.chat_locks: dict[int, asyncio.Lock] = defaultdict(asyncio.Lock)
        self.message_buffers: dict[
            int, list[tuple[Update, list[AttachmentData] | None]]
        ] = defaultdict(list)
        self.processing_tasks: dict[int, asyncio.Task] = {}
        self.batch_timers: dict[int, asyncio.TimerHandle] = {}
        self.pending_media_groups: dict[int, str] = {}
        self.pending_media_group_downloads: dict[int, int] = defaultdict(int)

    async def add_to_batch(
        self,
        update: Update,
        context: ContextTypes.DEFAULT_TYPE,
        attachments: list[AttachmentData] | None,
    ) -> None:
        if not update.effective_chat:
            logger.warning(
                "DefaultMessageBatcher: Update has no effective_chat, skipping."
            )
            return
        chat_id = update.effective_chat.id
        media_group_id = _message_media_group_id(update)
        async with self.chat_locks[chat_id]:
            self.message_buffers[chat_id].append((update, attachments))
            if media_group_id is not None:
                self.pending_media_groups[chat_id] = media_group_id
                if self.pending_media_group_downloads[chat_id] > 0:
                    self.pending_media_group_downloads[chat_id] -= 1
            buffer_size = len(self.message_buffers[chat_id])
            logger.info(
                f"Buffered update {update.update_id} (message {update.message.message_id if update.message else 'N/A'}) for chat {chat_id}. Buffer size: {buffer_size}"
            )
            self._schedule_batch_locked(chat_id, context)

    async def notify_pending_media_group(
        self,
        chat_id: int,
        media_group_id: str,
        context: ContextTypes.DEFAULT_TYPE,
    ) -> None:
        async with self.chat_locks[chat_id]:
            self.pending_media_groups[chat_id] = media_group_id
            self.pending_media_group_downloads[chat_id] += 1
            logger.debug(
                f"Pre-arming batch timer for chat {chat_id} pending media group "
                f"{media_group_id} (outstanding downloads: "
                f"{self.pending_media_group_downloads[chat_id]})."
            )
            self._schedule_batch_locked(chat_id, context)

    async def cancel_pending_media_group(
        self,
        chat_id: int,
        media_group_id: str,
        context: ContextTypes.DEFAULT_TYPE,
    ) -> None:
        async with self.chat_locks[chat_id]:
            if self.pending_media_group_downloads.get(chat_id, 0) > 0:
                self.pending_media_group_downloads[chat_id] -= 1
            logger.debug(
                f"Cancelled pending media-group download for chat {chat_id} "
                f"(group {media_group_id}, remaining outstanding: "
                f"{self.pending_media_group_downloads.get(chat_id, 0)})."
            )

            if self.pending_media_group_downloads.get(
                chat_id, 0
            ) == 0 and not self._has_buffered_album_message_locked(chat_id):
                # No more outstanding downloads and no album messages have
                # been added to the buffer: revert to plain-batching state.
                self.pending_media_groups.pop(chat_id, None)
                self.pending_media_group_downloads.pop(chat_id, None)

            if self.message_buffers.get(chat_id):
                self._schedule_batch_locked(chat_id, context)
            else:
                timer = self.batch_timers.pop(chat_id, None)
                if timer is not None:
                    timer.cancel()

    def _has_buffered_album_message_locked(self, chat_id: int) -> bool:
        for update, _ in self.message_buffers.get(chat_id, []):
            if update.message and update.message.media_group_id is not None:
                return True
        return False

    def _schedule_batch_locked(
        self, chat_id: int, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        delay = self._next_delay_locked(chat_id)

        if chat_id in self.batch_timers:
            self.batch_timers[chat_id].cancel()
            logger.debug(f"Cancelled existing batch timer for chat {chat_id}.")

        loop = asyncio.get_running_loop()
        self.batch_timers[chat_id] = loop.call_later(
            delay,
            lambda: asyncio.create_task(
                self._trigger_batch_processing(chat_id, context)
            ),
        )
        logger.debug(f"Scheduled batch processing for chat {chat_id} in {delay}s.")

    def _next_delay_locked(self, chat_id: int) -> float:
        if chat_id not in self.pending_media_groups:
            return self.batch_delay_seconds
        if self.pending_media_group_downloads.get(chat_id, 0) > 0:
            return self.media_group_max_wait_seconds
        return self.media_group_quiet_seconds

    async def _trigger_batch_processing(
        self, chat_id: int, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        """Gets the current batch and triggers the BatchProcessor if no task is running."""
        async with self.chat_locks[chat_id]:
            if chat_id in self.batch_timers:
                self.batch_timers.pop(chat_id)
            self.pending_media_groups.pop(chat_id, None)
            self.pending_media_group_downloads.pop(chat_id, None)

            current_batch = self.message_buffers[chat_id][:]
            self.message_buffers[chat_id].clear()
            logger.debug(
                f"Extracted batch of {len(current_batch)} for chat {chat_id}, cleared buffer."
            )

            if not current_batch:
                logger.info(
                    f"Batch for chat {chat_id} is empty, skipping processing trigger."
                )
                return

            if (
                chat_id not in self.processing_tasks
                or self.processing_tasks[chat_id].done()
            ):
                logger.info(
                    f"Starting new processing task for chat {chat_id} via batch trigger."
                )
                task = asyncio.create_task(
                    self.batch_processor.process_batch(chat_id, current_batch, context)
                )
                self.processing_tasks[chat_id] = task
                task.add_done_callback(
                    lambda t, c=chat_id: self._remove_task_callback(t, c)
                )
            else:
                logger.info(
                    f"Processing task already running for chat {chat_id}. Batch was cleared but not processed immediately."
                )
                self.message_buffers[chat_id] = (
                    current_batch + self.message_buffers[chat_id]
                )
                logger.warning(
                    f"Re-added batch to buffer for chat {chat_id} as task was still running."
                )

    def _remove_task_callback(self, task: asyncio.Task, chat_id: int) -> None:
        """Callback function to remove task from processing_tasks dict."""
        try:
            task.result()
        except asyncio.CancelledError:
            logger.info(f"Processing task for chat {chat_id} was cancelled.")
        except Exception:
            logger.debug(
                f"Processing task for chat {chat_id} completed with an exception (handled elsewhere)."
            )

        if hasattr(self, "processing_tasks"):
            self.processing_tasks.pop(chat_id, None)
            logger.debug(f"Task entry removed for chat {chat_id} via callback.")
        else:
            logger.warning(
                f"Cannot remove task entry for chat {chat_id}: processing_tasks dict not found."
            )


class NoBatchMessageBatcher(MessageBatcher):
    """Processes each message immediately, except for Telegram media groups.

    Media group messages (Telegram albums) are buffered briefly so that all
    photos and videos in the album are delivered to the assistant as a single
    message rather than as several fragmented ones.

    Each ``(chat_id, media_group_id)`` keeps its own buffer, outstanding
    download counter, and timer, so two albums arriving back-to-back stay
    isolated even if one is still downloading when the other starts. The
    flush timing follows the same quiet / max-wait scheme described on
    :class:`DefaultMessageBatcher`.
    """

    def __init__(
        self,
        batch_processor: BatchProcessor,
        media_group_quiet_seconds: float = 1.0,
        media_group_max_wait_seconds: float = 60.0,
    ) -> None:
        self.batch_processor = batch_processor
        self.media_group_quiet_seconds = media_group_quiet_seconds
        self.media_group_max_wait_seconds = media_group_max_wait_seconds
        self.chat_locks: dict[int, asyncio.Lock] = defaultdict(asyncio.Lock)
        self.media_group_buffers: dict[
            tuple[int, str], list[tuple[Update, list[AttachmentData] | None]]
        ] = defaultdict(list)
        self.media_group_timers: dict[tuple[int, str], asyncio.TimerHandle] = {}
        self.media_group_pending_downloads: dict[tuple[int, str], int] = defaultdict(
            int
        )
        # Per-chat FIFO of currently active media_group_ids. Insertion order
        # is preserved so that flushes triggered by an unrelated message land
        # in the order the user sent the albums.
        self.active_media_group_ids: dict[int, list[str]] = {}

    async def add_to_batch(
        self,
        update: Update,
        context: ContextTypes.DEFAULT_TYPE,
        attachments: list[AttachmentData] | None,
    ) -> None:
        if not update.effective_chat:
            logger.warning("NoBatchMessageBatcher: Update has no effective_chat.")
            return
        chat_id = update.effective_chat.id
        media_group_id = _message_media_group_id(update)

        if media_group_id is None and chat_id not in self.active_media_group_ids:
            logger.info(
                f"NoBatchMessageBatcher: Immediately processing update {update.update_id} for chat {chat_id}"
            )
            await self.batch_processor.process_batch(
                chat_id, [(update, attachments)], context
            )
            return

        pending_flushes: list[list[tuple[Update, list[AttachmentData] | None]]] = []
        immediate_batch: list[tuple[Update, list[AttachmentData] | None]] | None = None

        async with self.chat_locks[chat_id]:
            if media_group_id is None:
                # Non-album message arrived while one or more albums are
                # buffered for this chat. Flush every active album in arrival
                # order before processing this message, so the user's send
                # order is preserved in what the assistant sees.
                for active_group in list(self.active_media_group_ids.get(chat_id, [])):
                    extracted = self._extract_buffer_locked(chat_id, active_group)
                    if extracted:
                        pending_flushes.append(extracted)
                        logger.info(
                            f"NoBatchMessageBatcher: Flushing media group "
                            f"{active_group} of {len(extracted)} message(s) for "
                            f"chat {chat_id} due to non-album message arrival."
                        )
                immediate_batch = [(update, attachments)]
            else:
                key = (chat_id, media_group_id)
                self.media_group_buffers[key].append((update, attachments))
                self._add_active_album_locked(chat_id, media_group_id)
                if self.media_group_pending_downloads[key] > 0:
                    self.media_group_pending_downloads[key] -= 1
                logger.info(
                    f"NoBatchMessageBatcher: Buffered media group update "
                    f"{update.update_id} (group {media_group_id}) for chat "
                    f"{chat_id}. Buffer size: {len(self.media_group_buffers[key])}, "
                    f"outstanding downloads: "
                    f"{self.media_group_pending_downloads[key]}"
                )
                self._reset_media_group_timer_locked(chat_id, media_group_id, context)

        for batch in pending_flushes:
            await self.batch_processor.process_batch(chat_id, batch, context)
        if immediate_batch is not None:
            logger.info(
                f"NoBatchMessageBatcher: Immediately processing update {update.update_id} for chat {chat_id}"
            )
            await self.batch_processor.process_batch(chat_id, immediate_batch, context)

    async def notify_pending_media_group(
        self,
        chat_id: int,
        media_group_id: str,
        context: ContextTypes.DEFAULT_TYPE,
    ) -> None:
        async with self.chat_locks[chat_id]:
            key = (chat_id, media_group_id)
            self.media_group_pending_downloads[key] += 1
            self._add_active_album_locked(chat_id, media_group_id)
            logger.debug(
                f"NoBatchMessageBatcher: Pre-arming media group timer for chat "
                f"{chat_id} (group {media_group_id}, outstanding downloads: "
                f"{self.media_group_pending_downloads[key]})."
            )
            self._reset_media_group_timer_locked(chat_id, media_group_id, context)

    async def cancel_pending_media_group(
        self,
        chat_id: int,
        media_group_id: str,
        context: ContextTypes.DEFAULT_TYPE,
    ) -> None:
        async with self.chat_locks[chat_id]:
            key = (chat_id, media_group_id)
            if self.media_group_pending_downloads.get(key, 0) > 0:
                self.media_group_pending_downloads[key] -= 1
            logger.debug(
                f"NoBatchMessageBatcher: Cancelled pending media-group download "
                f"for chat {chat_id} (group {media_group_id}, remaining "
                f"outstanding: {self.media_group_pending_downloads.get(key, 0)})."
            )

            if self.media_group_buffers.get(key):
                # Buffered album messages still need flushing; just reschedule
                # the timer with the (now possibly shorter) delay.
                self._reset_media_group_timer_locked(chat_id, media_group_id, context)
                return

            # No album messages buffered; if no more outstanding downloads,
            # abandon this album entirely so the chat is not treated as having
            # an in-flight album for unrelated future messages.
            if self.media_group_pending_downloads.get(key, 0) == 0:
                self.media_group_pending_downloads.pop(key, None)
                timer = self.media_group_timers.pop(key, None)
                if timer is not None:
                    timer.cancel()
                self._remove_active_album_locked(chat_id, media_group_id)

    def _add_active_album_locked(self, chat_id: int, media_group_id: str) -> None:
        groups = self.active_media_group_ids.setdefault(chat_id, [])
        if media_group_id not in groups:
            groups.append(media_group_id)

    def _remove_active_album_locked(self, chat_id: int, media_group_id: str) -> None:
        groups = self.active_media_group_ids.get(chat_id)
        if not groups:
            return
        if media_group_id in groups:
            groups.remove(media_group_id)
        if not groups:
            self.active_media_group_ids.pop(chat_id, None)

    def _reset_media_group_timer_locked(
        self, chat_id: int, media_group_id: str, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        key = (chat_id, media_group_id)
        if key in self.media_group_timers:
            self.media_group_timers[key].cancel()
        delay = (
            self.media_group_quiet_seconds
            if self.media_group_pending_downloads.get(key, 0) == 0
            else self.media_group_max_wait_seconds
        )
        loop = asyncio.get_running_loop()
        self.media_group_timers[key] = loop.call_later(
            delay,
            lambda: asyncio.create_task(
                self._flush_media_group(chat_id, media_group_id, context)
            ),
        )

    def _extract_buffer_locked(
        self, chat_id: int, media_group_id: str
    ) -> list[tuple[Update, list[AttachmentData] | None]]:
        key = (chat_id, media_group_id)
        buffer = self.media_group_buffers.pop(key, [])
        self.media_group_pending_downloads.pop(key, None)
        timer = self.media_group_timers.pop(key, None)
        if timer is not None:
            timer.cancel()
        self._remove_active_album_locked(chat_id, media_group_id)
        return buffer

    async def _flush_media_group(
        self, chat_id: int, media_group_id: str, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        key = (chat_id, media_group_id)
        async with self.chat_locks[chat_id]:
            self.media_group_timers.pop(key, None)
            buffer = self.media_group_buffers.pop(key, [])
            outstanding = self.media_group_pending_downloads.pop(key, 0)
            self._remove_active_album_locked(chat_id, media_group_id)
            if not buffer:
                return
            if outstanding > 0:
                logger.warning(
                    f"NoBatchMessageBatcher: Flushing media group {media_group_id} "
                    f"for chat {chat_id} after max wait elapsed; {outstanding} "
                    f"download(s) never completed."
                )
            else:
                logger.info(
                    f"NoBatchMessageBatcher: Flushing media group {media_group_id} "
                    f"of {len(buffer)} message(s) for chat {chat_id}."
                )
        await self.batch_processor.process_batch(chat_id, buffer, context)
