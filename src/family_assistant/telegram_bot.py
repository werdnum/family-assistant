import asyncio
import base64
import contextlib
import html
import io
import json
import logging
import traceback
import uuid
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Coroutine, Dict, List, Optional, Tuple

import telegramify_markdown
from telegram import Update
from telegram.constants import ChatAction, ParseMode
from telegram.ext import (
    Application,
    ApplicationBuilder, # Add ApplicationBuilder
    CallbackContext,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

# Import necessary types for type hinting
from family_assistant.processing import ProcessingService
from family_assistant.storage.context import DatabaseContext
# from .storage.context import get_db_context # get_db_context is passed as a function

# Assuming these are passed or accessible

logger = logging.getLogger(__name__)


class TelegramUpdateHandler: # Renamed from TelegramBotHandler
    """Handles specific Telegram updates (messages, commands) and delegates processing."""

    def __init__(
        self,
        application: Application,
        allowed_chat_ids: List[int],
        developer_chat_id: Optional[int],
        processing_service: "ProcessingService", # Use string quote for forward reference
        get_db_context_func: Callable[..., contextlib.AbstractAsyncContextManager["DatabaseContext"]],
    ):
        """
        Initializes the TelegramUpdateHandler. # Updated docstring

        Args:
            application: The telegram.ext.Application instance.
            allowed_chat_ids: List of chat IDs allowed to interact with the bot.
            developer_chat_id: Optional chat ID for sending error notifications.
            processing_service: The ProcessingService instance.
            get_db_context_func: Async context manager function to get a DatabaseContext.
        """
        # Imports moved to top level

        self.application = application
        self.allowed_chat_ids = allowed_chat_ids
        self.developer_chat_id = developer_chat_id
        self.processing_service = processing_service # Store the service instance
        self.get_db_context = get_db_context_func

        # Internal state for message batching
        self.chat_locks: Dict[int, asyncio.Lock] = defaultdict(asyncio.Lock)
        self.message_buffers: Dict[int, List[Tuple[Update, Optional[bytes]]]] = defaultdict(list)
        self.processing_tasks: Dict[int, asyncio.Task] = {}

        # Store storage functions needed directly by the handler (e.g., history)
        # These might be better accessed via the db_context passed around,
        # but keeping add_message_to_history accessible for now.
        # Import storage here if needed, or rely on db_context methods.
        from family_assistant import storage # Import storage locally if needed
        self.storage = storage


    @contextlib.asynccontextmanager
    async def _typing_notifications(
        self,
        context: ContextTypes.DEFAULT_TYPE,
        chat_id: int,
        action: str = ChatAction.TYPING,
    ):
        """Context manager to send typing notifications periodically."""
        stop_event = asyncio.Event()

        async def typing_loop():
            while not stop_event.is_set():
                try:
                    await context.bot.send_chat_action(chat_id=chat_id, action=action)
                    await asyncio.wait_for(stop_event.wait(), timeout=4.5)
                except asyncio.TimeoutError:
                    pass
                except Exception as e:
                    logger.warning(f"Error sending chat action: {e}")
                    await asyncio.sleep(5)

        typing_task = asyncio.create_task(typing_loop())
        try:
            yield
        finally:
            stop_event.set()
            with contextlib.suppress(asyncio.CancelledError):
                await asyncio.wait_for(typing_task, timeout=1.0)

    async def start(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Sends a welcome message when the /start command is issued."""
        chat_id = update.effective_chat.id
        if self.allowed_chat_ids and chat_id not in self.allowed_chat_ids:
            logger.warning(f"Unauthorized /start command from chat_id {chat_id}")
            return
        await update.message.reply_text(
            f"Hello! I'm your family assistant. Your chat ID is `{chat_id}`. How can I help?"
        )

    async def process_chat_queue(self, chat_id: int, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Processes the message buffer for a given chat."""
        logger.debug(f"Starting process_chat_queue for chat_id {chat_id}")
        async with self.chat_locks[chat_id]:
            buffered_batch = self.message_buffers[chat_id][:]
            self.message_buffers[chat_id].clear()
            logger.debug(f"Cleared buffer for chat {chat_id}, processing {len(buffered_batch)} items.")

        if not buffered_batch:
            logger.info(f"Processing queue for chat {chat_id} called with empty buffer. Exiting.")
            return

        logger.info(f"Processing batch of {len(buffered_batch)} message update(s) for chat {chat_id}.")

        last_update, _ = buffered_batch[-1]
        user = last_update.effective_user
        user_name = user.first_name if user else "Unknown User"
        reply_target_message_id = last_update.message.message_id if last_update.message else None
        logger.debug(f"Extracted user='{user_name}', reply_target_id={reply_target_message_id} from last update.")

        all_texts = []
        first_photo_bytes = None
        forward_context = ""

        for update_item, photo_bytes in buffered_batch:
            if update_item.message:
                text = update_item.message.caption or update_item.message.text or ""
                if text:
                    all_texts.append(text)
                if photo_bytes and first_photo_bytes is None:
                    first_photo_bytes = photo_bytes
                    logger.debug(f"Found first photo in batch from message {update_item.message.message_id}")
                if update_item.message.forward_origin:
                    origin = update_item.message.forward_origin
                    original_sender_name = "Unknown Sender"
                    if origin.sender_user:
                        original_sender_name = origin.sender_user.first_name or "User"
                    elif origin.sender_chat:
                        original_sender_name = origin.sender_chat.title or "Chat/Channel"
                    forward_context = f"(forwarded from {original_sender_name}) "
                    logger.debug(f"Detected forward context from {original_sender_name} in last message.")

        combined_text = "\n\n".join(all_texts).strip()
        logger.debug(f"Combined text: '{combined_text[:100]}...'")

        formatted_user_text_content = f"{forward_context}{combined_text}".strip()
        text_content_part = {"type": "text", "text": formatted_user_text_content}
        trigger_content_parts = [text_content_part]

        if first_photo_bytes:
            try:
                base64_image = base64.b64encode(first_photo_bytes).decode("utf-8")
                mime_type = "image/jpeg"
                trigger_content_parts.append(
                    {
                        "type": "image_url",
                        "image_url": {"url": f"data:{mime_type};base64,{base64_image}"},
                    }
                )
                logger.info("Added first photo from batch to trigger content.")
            except Exception as img_err:
                logger.error(f"Error encoding photo from batch: {img_err}", exc_info=True)
                await context.bot.send_message(chat_id, "Error processing image in batch.")
                trigger_content_parts = [text_content_part]

        llm_response_content = None
        tool_call_info = None
        logger.debug(f"Proceeding with trigger content and user '{user_name}'.")

        try:
            # Retrieve the ProcessingService instance - it should be passed during init or accessible
            # Assuming it's stored in bot_data by main.py
            processing_service = context.bot_data.get("processing_service")
            if not processing_service:
                logger.error("ProcessingService not found in bot_data. Cannot generate response.")
                await context.bot.send_message(chat_id, "Internal error: Processing service unavailable.")
                return

            async with self.get_db_context() as db_context:
                async with self._typing_notifications(context, chat_id):
                    # Call the method on the ProcessingService instance
                    llm_response_content, tool_call_info = await self.processing_service.generate_llm_response_for_chat(
                        db_context=db_context,
                        # processing_service is now self.processing_service, no need to pass
                        application=self.application,
                        chat_id=chat_id,
                        trigger_content_parts=trigger_content_parts,
                        user_name=user_name,
                    )

            if llm_response_content:
                try:
                    converted_markdown = telegramify_markdown.markdownify(llm_response_content)
                    await context.bot.send_message(
                        chat_id=chat_id,
                        text=converted_markdown,
                        parse_mode=ParseMode.MARKDOWN_V2,
                        reply_to_message_id=reply_target_message_id,
                    )
                except Exception as md_err:
                    logger.error(f"Failed to convert markdown: {md_err}. Sending plain text.", exc_info=True)
                    await context.bot.send_message(
                        chat_id=chat_id,
                        text=llm_response_content,
                        reply_to_message_id=reply_target_message_id,
                    )
            else:
                logger.warning("Received empty response from LLM.")
                if reply_target_message_id:
                    await context.bot.send_message(
                        chat_id=chat_id,
                        text="Sorry, I couldn't process that request.",
                        reply_to_message_id=reply_target_message_id,
                    )

        except Exception as e:
            logger.error(f"Error processing message batch for chat {chat_id}: {e}", exc_info=True)
            if reply_target_message_id:
                try:
                    await context.bot.send_message(
                        chat_id=chat_id,
                        text="Sorry, something went wrong while processing your request.",
                        reply_to_message_id=reply_target_message_id,
                    )
                except Exception as reply_err:
                    logger.error(f"Failed to send error reply to chat {chat_id}: {reply_err}")
                    try:
                        await context.bot.send_message(
                            chat_id=chat_id,
                            text="Sorry, something went wrong while processing your request (reply failed).",
                        )
                    except Exception as fallback_err:
                        logger.error(f"Failed to send fallback error message to chat {chat_id}: {fallback_err}")
            # Let the main error handler notify the developer
            # Re-raise the exception so the main error handler catches it
            raise e

        finally:
            try:
                async with self.get_db_context() as db_context_for_history:
                    if reply_target_message_id:
                        history_user_content = combined_text
                        if first_photo_bytes:
                            history_user_content += " [Image(s) Attached]"
                        await self.storage.add_message_to_history(
                            db_context=db_context_for_history,
                            chat_id=chat_id,
                            message_id=reply_target_message_id,
                            timestamp=datetime.now(timezone.utc),
                            role="user",
                            content=history_user_content,
                            tool_calls_info=None,
                        )
                    else:
                        logger.warning(f"Could not store batched user message for chat {chat_id} due to missing message ID.")

                    if llm_response_content and reply_target_message_id:
                        bot_message_pseudo_id = reply_target_message_id + 1
                        await self.storage.add_message_to_history(
                            db_context=db_context_for_history,
                            chat_id=chat_id,
                            message_id=bot_message_pseudo_id,
                            timestamp=datetime.now(timezone.utc),
                            role="assistant",
                            content=llm_response_content,
                            tool_calls_info=tool_call_info,
                        )
            except Exception as db_err:
                logger.error(f"Failed to store batched message history in DB for chat {chat_id}: {db_err}", exc_info=True)

            async with self.chat_locks[chat_id]:
                if self.processing_tasks.get(chat_id) is asyncio.current_task():
                    self.processing_tasks.pop(chat_id, None)
                    logger.info(f"Processing task for chat {chat_id} finished and removed.")
                else:
                    logger.warning(f"Current task for chat {chat_id} doesn't match entry in processing_tasks during cleanup.")


    async def message_handler(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Buffers incoming messages and triggers processing if not already running."""
        chat_id = update.effective_chat.id
        photo_bytes = None

        if self.allowed_chat_ids and chat_id not in self.allowed_chat_ids:
            logger.warning(f"Ignoring message from unauthorized chat_id {chat_id}")
            return

        if update.message.photo:
            logger.info(f"Message {update.message.message_id} from chat {chat_id} contains photo.")
            try:
                photo_size = update.message.photo[-1]
                photo_file = await photo_size.get_file()
                with io.BytesIO() as buf:
                    await photo_file.download_to_memory(out=buf)
                    buf.seek(0)
                    photo_bytes = buf.read()
                logger.debug(f"Photo from message {update.message.message_id} loaded into bytes.")
            except Exception as img_err:
                logger.error(f"Failed to process photo bytes for message {update.message.message_id}: {img_err}", exc_info=True)
                await update.message.reply_text("Sorry, error processing attached image.")
                return

        async with self.chat_locks[chat_id]:
            self.message_buffers[chat_id].append((update, photo_bytes))
            buffer_size = len(self.message_buffers[chat_id])
            logger.info(f"Buffered update {update.update_id} (message {update.message.message_id if update.message else 'N/A'}) for chat {chat_id}. Buffer size: {buffer_size}")

            if chat_id not in self.processing_tasks or self.processing_tasks[chat_id].done():
                logger.info(f"Starting new processing task for chat {chat_id}.")
                task = asyncio.create_task(self.process_chat_queue(chat_id, context))
                self.processing_tasks[chat_id] = task
                # Add callback to remove task from dict upon completion/error
                # Use lambda to capture chat_id correctly
                task.add_done_callback(lambda t, c=chat_id: self._remove_task_callback(t, c))
            else:
                logger.info(f"Processing task already running for chat {chat_id}. Message added to buffer.")

    def _remove_task_callback(self, task: asyncio.Task, chat_id: int):
        """Callback function to remove task from processing_tasks dict."""
        # Check for exceptions in the completed task
        try:
            task.result() # Raise exception if task failed
        except asyncio.CancelledError:
            logger.info(f"Processing task for chat {chat_id} was cancelled.")
        except Exception:
            # Error already logged by process_chat_queue or error_handler
            # We just need to ensure the task is removed from the dict
            logger.debug(f"Processing task for chat {chat_id} completed with an exception (handled elsewhere).")
            pass # Error should have been logged by the task itself or the error handler

        # Remove the task using the lock to prevent race conditions
        # Need to run this removal in the event loop if called from a different thread context,
        # but add_done_callback usually runs in the same loop.
        # Using a lock here might be overkill/problematic if the callback isn't async.
        # Let's simplify: just pop it. The lock in message_handler prevents starting a new one while popping.
        self.processing_tasks.pop(chat_id, None)
        logger.debug(f"Task entry removed for chat {chat_id} via callback.")


    async def error_handler(self, update: object, context: CallbackContext) -> None:
        """Log the error and send a telegram message to notify the developer."""
        # Use the error stored in context
        error = context.error
        logger.error(f"Exception while handling an update: {error}", exc_info=error)

        tb_list = traceback.format_exception(None, error, error.__traceback__)
        tb_string = "".join(tb_list)

        update_str = update.to_dict() if isinstance(update, Update) else str(update)
        message = (
            "An exception was raised while handling an update\n"
            f"<pre>update = {html.escape(json.dumps(update_str, indent=2, ensure_ascii=False))}</pre>\n\n"
            f"<pre>context.chat_data = {html.escape(str(context.chat_data))}</pre>\n\n"
            f"<pre>context.user_data = {html.escape(str(context.user_data))}</pre>\n\n"
            f"<pre>{html.escape(tb_string)}</pre>"
        )

        if self.developer_chat_id:
            max_len = 4096
            for i in range(0, len(message), max_len):
                try:
                    await context.bot.send_message(
                        chat_id=self.developer_chat_id,
                        text=message[i : i + max_len],
                        parse_mode=ParseMode.HTML,
                    )
                except Exception as e:
                    logger.error(f"Failed to send error message to developer: {e}")
        else:
            logger.warning("DEVELOPER_CHAT_ID not set, cannot send error notification.")

    def register_handlers(self):
        """Registers the necessary Telegram handlers with the application."""
        # Command handlers
        self.application.add_handler(CommandHandler("start", self.start))

        # Message handlers (text or photo with optional caption, excluding commands)
        self.application.add_handler(
            MessageHandler(
                (filters.TEXT | filters.PHOTO) & ~filters.COMMAND, self.message_handler
            )
        )

        # Error handler
        self.application.add_error_handler(self.error_handler)
        logger.info("Telegram handlers registered.")


class TelegramService:
    """Manages the Telegram bot application lifecycle and update handling."""

    def __init__(
        self,
        telegram_token: str,
        allowed_chat_ids: List[int],
        developer_chat_id: Optional[int],
        processing_service: ProcessingService,
        get_db_context_func: Callable[..., contextlib.AbstractAsyncContextManager[DatabaseContext]],
    ):
        """
        Initializes the Telegram Service.

        Args:
            telegram_token: The Telegram Bot API token.
            allowed_chat_ids: List of chat IDs allowed to interact with the bot.
            developer_chat_id: Optional chat ID for sending error notifications.
            processing_service: The ProcessingService instance.
            get_db_context_func: Async context manager function to get a DatabaseContext.
        """
        logger.info("Initializing TelegramService...")
        self.application = (
            ApplicationBuilder().token(telegram_token).build()
        )

        # Store the ProcessingService instance in bot_data for access in handlers
        # Note: This assumes handlers might still need direct access via context.bot_data
        # If handlers only use self.processing_service, this line might be removable.
        self.application.bot_data["processing_service"] = processing_service
        logger.info("Stored ProcessingService instance in application.bot_data.")

        # Instantiate the handler class
        self.update_handler = TelegramUpdateHandler( # Use renamed class
            application=self.application,
            allowed_chat_ids=allowed_chat_ids,
            developer_chat_id=developer_chat_id,
            processing_service=processing_service,
            get_db_context_func=get_db_context_func,
        )

        # Register handlers using the handler instance
        self.update_handler.register_handlers()
        logger.info("TelegramService initialized.")

    async def start_polling(self):
        """Initializes the application and starts polling for updates."""
        logger.info("Starting Telegram polling...")
        await self.application.initialize()
        await self.application.start()
        # Use Update.ALL_TYPES to ensure all relevant updates are received
        await self.application.updater.start_polling(allowed_updates=Update.ALL_TYPES)
        logger.info("Telegram polling started successfully.")

    async def stop_polling(self):
        """Stops the polling and shuts down the application gracefully."""
        if self.application and self.application.updater:
            logger.info("Stopping Telegram polling...")
            try:
                if self.application.updater.running: # Check if polling before stopping
                    await self.application.updater.stop()
                    logger.info("Telegram polling stopped.")
                else:
                    logger.info("Telegram polling was not running.")
            except Exception as e:
                logger.error(f"Error stopping Telegram updater: {e}", exc_info=True)

        if self.application:
            logger.info("Shutting down Telegram application...")
            try:
                await self.application.shutdown()
                logger.info("Telegram application shut down.")
            except Exception as e:
                logger.error(f"Error shutting down Telegram application: {e}", exc_info=True)
        else:
            logger.info("Telegram application instance not found for shutdown.")
