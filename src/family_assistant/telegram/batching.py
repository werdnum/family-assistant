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
    """Buffers messages and processes them in batches to avoid race conditions."""

    def __init__(
        self,
        batch_processor: BatchProcessor,
        batch_delay_seconds: float = 0.5,
        media_group_delay_seconds: float = 3.0,
    ) -> None:
        self.batch_processor = batch_processor
        self.batch_delay_seconds = batch_delay_seconds
        self.media_group_delay_seconds = media_group_delay_seconds
        self.chat_locks: dict[int, asyncio.Lock] = defaultdict(asyncio.Lock)
        self.message_buffers: dict[
            int, list[tuple[Update, list[AttachmentData] | None]]
        ] = defaultdict(list)
        self.processing_tasks: dict[int, asyncio.Task] = {}
        self.batch_timers: dict[int, asyncio.TimerHandle] = {}
        self.pending_media_groups: dict[int, str] = {}

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
            logger.debug(
                f"Pre-arming batch timer for chat {chat_id} pending media group {media_group_id}."
            )
            self._schedule_batch_locked(chat_id, context)

    def _schedule_batch_locked(
        self, chat_id: int, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        delay = (
            self.media_group_delay_seconds
            if chat_id in self.pending_media_groups
            else self.batch_delay_seconds
        )

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

    async def _trigger_batch_processing(
        self, chat_id: int, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        """Gets the current batch and triggers the BatchProcessor if no task is running."""
        async with self.chat_locks[chat_id]:
            if chat_id in self.batch_timers:
                self.batch_timers.pop(chat_id)
            self.pending_media_groups.pop(chat_id, None)

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

    Media group messages (Telegram albums) are still buffered briefly so that all
    photos in the album are delivered to the assistant as a single message rather
    than as several fragmented ones.
    """

    def __init__(
        self,
        batch_processor: BatchProcessor,
        media_group_delay_seconds: float = 3.0,
    ) -> None:
        self.batch_processor = batch_processor
        self.media_group_delay_seconds = media_group_delay_seconds
        self.chat_locks: dict[int, asyncio.Lock] = defaultdict(asyncio.Lock)
        self.media_group_buffers: dict[
            int, list[tuple[Update, list[AttachmentData] | None]]
        ] = defaultdict(list)
        self.media_group_ids: dict[int, str] = {}
        self.media_group_timers: dict[int, asyncio.TimerHandle] = {}

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

        if media_group_id is None and chat_id not in self.media_group_ids:
            logger.info(
                f"NoBatchMessageBatcher: Immediately processing update {update.update_id} for chat {chat_id}"
            )
            batch = [(update, attachments)]
            await self.batch_processor.process_batch(chat_id, batch, context)
            return

        async with self.chat_locks[chat_id]:
            if media_group_id is None:
                # A non-album message arrived while we were buffering an album:
                # flush the album first, then process this message.
                pending_batch = self._extract_buffer_locked(chat_id)
                if pending_batch:
                    logger.info(
                        f"NoBatchMessageBatcher: Flushing media group of "
                        f"{len(pending_batch)} message(s) for chat {chat_id} due to "
                        f"non-album message arrival."
                    )
                    await self.batch_processor.process_batch(
                        chat_id, pending_batch, context
                    )
                logger.info(
                    f"NoBatchMessageBatcher: Immediately processing update {update.update_id} for chat {chat_id}"
                )
                await self.batch_processor.process_batch(
                    chat_id, [(update, attachments)], context
                )
                return

            # Album message: buffer and (re)arm the flush timer.
            self.media_group_buffers[chat_id].append((update, attachments))
            self.media_group_ids[chat_id] = media_group_id
            logger.info(
                f"NoBatchMessageBatcher: Buffered media group update {update.update_id} "
                f"(group {media_group_id}) for chat {chat_id}. Buffer size: "
                f"{len(self.media_group_buffers[chat_id])}"
            )
            self._reset_media_group_timer_locked(chat_id, context)

    async def notify_pending_media_group(
        self,
        chat_id: int,
        media_group_id: str,
        context: ContextTypes.DEFAULT_TYPE,
    ) -> None:
        async with self.chat_locks[chat_id]:
            self.media_group_ids[chat_id] = media_group_id
            logger.debug(
                f"NoBatchMessageBatcher: Pre-arming media group timer for chat "
                f"{chat_id} (group {media_group_id})."
            )
            self._reset_media_group_timer_locked(chat_id, context)

    def _reset_media_group_timer_locked(
        self, chat_id: int, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        if chat_id in self.media_group_timers:
            self.media_group_timers[chat_id].cancel()
        loop = asyncio.get_running_loop()
        self.media_group_timers[chat_id] = loop.call_later(
            self.media_group_delay_seconds,
            lambda: asyncio.create_task(self._flush_media_group(chat_id, context)),
        )

    def _extract_buffer_locked(
        self, chat_id: int
    ) -> list[tuple[Update, list[AttachmentData] | None]]:
        buffer = self.media_group_buffers.pop(chat_id, [])
        self.media_group_ids.pop(chat_id, None)
        timer = self.media_group_timers.pop(chat_id, None)
        if timer is not None:
            timer.cancel()
        return buffer

    async def _flush_media_group(
        self, chat_id: int, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        async with self.chat_locks[chat_id]:
            self.media_group_timers.pop(chat_id, None)
            buffer = self.media_group_buffers.pop(chat_id, [])
            self.media_group_ids.pop(chat_id, None)
            if not buffer:
                return
            logger.info(
                f"NoBatchMessageBatcher: Flushing media group of {len(buffer)} "
                f"message(s) for chat {chat_id}."
            )
        await self.batch_processor.process_batch(chat_id, buffer, context)
