import asyncio
import base64
import contextlib
import html
import io
import json
import logging
import traceback
import uuid
import functools  # Ensure functools is imported
from collections import defaultdict
from datetime import datetime, timedelta, timezone

import telegramify_markdown
from telegram import Update
from telegram.constants import ChatAction, ParseMode
from telegram import (
    ForceReply,  # Add ForceReply import
    InlineKeyboardButton,
    Update,
    Message,
)
from telegram.constants import ChatAction, ParseMode
from telegram.ext import (
    Application,
    ApplicationBuilder,
    CallbackQueryHandler,  # Add CallbackQueryHandler
    CallbackContext,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

# Import necessary types for type hinting
from telegram import InlineKeyboardMarkup  # Move this import here
from sqlalchemy import update # For error handling db update
from typing import Any, Callable, Coroutine, Dict, List, Optional, Tuple, cast # Added cast

# Import necessary types for type hinting
from family_assistant.processing import ProcessingService
from family_assistant.storage.context import DatabaseContext

# from .storage.context import get_db_context # get_db_context is passed as a function

# Assuming these are passed or accessible

logger = logging.getLogger(__name__)


# Import telegram errors for specific checking
from telegram.error import Conflict


class TelegramUpdateHandler:  # Renamed from TelegramBotHandler
    """Handles specific Telegram updates (messages, commands) and delegates processing."""

    def __init__(
        self,
        telegram_service: "TelegramService",  # Accept the service instance
        application: Application,
        allowed_user_ids: List[int],
        developer_chat_id: Optional[int],
        processing_service: "ProcessingService",  # Use string quote for forward reference
        get_db_context_func: Callable[
            ..., contextlib.AbstractAsyncContextManager["DatabaseContext"]
        ],
    ):
        """
        Initializes the TelegramUpdateHandler. # Updated docstring

        Args:
            application: The telegram.ext.Application instance.
            allowed_user_ids: List of chat IDs allowed to interact with the bot.
            developer_chat_id: Optional chat ID for sending error notifications.
            processing_service: The ProcessingService instance.
            get_db_context_func: Async context manager function to get a DatabaseContext.
        """
        self.telegram_service = telegram_service  # Store the service instance
        # Imports moved to top level

        self.application = application
        self.allowed_user_ids = allowed_user_ids
        self.developer_chat_id = developer_chat_id
        self.processing_service = processing_service  # Store the service instance
        self.get_db_context = get_db_context_func

        # Internal state for message batching
        self.chat_locks: Dict[int, asyncio.Lock] = defaultdict(asyncio.Lock)
        self.message_buffers: Dict[int, List[Tuple[Update, Optional[bytes]]]] = (
            defaultdict(list)
        )
        self.processing_tasks: Dict[int, asyncio.Task] = {}
        # Store pending confirmation Futures
        self.pending_confirmations: Dict[str, asyncio.Future] = {}
        self.confirmation_timeout = (
            3600.0  # Default 1 hour, should match ConfirmingToolsProvider
        )

        # Store storage functions needed directly by the handler (e.g., history)
        # These might be better accessed via the db_context passed around,
        # but keeping add_message_to_history accessible for now.
        # Import storage here if needed, or rely on db_context methods.
        from family_assistant import storage  # Import storage locally if needed

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
        user_id = update.effective_user.id
        if self.allowed_user_ids and user_id not in self.allowed_user_ids:
            logger.warning(f"Unauthorized /start command from chat_id {user_id}")
            await update.message.reply_text(
                f"You're not authorized to use this bot. Give your user ID `{user_id}` to the person who runs this bot."
            )
            return

        # Use MarkdownV2 for formatting the list
        welcome_message = (
            "Hello\\! I'm your family assistant\\. Here's a quick look at what I can do:\n\n"
            "• Answer questions about upcoming calendar events\n"
            "• Add, modify, or delete calendar events\n"
            "• Remember information you give me \\(add notes\\)\n"
            "• Answer questions based on saved notes\n"
            "• Search notes, emails, or documents \\(if configured\\)\n"
            "• Summarize web pages \\(provide the full URL\\)\n"
            "• Perform web searches\n"
            "• Understand photos you send with questions\n"
            "• Schedule follow\\-up reminders in this chat\n"
            "• Control Home Assistant devices \\(if configured\\)\n\n"
            "How can I help you today?"
        )
        await update.message.reply_text(
            welcome_message, parse_mode=ParseMode.MARKDOWN_V2
        )

    async def process_chat_queue(
        self, chat_id: int, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        """Processes the message buffer for a given chat."""
        logger.debug(f"Starting process_chat_queue for chat_id {chat_id}")
        async with self.chat_locks[chat_id]:
            buffered_batch = self.message_buffers[chat_id][:]
            self.message_buffers[chat_id].clear()
            logger.debug(
                f"Cleared buffer for chat {chat_id}, processing {len(buffered_batch)} items."
            )

        if not buffered_batch:
            logger.info(
                f"Processing queue for chat {chat_id} called with empty buffer. Exiting."
            )
            return

        logger.info(
            f"Processing batch of {len(buffered_batch)} message update(s) for chat {chat_id}."
        )

        last_update, _ = buffered_batch[-1]
        user = last_update.effective_user
        user_name = user.first_name if user else "Unknown User"
        reply_target_message_id = (
            last_update.message.message_id if last_update.message else None
        )
        logger.debug(
            f"Extracted user='{user_name}', reply_target_id={reply_target_message_id} from last update."
        )
        # Check if the last message is a reply
        is_reply = bool(
            last_update.message and last_update.message.reply_to_message
        )
        replied_to_interface_id = str(last_update.message.reply_to_message.message_id) if is_reply else None
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
                    logger.debug(
                        f"Found first photo in batch from message {update_item.message.message_id}"
                    )
                if update_item.message.forward_origin:
                    origin = update_item.message.forward_origin
                    original_sender_name = "Unknown Sender"
                    if origin.sender_user:
                        original_sender_name = origin.sender_user.first_name or "User"
                    elif origin.sender_chat:
                        original_sender_name = (
                            origin.sender_chat.title or "Chat/Channel"
                        )
                    forward_context = f"(forwarded from {original_sender_name}) "
                    logger.debug(
                        f"Detected forward context from {original_sender_name} in last message."
                    )

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
                logger.error(
                    f"Error encoding photo from batch: {img_err}", exc_info=True
                )
                await context.bot.send_message(
                    chat_id, "Error processing image in batch."
                )
                trigger_content_parts = [text_content_part] # Revert to text only

        tool_call_info: Optional[List[Dict[str, Any]]] = None
        reasoning_info: Optional[Dict[str, Any]] = None  # Added
        sent_assistant_message: Optional[Message] = (
            None  # To store the sent message object
        )
        processing_error_traceback: Optional[str] = None  # Added
        logger.debug(f"Proceeding with trigger content and user '{user_name}'.")

        # Define interface type and conversation ID
        interface_type = "telegram"
        conversation_id = str(chat_id)

        try:
            # Retrieve the ProcessingService instance - it should be passed during init or accessible
            # Assuming it's stored in bot_data by main.py
            processing_service = context.bot_data.get("processing_service")
            if not processing_service:
                logger.error(
                    "ProcessingService not found in bot_data. Cannot generate response."
                )
                await context.bot.send_message(
                    chat_id, "Internal error: Processing service unavailable."
                )
                return

            db_context_getter = self.get_db_context()  # Get the coroutine first
            async with (
                await db_context_getter as db_context
            ):
                # --- Determine Thread Root ID ---
                thread_root_id_for_turn: Optional[int] = None
                if replied_to_interface_id:
                    try:
                        replied_to_db_msg = await self.storage.get_message_by_interface_id(
                            db_context=db_context,
                            interface_type=interface_type,
                            conversation_id=conversation_id,
                            interface_message_id=replied_to_interface_id,
                        )
                        if replied_to_db_msg:
                            # If the replied-to message has a root ID, use it. Otherwise, use its own internal ID.
                            thread_root_id_for_turn = replied_to_db_msg.get("thread_root_id") or replied_to_db_msg.get("internal_id")
                            logger.info(f"Determined thread_root_id {thread_root_id_for_turn} from replied-to message {replied_to_interface_id}")
                        else:
                             logger.warning(f"Could not find replied-to message {replied_to_interface_id} in DB to determine thread root.")
                    except Exception as thread_err:
                        logger.error(f"Error determining thread root ID: {thread_err}", exc_info=True)
                        # Continue without thread_root_id if error occurs

                # --- Save User Trigger Message(s) ---
                # Combine text again for saving user message content
                history_user_content = combined_text.strip()
                if first_photo_bytes:
                    history_user_content += " [Image(s) Attached]"

                # Use the last user update for ID and timestamp
                user_message_id = last_update.message.message_id if last_update.message else None
                user_message_timestamp = last_update.message.date if last_update.message else datetime.now(timezone.utc)

                if user_message_id:
                    await self.storage.add_message_to_history(
                        db_context=db_context,
                        interface_type=interface_type,
                        conversation_id=conversation_id,
                        interface_message_id=str(user_message_id), # User message has interface ID
                        turn_id=None, # User message is the trigger, has no turn_id
                        thread_root_id=thread_root_id_for_turn, # Use determined root ID
                        timestamp=user_message_timestamp,
                        role="user",
                        content=history_user_content,
                        tool_calls=None,
                        reasoning_info=None,
                        error_traceback=None, # Error hasn't happened yet
                        tool_call_id=None,
                    )
                else:
                    logger.warning(f"Could not get user message ID for chat {chat_id} to save to history.")

                async with self._typing_notifications(context, chat_id):
                    # Create the partial function for the confirmation callback
                    confirmation_callback_partial = functools.partial(
                        self._request_confirmation_impl,
                        chat_id=chat_id,
                    )
                    (
                        generated_turn_messages, # New return type
                        final_reasoning_info, # Capture final reasoning info

                        processing_error_traceback, # Capture traceback directly
                    ) = await self.processing_service.generate_llm_response_for_chat( # Call updated method
                        db_context=db_context, # Pass db context
                        application=self.application,
                        interface_type=interface_type, # Pass identifiers
                        conversation_id=conversation_id, # Pass identifiers
                        trigger_content_parts=trigger_content_parts,
                        user_name=user_name,
                        replied_to_interface_id=replied_to_interface_id, # Pass replied_to ID
                        # Pass the partially applied confirmation request callback
                        request_confirmation_callback=confirmation_callback_partial,
                    )
                    # Add turn_id to all generated messages before saving
                    # Get the turn_id from the first message (they all share it)
                    turn_id_for_saving = None
                    if generated_turn_messages:
                        turn_id_for_saving = generated_turn_messages[0].get("turn_id")
                    if not turn_id_for_saving:
                        logger.error(f"Could not extract turn_id from generated messages for chat {chat_id}. Assigning new UUID.")
                        turn_id_for_saving = str(uuid.uuid4()) # Fallback, should not happen

                    # Save all generated messages (assistant requests, tool responses, final answer)
                    last_assistant_internal_id : Optional[int] = None # Track for updating interface_id
                    for msg_dict in generated_turn_messages:
                        # Ensure correct IDs are set before saving
                        msg_dict["turn_id"] = turn_id_for_saving # Ensure turn_id is set
                        msg_dict["thread_root_id"] = thread_root_id_for_turn # Ensure thread_root is set
                        msg_dict["interface_type"] = interface_type # Add identifiers
                        msg_dict["conversation_id"] = conversation_id # Add identifiers
                        saved_msg = await self.storage.add_message_to_history(
                            db_context=db_context,
                            **msg_dict # Pass the dict directly
                        )
                        if msg_dict.get("role") == "assistant":
                            last_assistant_internal_id = saved_msg.internal_id # Assuming add_message returns the object or ID

            # Create ForceReply object
            force_reply_markup = ForceReply(selective=False)
            # Find the final message content to send to the user from the generated list
            final_llm_content_to_send : Optional[str] = None
            if generated_turn_messages:
                # Look for the last message with content, prioritizing assistant role
                for msg in reversed(generated_turn_messages):
                    if msg.get("content") and msg.get("role") == "assistant":
                        final_llm_content_to_send = msg["content"]
                        break
                # If no assistant content, take the last content regardless of role (e.g., tool error)
                if not final_llm_content_to_send and generated_turn_messages[-1].get("content"):
                     final_llm_content_to_send = generated_turn_messages[-1]["content"]

            if final_llm_content_to_send: # Check if there's content to send
                try:
                    # Format the final content for Telegram
                    converted_markdown = telegramify_markdown.markdownify(final_llm_content_to_send)
                    sent_assistant_message = await context.bot.send_message(
                        chat_id=chat_id,
                        text=converted_markdown,
                        parse_mode=ParseMode.MARKDOWN_V2,
                        reply_to_message_id=reply_target_message_id,
                        reply_markup=force_reply_markup,  # Add ForceReply
                    )
                except Exception as md_err:
                    logger.error(
                        f"Failed to convert markdown: {md_err}. Sending plain text.",
                        exc_info=True,
                    )
                    sent_assistant_message = await context.bot.send_message(
                        chat_id=chat_id,
                        text=final_llm_content_to_send,
                        reply_to_message_id=reply_target_message_id,
                        reply_markup=force_reply_markup,  # Add ForceReply
                    )
                # --- Update Interface ID for the Sent Message ---
                # Ensure this runs only if the message was actually sent
                if sent_assistant_message:
                    if last_assistant_internal_id:
                        await self.storage.update_message_interface_id(
                            db_context=db_context,
                            internal_id=last_assistant_internal_id,
                            interface_message_id=str(sent_assistant_message.message_id)
                        )
                        logger.info(f"Updated interface_message_id for internal_id {last_assistant_internal_id}")
                    else:
                        logger.warning(f"Sent assistant message {sent_assistant_message.message_id} but couldn't find its internal_id to update.")
            # If an error occurred during processing, check for traceback *before* handling empty response
            elif processing_error_traceback and reply_target_message_id:
                logger.info(
                    f"Sending error message to chat {chat_id} due to processing error:\n{processing_error_traceback}"
                )
                await context.bot.send_message(
                    chat_id=chat_id,
                    text="Sorry, something went wrong while processing your request.",
                    reply_to_message_id=reply_target_message_id,
                    reply_markup=force_reply_markup,  # Add ForceReply
                )
            # Only handle empty response if there was no content AND no processing error
            else:
                logger.warning( # This case might be less common now as we save all messages
                    "Received empty response from LLM (and no processing error detected)."
                )
                if reply_target_message_id:
                    sent_assistant_message = await context.bot.send_message(
                        chat_id=chat_id,
                        text="Sorry, I couldn't process that request.",  # Generic message for empty response
                        reply_to_message_id=reply_target_message_id,
                        reply_markup=force_reply_markup,  # Add ForceReply
                    )

        except Exception as e:
            # This catches errors *outside* the generate_llm_response_for_chat call
            # (e.g., DB connection issues before the call, Telegram API errors sending reply)
            logger.exception( # Use exception to log traceback automatically
                f"Unhandled error in process_chat_queue for chat {chat_id}: {e}",
                exc_info=True,  # noqa: F821
            )
            # Capture traceback if not already captured by generate_llm_response_for_chat
            if not processing_error_traceback:
                import traceback

                processing_error_traceback = traceback.format_exc()
            # --- Attempt to notify user if possible ---
            if reply_target_message_id:
                with contextlib.suppress(
                    Exception
                ):  # Suppress errors sending the error message
                    sent_assistant_message = await context.bot.send_message(
                        chat_id=chat_id,
                        text="Sorry, an unexpected error occurred.",
                        reply_to_message_id=reply_target_message_id,
                    )

            # --- Save Error Traceback with User Message ---
            # If an error happened, try to update the user message record
            # This assumes the user message was saved before the error occurred
            if processing_error_traceback and user_message_id:
                try:
                    db_context_getter_err = self.get_db_context()
                    # Get a new context for this update attempt
                    async with await db_context_getter_err as db_ctx_err:
                         # Fetch the user message's internal ID first (can't update by interface ID)
                         user_msg_record = await self.storage.get_message_by_interface_id(
                             db_ctx_err, interface_type, conversation_id, str(user_message_id)
                         )
                         if user_msg_record and user_msg_record.get("internal_id"):
                             stmt = (
                                 # Use SQLAlchemy update directly
                                 update(self.storage.message_history_table)
                                 .where(self.storage.message_history_table.c.internal_id == user_msg_record["internal_id"])
                                 .values(error_traceback=processing_error_traceback)
                             )
                             await db_ctx_err.execute_with_retry(stmt)
                             logger.info(f"Saved error traceback to user message internal_id {user_msg_record['internal_id']}")
                         else:
                             logger.error("Could not find user message record to attach error traceback.")
                except Exception as db_err_save:
                    logger.error(f"Failed to save error traceback to DB for chat {chat_id}: {db_err_save}", exc_info=True)

            # Let the main error handler notify the developer
            raise e  # Re-raise for the main error handler

        finally:
            # History saving moved into the main try block
            async with self.chat_locks[chat_id]:
                if self.processing_tasks.get(chat_id) is asyncio.current_task():
                    self.processing_tasks.pop(chat_id, None)
                    logger.info(
                        f"Processing task for chat {chat_id} finished and removed."
                    )
                else:
                    logger.warning(
                        f"Current task for chat {chat_id} doesn't match entry in processing_tasks during cleanup."
                    )

    async def message_handler(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        """Buffers incoming messages and triggers processing if not already running."""
        user_id = update.effective_user.id
        chat_id = update.effective_chat.id
        photo_bytes = None

        if self.allowed_user_ids and user_id not in self.allowed_user_ids:
            logger.warning(f"Ignoring message from unauthorized user {user_id}")
            return

        if update.message.photo:
            logger.info(
                f"Message {update.message.message_id} from chat {chat_id} contains photo."
            )
            try:
                photo_size = update.message.photo[-1]
                photo_file = await photo_size.get_file()
                with io.BytesIO() as buf:
                    await photo_file.download_to_memory(out=buf)
                    buf.seek(0)
                    photo_bytes = buf.read()
                logger.debug(
                    f"Photo from message {update.message.message_id} loaded into bytes."
                )
            except Exception as img_err:
                logger.error(
                    f"Failed to process photo bytes for message {update.message.message_id}: {img_err}",
                    exc_info=True,
                )
                await update.message.reply_text(
                    "Sorry, error processing attached image."
                )
                return

        async with self.chat_locks[chat_id]:
            self.message_buffers[chat_id].append((update, photo_bytes))
            buffer_size = len(self.message_buffers[chat_id])
            logger.info(
                f"Buffered update {update.update_id} (message {update.message.message_id if update.message else 'N/A'}) for chat {chat_id}. Buffer size: {buffer_size}"
            )

            if (
                chat_id not in self.processing_tasks
                or self.processing_tasks[chat_id].done()
            ):
                logger.info(f"Starting new processing task for chat {chat_id}.")
                task = asyncio.create_task(self.process_chat_queue(chat_id, context))
                self.processing_tasks[chat_id] = task
                # Add callback to remove task from dict upon completion/error
                # Use lambda to capture chat_id correctly
                task.add_done_callback(
                    # Ensure the callback function exists
                    lambda t, c=chat_id: (
                        self._remove_task_callback(t, c)
                        if hasattr(self, "_remove_task_callback")
                        else None
                    )
                )
            else:
                logger.info(
                    f"Processing task already running for chat {chat_id}. Message added to buffer."
                )

    def _remove_task_callback(self, task: asyncio.Task, chat_id: int):
        """Callback function to remove task from processing_tasks dict."""
        # Check for exceptions in the completed task
        try:
            task.result()  # Raise exception if task failed
        except asyncio.CancelledError:
            logger.info(f"Processing task for chat {chat_id} was cancelled.")
        except Exception:
            # Error already logged by process_chat_queue or error_handler
            # We just need to ensure the task is removed from the dict
            logger.debug(
                f"Processing task for chat {chat_id} completed with an exception (handled elsewhere)."
            )
            pass  # Error should have been logged by the task itself or the error handler

        # Remove the task using the lock to prevent race conditions
        # Need to run this removal in the event loop if called from a different thread context,
        # but add_done_callback usually runs in the same loop.
        # Using a lock here might be overkill/problematic if the callback isn't async.
        # Let's simplify: just pop it. The lock in message_handler prevents starting a new one while popping.
        # Ensure the dict exists before popping
        if hasattr(self, "processing_tasks"):
            self.processing_tasks.pop(chat_id, None)
            logger.debug(f"Task entry removed for chat {chat_id} via callback.")
        else:
            logger.warning(
                f"Cannot remove task entry for chat {chat_id}: processing_tasks dict not found."
            )

    # --- Confirmation Handling Logic ---

    async def _request_confirmation_impl(
        self, # Make it instance method if not already
        chat_id: int,
        prompt_text: str,
        tool_name: str,
        tool_args: Dict[str, Any],
        timeout: float,
    ) -> bool:
        """Internal implementation to send confirmation and wait."""
        confirm_uuid = str(uuid.uuid4())
        # Need the Telegram context - assume it's accessible via self.application?
        # This might require passing the specific `context` object from the handler
        # Or finding a way to get a context for sending messages.
        # Let's assume self.application.bot can send messages.
        if not self.application or not self.application.bot:
             raise RuntimeError("Telegram application or bot instance not available for sending confirmation.")
        logger.info(
            f"Requesting confirmation (UUID: {confirm_uuid}) for tool '{tool_name}' in chat {chat_id}"
        )

        keyboard = InlineKeyboardMarkup(
            [
                [
                    InlineKeyboardButton(
                        "✅ Confirm", callback_data=f"confirm:{confirm_uuid}:yes"
                    ),
                    InlineKeyboardButton(
                        "❌ Cancel", callback_data=f"confirm:{confirm_uuid}:no"
                    ),
                ]
            ]
        )

        try:
            # Send the confirmation message
            sent_message = await self.application.bot.send_message( # Use self.application.bot
                chat_id=chat_id,
                text=prompt_text,  # Assumes already MarkdownV2 formatted/escaped
                parse_mode=ParseMode.MARKDOWN_V2,
                reply_markup=keyboard,
            )
            logger.debug(
                f"Confirmation message sent (Message ID: {sent_message.message_id})"
            )
        except Exception as send_err:
            logger.error(
                f"Failed to send confirmation message to chat {chat_id}: {send_err}",
                exc_info=True,
            )
            # Cannot proceed without sending the message
            raise RuntimeError(
                f"Failed to send confirmation message: {send_err}"
            ) from send_err

        # Create and store the Future
        confirmation_future = asyncio.get_running_loop().create_future()
        self.pending_confirmations[confirm_uuid] = confirmation_future

        try:
            # Wait for the future to be set by the callback handler, with timeout
            logger.debug(
                f"Waiting for confirmation response (UUID: {confirm_uuid}, Timeout: {timeout}s)"
            )
            user_confirmed = await asyncio.wait_for(
                confirmation_future, timeout=timeout
            )
            logger.info(
                f"Confirmation response received for {confirm_uuid}: {user_confirmed}"
            )
            return user_confirmed
        except asyncio.TimeoutError:
            logger.warning(f"Confirmation {confirm_uuid} timed out after {timeout}s.")
            # Clean up the original confirmation message (remove keyboard)
            try:
                await self.application.bot.edit_message_reply_markup( # Use self.application.bot
                    chat_id=chat_id,
                    message_id=sent_message.message_id,
                    reply_markup=None,  # Remove keyboard
                )
                await self.application.bot.edit_message_text( # Use self.application.bot
                    chat_id=chat_id,
                    message_id=sent_message.message_id,
                    text=prompt_text
                    + "\n\n\\(Confirmation timed out\\)",  # Append timeout message
                    parse_mode=ParseMode.MARKDOWN_V2,
                )
            except Exception as edit_err:
                logger.warning(
                    f"Failed to edit confirmation message {sent_message.message_id} on timeout: {edit_err}"
                )
            # Future is automatically cancelled by wait_for on timeout, but remove from dict
            self.pending_confirmations.pop(confirm_uuid, None)
            raise  # Re-raise TimeoutError for the caller (ConfirmingToolsProvider)
        finally:
            # Ensure future is removed from dict if it's still there (e.g., cancellation)
            self.pending_confirmations.pop(confirm_uuid, None)

    async def confirmation_callback_handler(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        """Handles button presses for tool confirmations."""
        query = update.callback_query
        await query.answer()  # Answer immediately to remove loading indicator

        callback_data = query.data
        logger.info(f"Received confirmation callback: {callback_data}")

        try:
            _, confirm_uuid, action = callback_data.split(":")
        except ValueError:
            logger.error(f"Invalid confirmation callback data format: {callback_data}")
            await query.edit_message_text(text="Error: Invalid callback data.")
            return

        # Find the pending future
        confirmation_future = self.pending_confirmations.pop(confirm_uuid, None)

        if confirmation_future and not confirmation_future.done():
            original_text = (
                query.message.text_markdown_v2
            )  # Get original text with markdown

            if action == "yes":
                logger.debug(f"Setting confirmation result for {confirm_uuid} to True")
                confirmation_future.set_result(True)
                status_text = "\n\n*Confirmed* ✅"
            elif action == "no":
                logger.debug(f"Setting confirmation result for {confirm_uuid} to False")
                confirmation_future.set_result(False)
                status_text = "\n\n*Cancelled* ❌"
            else:
                logger.warning(
                    f"Unknown action '{action}' in confirmation callback {confirm_uuid}"
                )
                # Don't set future result, edit message to show error?
                status_text = "\n\n*Error: Unknown action*"
                # Keep future in dict? Or cancel it? Let's cancel.
                confirmation_future.cancel()
            # The noqa was here previously, likely unnecessary if edit_message_text is defined
            # Edit the original message to remove keyboard and show status
            try:  # noqa: F821
                await query.edit_message_text(
                    text=original_text + status_text,
                    parse_mode=ParseMode.MARKDOWN_V2,
                    reply_markup=None,  # Remove keyboard
                )
            except Exception as edit_err:
                logger.error(
                    f"Failed to edit confirmation message {query.message.message_id} after callback: {edit_err}"
                )

        elif confirmation_future and confirmation_future.done():
            logger.warning(
                f"Confirmation callback {confirm_uuid} received, but future was already done (likely timed out or duplicate callback)."
            )
            # Optionally edit message to indicate it expired?
            await query.edit_message_text(
                text=query.message.text_markdown_v2
                + "\n\n\\(Request already handled or expired\\)",
                parse_mode=ParseMode.MARKDOWN_V2,
                reply_markup=None,
            )
        else:
            logger.warning(
                f"Confirmation callback {confirm_uuid} received, but no pending confirmation found."
            )
            await query.edit_message_text(
                text="This confirmation request is no longer valid or has expired.",
                reply_markup=None,
            )

    async def error_handler(self, update: object, context: CallbackContext) -> None:
        """Log the error, store it in the service, and notify the developer."""
        error = context.error
        logger.error(f"Exception while handling an update: {error}", exc_info=error)

        # Store the error in the TelegramService instance
        if self.telegram_service:
            self.telegram_service._last_error = error
            # Log if polling might stop due to this error
            if isinstance(error, Conflict):
                logger.critical(
                    f"Telegram Conflict error detected: {error}. Polling will likely stop."
                )
            elif isinstance(
                error, Exception
            ):  # Catch other potential fatal errors if needed
                # You might add checks for other specific errors that stop polling
                pass

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

        # Callback query handler for confirmations
        self.application.add_handler(
            CallbackQueryHandler(
                self.confirmation_callback_handler, pattern=r"^confirm:"
            )
        )

        # Error handler (add last)
        self.application.add_error_handler(self.error_handler)
        logger.info("Telegram handlers registered (including confirmation callback).")


class TelegramService:
    """Manages the Telegram bot application lifecycle and update handling."""

    def __init__(
        self,
        telegram_token: str,
        allowed_user_ids: List[int],
        developer_chat_id: Optional[int],
        processing_service: ProcessingService,
        get_db_context_func: Callable[
            ..., contextlib.AbstractAsyncContextManager[DatabaseContext]
        ],
    ):
        """
        Initializes the Telegram Service.

        Args:
            telegram_token: The Telegram Bot API token.
            allowed_user_ids: List of chat IDs allowed to interact with the bot.
            developer_chat_id: Optional chat ID for sending error notifications.
            processing_service: The ProcessingService instance.
            get_db_context_func: Async context manager function to get a DatabaseContext.
        """
        logger.info("Initializing TelegramService...")
        self.application = ApplicationBuilder().token(telegram_token).build()
        self._was_started: bool = False
        self._last_error: Optional[Exception] = None

        # Store the ProcessingService instance in bot_data for access in handlers
        # Note: This assumes handlers might still need direct access via context.bot_data
        # If handlers only use self.processing_service, this line might be removable.
        self.application.bot_data["processing_service"] = processing_service
        logger.info("Stored ProcessingService instance in application.bot_data.")

        # Instantiate the handler class, passing self (the service instance)
        self.update_handler = TelegramUpdateHandler(
            telegram_service=self,  # Pass self
            application=self.application,
            allowed_user_ids=allowed_user_ids,
            developer_chat_id=developer_chat_id,
            processing_service=processing_service,
            get_db_context_func=get_db_context_func,
            # Pass confirmation timeout if needed, or let handler use default
            # confirmation_timeout=...
        )

        # Register handlers using the handler instance
        self.update_handler.register_handlers()
        logger.info("TelegramService initialized.")

    # Add the confirmation request method to the service, delegating to the handler
    async def request_confirmation_from_user(
        self,
        chat_id: int,
        prompt_text: str,
        tool_name: str,
        tool_args: Dict[str, Any],
        timeout: float,
    ) -> bool:
        """Public method to request confirmation, called by ConfirmingToolsProvider."""
        if hasattr(self.update_handler, "_request_confirmation_impl"):
            return await self.update_handler._request_confirmation_impl(
                chat_id=chat_id,
                prompt_text=prompt_text,
                tool_name=tool_name,
                tool_args=tool_args,
                timeout=timeout,
            )
        else:
            logger.error(
                "TelegramUpdateHandler does not have the _request_confirmation_impl method."
            )
            raise RuntimeError(
                "Confirmation mechanism not properly initialized in handler."
            )

    async def start_polling(self):
        """Initializes the application and starts polling for updates."""
        logger.info("Starting Telegram polling...")
        await self.application.initialize()
        await self.application.start()
        # Use Update.ALL_TYPES to ensure all relevant updates are received
        await self.application.updater.start_polling(allowed_updates=Update.ALL_TYPES)
        self._was_started = True  # Mark as started
        self._last_error = None  # Clear last error on successful start
        logger.info("Telegram polling started successfully.")

    @property
    def last_error(self) -> Optional[Exception]:
        """Returns the last error encountered by the error handler."""
        return self._last_error

    async def stop_polling(self):
        """Stops the polling and shuts down the application gracefully."""
        self._was_started = False  # Mark as stopped (or stopping)
        if self.application and self.application.updater:
            logger.info("Stopping Telegram polling...")
            try:
                if self.application.updater.running:  # Check if polling before stopping
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
                logger.error(
                    f"Error shutting down Telegram application: {e}", exc_info=True
                )
        else:
            logger.info("Telegram application instance not found for shutdown.")
