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


class DefaultMessageBatcher(MessageBatcher):
    """Buffers messages and processes them in batches to avoid race conditions."""

    def __init__(
        self, batch_processor: BatchProcessor, batch_delay_seconds: float = 0.5
    ) -> None:
        self.batch_processor = batch_processor
        self.batch_delay_seconds = batch_delay_seconds
        self.chat_locks: dict[int, asyncio.Lock] = defaultdict(asyncio.Lock)
        self.message_buffers: dict[
            int, list[tuple[Update, list[AttachmentData] | None]]
        ] = defaultdict(list)
        self.processing_tasks: dict[int, asyncio.Task] = {}
        self.batch_timers: dict[
            int, asyncio.TimerHandle
        ] = {}  # Store timers for delayed processing

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
        async with self.chat_locks[chat_id]:
            self.message_buffers[chat_id].append((update, attachments))
            buffer_size = len(self.message_buffers[chat_id])
            logger.info(
                f"Buffered update {update.update_id} (message {update.message.message_id if update.message else 'N/A'}) for chat {chat_id}. Buffer size: {buffer_size}"
            )

            if chat_id in self.batch_timers:
                self.batch_timers[chat_id].cancel()
                logger.debug(f"Cancelled existing batch timer for chat {chat_id}.")

            loop = asyncio.get_running_loop()
            self.batch_timers[chat_id] = loop.call_later(
                self.batch_delay_seconds,
                lambda: asyncio.create_task(
                    self._trigger_batch_processing(chat_id, context)
                ),
            )
            logger.debug(
                f"Scheduled batch processing for chat {chat_id} in {self.batch_delay_seconds}s."
            )

    async def _trigger_batch_processing(
        self, chat_id: int, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        """Gets the current batch and triggers the BatchProcessor if no task is running."""
        async with self.chat_locks[chat_id]:
            if chat_id in self.batch_timers:
                self.batch_timers.pop(chat_id)

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
    """A simple batcher that processes each message immediately without buffering."""

    def __init__(self, batch_processor: BatchProcessor) -> None:
        self.batch_processor = batch_processor

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
        logger.info(
            f"NoBatchMessageBatcher: Immediately processing update {update.update_id} for chat {chat_id}"
        )
        batch = [(update, attachments)]
        await self.batch_processor.process_batch(chat_id, batch, context)
