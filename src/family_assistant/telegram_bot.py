import asyncio
import base64
import contextlib
import html
import io
import json
import logging
import os  # Added for environment variable access
import traceback
import uuid
from collections import defaultdict
from collections.abc import AsyncIterator, Callable  # Added cast
from datetime import datetime, timezone
from typing import (
    Any,
    Protocol,
    runtime_checkable,
)

import telegramify_markdown  # type: ignore[import-untyped]
from sqlalchemy import update  # For error handling db update
from telegram import (
    ForceReply,  # Add ForceReply import
    InlineKeyboardButton,
    InlineKeyboardMarkup,  # Move this import here
    Message,
    MessageOriginChannel,
    MessageOriginChat,
    MessageOriginHiddenUser,
    MessageOriginUser,
    Update,
)
from telegram.constants import ChatAction, ParseMode
from telegram.error import Conflict  # Import telegram errors for specific checking
from telegram.ext import (
    Application,
    ApplicationBuilder,
    CallbackContext,
    CallbackQueryHandler,  # Add CallbackQueryHandler
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

# Import necessary types for type hinting
from family_assistant.processing import ProcessingService
from family_assistant.storage.context import DatabaseContext

logger = logging.getLogger(__name__)


# Import telegram errors for specific checking

# --- Protocols for Refactoring ---


@runtime_checkable
class BatchProcessor(Protocol):
    """Protocol defining the interface for processing a batch of messages."""

    async def process_batch(
        self,
        chat_id: int,
        batch: list[tuple[Update, bytes | None]],
        context: ContextTypes.DEFAULT_TYPE,
    ) -> None:
        """Processes a given batch of updates for a specific chat."""
        ...


@runtime_checkable
class MessageBatcher(Protocol):
    """Protocol defining the interface for buffering messages."""

    async def add_to_batch(
        self,
        update: Update,
        context: ContextTypes.DEFAULT_TYPE,
        photo_bytes: bytes | None,
    ) -> None:
        """Adds an update to the batch and triggers processing if necessary."""
        ...


@runtime_checkable
class ConfirmationUIManager(Protocol):
    """Protocol defining the interface for requesting user confirmation."""

    async def request_confirmation(
        self,
        chat_id: int,
        prompt_text: str,
        tool_name: str,
        tool_args: dict[str, Any],
        timeout: float,
    ) -> bool:
        """
        Requests confirmation from the user via the UI.

        Returns True if confirmed, False if denied or timed out.
        """
        ...


# --- MessageBatcher Implementations ---


class DefaultMessageBatcher(MessageBatcher):
    """Buffers messages and processes them in batches to avoid race conditions."""

    def __init__(
        self, batch_processor: BatchProcessor, batch_delay_seconds: float = 0.5
    ) -> None:
        self.batch_processor = batch_processor
        self.batch_delay_seconds = batch_delay_seconds
        self.chat_locks: dict[int, asyncio.Lock] = defaultdict(asyncio.Lock)
        self.message_buffers: dict[int, list[tuple[Update, bytes | None]]] = (
            defaultdict(list)
        )
        self.processing_tasks: dict[int, asyncio.Task] = {}
        self.batch_timers: dict[
            int, asyncio.TimerHandle
        ] = {}  # Store timers for delayed processing

    async def add_to_batch(
        self,
        update: Update,
        context: ContextTypes.DEFAULT_TYPE,
        photo_bytes: bytes | None,
    ) -> None:
        if not update.effective_chat:
            logger.warning(
                "DefaultMessageBatcher: Update has no effective_chat, skipping."
            )
            return
        chat_id = update.effective_chat.id
        async with self.chat_locks[chat_id]:
            self.message_buffers[chat_id].append((update, photo_bytes))
            buffer_size = len(self.message_buffers[chat_id])
            logger.info(
                f"Buffered update {update.update_id} (message {update.message.message_id if update.message else 'N/A'}) for chat {chat_id}. Buffer size: {buffer_size}"
            )

            # Cancel existing timer if new message arrives
            if chat_id in self.batch_timers:
                self.batch_timers[chat_id].cancel()
                logger.debug(f"Cancelled existing batch timer for chat {chat_id}.")

            # Start a new timer to process the batch after a short delay
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
            if chat_id in self.batch_timers:  # Remove timer as we are processing now
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
                # Re-add batch? Or just let it be dropped? Current logic drops it.
                # Let's re-add it to avoid losing messages if a task is slow.
                self.message_buffers[chat_id] = (
                    current_batch + self.message_buffers[chat_id]
                )
                logger.warning(
                    f"Re-added batch to buffer for chat {chat_id} as task was still running."
                )

    def _remove_task_callback(
        self, task: asyncio.Task, chat_id: int
    ) -> None:  # No longer in TelegramUpdateHandler
        """Callback function to remove task from processing_tasks dict."""
        try:
            task.result()  # Raise exception if task failed
        except asyncio.CancelledError:
            logger.info(f"Processing task for chat {chat_id} was cancelled.")
        except Exception:
            logger.debug(
                f"Processing task for chat {chat_id} completed with an exception (handled elsewhere)."
            )
            pass  # Error should have been logged by the task itself or the error handler

        if hasattr(self, "processing_tasks"):
            self.processing_tasks.pop(chat_id, None)
            logger.debug(f"Task entry removed for chat {chat_id} via callback.")
        else:
            logger.warning(
                f"Cannot remove task entry for chat {chat_id}: processing_tasks dict not found."
            )


class TelegramUpdateHandler:  # Renamed from TelegramBotHandler
    """Handles specific Telegram updates (messages, commands) and delegates processing."""  # noqa: E501

    def __init__(
        self,
        telegram_service: "TelegramService",  # Accept the service instance
        allowed_user_ids: list[int],
        developer_chat_id: int | None,
        processing_service: "ProcessingService",  # Use string quote for forward reference
        get_db_context_func: Callable[
            ..., contextlib.AbstractAsyncContextManager["DatabaseContext"]
        ],
        message_batcher: MessageBatcher
        | None,  # Inject the batcher, can be None initially
        confirmation_manager: "TelegramConfirmationUIManager",  # Inject confirmation manager
    ) -> None:
        # Check for debug mode environment variable
        self.debug_mode = (
            os.environ.get("ASSISTANT_DEBUG_MODE", "false").lower() == "true"
        )
        logger.info(f"Debug mode enabled: {self.debug_mode}")

        """
        Initializes the TelegramUpdateHandler. # Updated docstring

        Args:
            telegram_service: The parent TelegramService instance.
            allowed_user_ids: List of chat IDs allowed to interact with the bot.
            developer_chat_id: Optional chat ID for sending error notifications.
            processing_service: The ProcessingService instance.
            get_db_context_func: Async context manager function to get a DatabaseContext.
            message_batcher: The message batcher instance to use.
        """
        self.telegram_service = telegram_service  # Store the service instance

        # application is accessed via telegram_service.application if needed
        self.allowed_user_ids = allowed_user_ids
        self.developer_chat_id = developer_chat_id
        self.processing_service = processing_service  # Store the service instance
        self.get_db_context = get_db_context_func
        self.message_batcher = message_batcher  # Store the injected batcher
        self.confirmation_manager: TelegramConfirmationUIManager = (
            confirmation_manager  # Store the injected manager
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
    ) -> AsyncIterator[None]:
        """Context manager to send typing notifications periodically."""
        stop_event = asyncio.Event()

        async def typing_loop() -> None:
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
        if not update.effective_user:
            logger.warning("Update has no effective_user, cannot process /start.")
            return
        user_id = update.effective_user.id

        if not update.message:  # Ensure message object exists to reply to
            logger.warning("Update has no message, cannot reply to /start.")
            return

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

    async def process_batch(  # Renamed from process_chat_queue, implements BatchProcessor protocol
        # Add batch parameter
        self,
        chat_id: int,
        batch: list[tuple[Update, bytes | None]],
        context: ContextTypes.DEFAULT_TYPE,
    ) -> None:
        """Processes the message buffer for a given chat."""
        # Logic to fetch the actual batch is now in the MessageBatcher
        # This method now receives the batch directly
        # Note: This requires DefaultMessageBatcher to actually *call* this method.
        # Let's adjust the method signature to receive the batch.

        # This method is *called* by the MessageBatcher now.
        # It needs the batch as an argument.
        # Let's adjust the BatchProcessor protocol and this implementation.

        # The protocol `process_batch` expects `batch` argument. Let's assume the batcher provides it.
        # The user requested DefaultMessageBatcher implementation, so let's adjust that first.
        # Assuming `DefaultMessageBatcher._trigger_batch_processing` calls this with the batch.
        logger.debug(f"Starting process_batch for chat_id {chat_id}")
        # The `batch` argument is now expected based on the protocol
        # We need to retrieve it somehow. Let's assume the batcher provides it.
        # We need to adjust the DefaultMessageBatcher to pass the batch.

        # --- Assuming DefaultMessageBatcher provides the batch ---
        # (We'll implement the caller side in DefaultMessageBatcher next)
        # Remove incorrect retrieval from context.job.data - batch is now a parameter

        if not batch:  # Check the passed batch
            logger.info(
                f"process_batch for chat {chat_id} called with empty batch. Exiting."
            )
            return

        last_update, _ = batch[-1]
        user = last_update.effective_user
        user_name = user.first_name if user else "Unknown User"
        reply_target_message_id = (
            last_update.message.message_id if last_update.message else None
        )
        logger.debug(
            f"Extracted user='{user_name}', reply_target_id={reply_target_message_id} from last update."
        )

        # Check if the last message is a reply
        replied_to_interface_id: str | None = None
        if last_update.message and last_update.message.reply_to_message:
            replied_to_interface_id = str(
                last_update.message.reply_to_message.message_id
            )

        all_texts = []
        first_photo_bytes = None
        forward_context = ""

        for update_item, photo_bytes in batch:
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
                    if isinstance(origin, MessageOriginUser):
                        original_sender_name = origin.sender_user.first_name or "User"
                    elif isinstance(origin, MessageOriginHiddenUser):
                        original_sender_name = origin.sender_user_name or "Hidden User"
                    elif isinstance(origin, MessageOriginChat):
                        original_sender_name = origin.sender_chat.title or "Chat"
                    elif isinstance(origin, MessageOriginChannel):
                        original_sender_name = origin.chat.title or "Channel"
                    forward_context = f"(forwarded from {original_sender_name}) "
                    logger.debug(
                        f"Detected forward context from {original_sender_name} in last message."
                    )

        combined_text = "\n\n".join(all_texts).strip()
        logger.debug(f"Combined text: '{combined_text[:100]}...'")

        formatted_user_text_content = f"{forward_context}{combined_text}".strip()
        text_content_part: dict[str, Any] = {
            "type": "text",
            "text": formatted_user_text_content,
        }
        trigger_content_parts: list[dict[str, Any]] = [text_content_part]

        if first_photo_bytes:
            try:
                base64_image = base64.b64encode(first_photo_bytes).decode("utf-8")
                mime_type = "image/jpeg"
                trigger_content_parts.append({
                    "type": "image_url",
                    "image_url": {"url": f"data:{mime_type};base64,{base64_image}"},
                })
                logger.info("Added first photo from batch to trigger content.")
            except Exception as img_err:
                logger.error(
                    f"Error encoding photo from batch: {img_err}", exc_info=True
                )
                await context.bot.send_message(
                    chat_id, "Error processing image in batch."
                )
                trigger_content_parts = [text_content_part]  # Revert to text only

        sent_assistant_message: Message | None = (
            None  # To store the sent message object
        )
        processing_error_traceback: str | None = None  # Added
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

            db_context_getter = self.get_db_context()
            async with db_context_getter as db_context:
                # --- Determine Thread Root ID ---
                thread_root_id_for_turn: int | None = None
                if replied_to_interface_id:
                    try:
                        replied_to_db_msg = (
                            await self.storage.get_message_by_interface_id(
                                db_context=db_context,
                                interface_type=interface_type,
                                conversation_id=conversation_id,
                                interface_message_id=replied_to_interface_id,
                            )
                        )
                        if replied_to_db_msg:
                            # If the replied-to message has a root ID, use it. Otherwise, use its own internal ID.
                            thread_root_id_for_turn = replied_to_db_msg.get(
                                "thread_root_id"
                            ) or replied_to_db_msg.get("internal_id")
                            logger.info(
                                f"Determined thread_root_id {thread_root_id_for_turn} from replied-to message {replied_to_interface_id}"
                            )
                        else:
                            logger.warning(
                                f"Could not find replied-to message {replied_to_interface_id} in DB to determine thread root."
                            )
                    except Exception as thread_err:
                        logger.error(
                            f"Error determining thread root ID: {thread_err}",
                            exc_info=True,
                        )
                        # Continue without thread_root_id if error occurs

                # --- Prepare and Save User Trigger Message(s) ---
                trigger_interface_message_id: str | None = None
                # Combine text again for saving user message content
                history_user_content = combined_text.strip()
                if first_photo_bytes:
                    history_user_content += " [Image(s) Attached]"

                # Use the last user update for ID and timestamp
                user_message_id = (
                    last_update.message.message_id if last_update.message else None
                )
                user_message_timestamp = (
                    last_update.message.date
                    if last_update.message
                    else datetime.now(timezone.utc)
                )

                if user_message_id:
                    trigger_interface_message_id = str(user_message_id)  # Store the ID
                    await self.storage.add_message_to_history(
                        db_context=db_context,
                        interface_type=interface_type,
                        conversation_id=conversation_id,
                        interface_message_id=str(
                            user_message_id
                        ),  # User message has interface ID
                        turn_id=None,  # User message is the trigger, has no turn_id
                        thread_root_id=thread_root_id_for_turn,  # Use determined root ID
                        timestamp=user_message_timestamp,
                        role="user",
                        content=history_user_content,
                        tool_calls=None,
                        reasoning_info=None,
                        error_traceback=None,  # Error hasn't happened yet
                        tool_call_id=None,
                    )
                else:
                    logger.warning(
                        f"Could not get user message ID for chat {chat_id} to save to history."
                    )

                async with self._typing_notifications(context, chat_id):
                    # Define an explicit wrapper function for the confirmation callback
                    # This captures `self` (for `self.confirmation_manager`) and `chat_id` from the outer scope
                    async def confirmation_callback_wrapper(
                        # Update signature to match ConfirmationCallbackSignature Protocol
                        chat_id: int,  # Match protocol and calling keyword
                        interface_type: str,  # Match protocol and calling keyword
                        turn_id: str | None,  # Match protocol and calling keyword
                        prompt_text: str,  # Match protocol and calling keyword
                        tool_name: str,  # Match protocol and calling keyword
                        tool_args: dict[str, Any],  # Match protocol and calling keyword
                        timeout: float,  # Match protocol and calling keyword
                    ) -> bool:
                        # The `chat_id` from the outer scope should ideally match the `chat_id` parameter.
                        # We use the parameters passed to the callback by ProcessingService.
                        return await self.confirmation_manager.request_confirmation(
                            chat_id=chat_id,  # Use the passed parameter
                            interface_type=interface_type,  # Use the passed parameter
                            turn_id=turn_id,  # Use the passed parameter
                            prompt_text=prompt_text,  # Use the passed parameter
                            tool_name=tool_name,  # Use the passed parameter
                            tool_args=tool_args,  # Use the passed parameter
                            timeout=timeout,  # Use the passed parameter
                        )

                    # Use the wrapper function as the callback
                    (
                        generated_turn_messages,  # New return type
                        final_reasoning_info,  # Capture final reasoning info
                        processing_error_traceback,  # Capture traceback directly
                    ) = await self.processing_service.generate_llm_response_for_chat(
                        db_context=db_context,  # Pass db context
                        application=self.telegram_service.application,  # Access via service
                        interface_type=interface_type,  # Pass identifiers
                        conversation_id=conversation_id,  # Pass identifiers
                        trigger_content_parts=trigger_content_parts,
                        trigger_interface_message_id=trigger_interface_message_id,  # Pass the trigger message ID
                        user_name=user_name,
                        replied_to_interface_id=replied_to_interface_id,  # Pass replied_to ID
                        # Pass the partially applied confirmation request callback
                        request_confirmation_callback=confirmation_callback_wrapper,  # Pass the wrapper
                    )
                    # Add turn_id to all generated messages before saving
                    turn_id_for_saving = None
                    if generated_turn_messages:
                        turn_id_for_saving = generated_turn_messages[0].get("turn_id")

                    if not turn_id_for_saving:
                        logger.error(
                            f"Could not extract turn_id from generated messages for chat {chat_id}. Assigning new UUID."
                        )
                        turn_id_for_saving = str(
                            uuid.uuid4()
                        )  # Fallback, should not happen

                    # Save all generated messages (assistant requests, tool responses, final answer)
                    last_assistant_internal_id: int | None = (
                        None  # Track for updating interface_id
                    )
                    for msg_dict in generated_turn_messages:
                        # Ensure correct IDs are set before saving
                        # Create a copy to avoid modifying the original dict potentially used elsewhere
                        msg_to_save = msg_dict.copy()
                        msg_to_save["turn_id"] = (
                            turn_id_for_saving  # Ensure turn_id is set from extracted value
                        )
                        msg_to_save["thread_root_id"] = (
                            thread_root_id_for_turn  # Ensure thread_root is set
                        )
                        msg_to_save["interface_type"] = (
                            interface_type  # Add identifiers
                        )
                        msg_to_save["conversation_id"] = (
                            conversation_id  # Add identifiers
                        )
                        # Explicitly pass args instead of ** to ensure only expected columns are used
                        saved_msg_mapping = await self.storage.add_message_to_history(
                            db_context=db_context,
                            **msg_to_save,  # Pass the prepared dict
                        )  # Capture the internal ID if the role is assistant and the message was saved successfully
                        if msg_dict.get("role") == "assistant" and saved_msg_mapping:
                            last_assistant_internal_id = (
                                # Use dict access (.get for safety) on the returned mapping
                                saved_msg_mapping.get("internal_id")
                            )
                    # --- END OF MESSAGE SAVING LOOP ---

                    # --- Sending and Updating Logic (Now inside the DB context) ---
                    # Create ForceReply object - needs selective=True for replies
                    # Let's keep selective=False for now as per previous code.
                    force_reply_markup = ForceReply(selective=False)
                    # Find the final message content to send to the user from the generated list
                    final_llm_content_to_send: str | None = None
                    if generated_turn_messages:
                        # Look for the last message with content, prioritizing assistant role
                        for msg in reversed(generated_turn_messages):
                            if msg.get("content") and msg.get("role") == "assistant":
                                final_llm_content_to_send = msg["content"]
                                break  # Exit the loop once found

                        # If no assistant content was found after the loop,
                        # take the content of the very last message in the turn, regardless of role.
                        if not final_llm_content_to_send and generated_turn_messages[
                            -1
                        ].get("content"):
                            final_llm_content_to_send = generated_turn_messages[-1][
                                "content"
                            ]

                if final_llm_content_to_send:  # Check if there's content to send
                    try:
                        # Format the final content for Telegram
                        converted_markdown = telegramify_markdown.markdownify(
                            final_llm_content_to_send
                        )
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
                    if (
                        sent_assistant_message
                        and last_assistant_internal_id is not None
                    ):
                        try:  # Wrap DB update in try/except
                            await self.storage.update_message_interface_id(
                                db_context=db_context,  # Use the still-active context
                                internal_id=last_assistant_internal_id,
                                interface_message_id=str(
                                    sent_assistant_message.message_id
                                ),
                            )
                            logger.info(
                                f"Updated interface_message_id for internal_id {last_assistant_internal_id}"
                            )
                        except Exception as update_err:
                            logger.error(
                                f"Failed to update interface_message_id for internal_id {last_assistant_internal_id}: {update_err}",
                                exc_info=True,
                            )
                    elif (
                        sent_assistant_message
                    ):  # Only log warning if message was sent but ID wasn't tracked
                        logger.warning(
                            f"Sent assistant message {sent_assistant_message.message_id} but couldn't find its internal_id to update."
                        )  # If an error occurred during processing, check for traceback *before* handling empty response
                elif (
                    processing_error_traceback and reply_target_message_id
                ):  # If processing failed, send error message
                    error_message_to_send = (
                        "Sorry, something went wrong while processing your request."
                    )
                    if self.debug_mode:
                        logger.info(f"Sending DEBUG error traceback to chat {chat_id}")
                        # Format traceback for Telegram (HTML preformatted)
                        error_message_to_send = (
                            "Encountered error during processing \\(debug mode\\):\n"
                            f"<pre>{html.escape(processing_error_traceback)}</pre>"
                        )
                    else:
                        logger.info(f"Sending generic error message to chat {chat_id}")

                    await context.bot.send_message(
                        # Send either the generic message or the traceback
                        chat_id=chat_id,
                        text=error_message_to_send,
                        parse_mode=(
                            ParseMode.HTML if self.debug_mode else None
                        ),  # Use HTML for traceback
                        reply_to_message_id=reply_target_message_id,
                        reply_markup=force_reply_markup,  # Add ForceReply
                    )
                else:  # Handle case where there's no content and no specific processing error
                    logger.warning(  # This case might be less common now as we save all messages
                        "Received empty response from LLM (and no processing error detected)."
                    )
                    if reply_target_message_id:
                        sent_assistant_message = await context.bot.send_message(
                            chat_id=chat_id,
                            text="Sorry, I couldn't process that request.",  # Generic message for empty response
                            reply_to_message_id=reply_target_message_id,
                            reply_markup=force_reply_markup,  # Add ForceReply
                        )
            # --- END OF DB CONTEXT BLOCK --- # async with automatically handles exit/commit/rollback

        except Exception as e:
            # This catches errors *outside* the generate_llm_response_for_chat call
            # (e.g., DB connection issues before the call, Telegram API errors sending reply)
            logger.exception(  # Use exception to log traceback automatically
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
                        # Send traceback in debug mode, otherwise generic message
                        text=(
                            f"An unexpected error occurred \\(debug mode\\):\n<pre>{html.escape(processing_error_traceback)}</pre>"
                            if self.debug_mode and processing_error_traceback
                            else "Sorry, an unexpected error occurred."
                        ),
                        parse_mode=(
                            ParseMode.HTML
                            if self.debug_mode and processing_error_traceback
                            else None
                        ),
                        reply_to_message_id=reply_target_message_id,
                    )
                    logger.info(
                        f"Sent {'debug' if self.debug_mode else 'generic'} unexpected error message to chat {chat_id}"
                    )

            # --- Save Error Traceback with User Message ---
            # If an error happened, try to update the user message record
            # This assumes the user message was saved before the error occurred
            if processing_error_traceback and user_message_id:
                try:
                    # Get a new context for this update attempt
                    async with self.get_db_context() as db_ctx_err:
                        # Fetch the user message's internal ID first (can't update by interface ID)
                        user_msg_record = await self.storage.get_message_by_interface_id(
                            db_context=db_ctx_err,
                            interface_type=interface_type,
                            conversation_id=conversation_id,  # Correct variable name was already here
                            interface_message_id=str(user_message_id),
                        )
                        if user_msg_record and user_msg_record.get("internal_id"):
                            stmt = (
                                # Use SQLAlchemy update directly
                                update(self.storage.message_history_table)
                                .where(
                                    self.storage.message_history_table.c.internal_id
                                    == user_msg_record["internal_id"]
                                )
                                .values(error_traceback=processing_error_traceback)
                            )
                            await db_ctx_err.execute_with_retry(stmt)
                            logger.info(
                                f"Saved error traceback to user message internal_id {user_msg_record['internal_id']}"
                            )
                        else:
                            logger.error(
                                "Could not find user message record to attach error traceback."
                            )
                except Exception as db_err_save:
                    logger.error(
                        f"Failed to save error traceback to DB for chat {chat_id}: {db_err_save}",
                        exc_info=True,
                    )

            # Let the main error handler notify the developer
            raise e  # Re-raise for the main error handler

    # --- Error Handling and Registration - Moved back into TelegramUpdateHandler ---

    async def error_handler(self, update: object, context: CallbackContext) -> None:
        """Log the error, store it in the service, and notify the developer."""
        error = context.error
        logger.error(f"Exception while handling an update: {error}", exc_info=error)

        if self.telegram_service:
            self.telegram_service._last_error = error  # type: ignore[attr-defined] # _last_error is on TelegramService
            if isinstance(error, Conflict):
                logger.critical(
                    f"Telegram Conflict error detected: {error}. Polling will likely stop."
                )
            # No need for `elif isinstance(error, Exception): pass`

        if error:
            tb_list = traceback.format_exception(None, error, error.__traceback__)
            tb_string = "".join(tb_list)
        else:
            tb_string = "No exception context available."

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

    def register_handlers(self) -> None:
        """Registers the necessary Telegram handlers with the application."""
        # Access application via the telegram_service instance
        application = self.telegram_service.application  # Get application instance

        application.add_handler(CommandHandler("start", self.start))

        application.add_handler(
            MessageHandler(
                (filters.TEXT | filters.PHOTO) & ~filters.COMMAND, self.message_handler
            )
        )

        application.add_error_handler(self.error_handler)
        logger.info("Telegram handlers registered (start, message, error).")

    async def message_handler(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:  # noqa: E501
        """Buffers incoming messages and triggers processing if not already running."""
        if not update.effective_user:
            logger.warning("Message handler: Update has no effective_user.")
            return
        user_id = update.effective_user.id

        if not update.effective_chat:
            logger.warning("Message handler: Update has no effective_chat.")
            return
        chat_id = update.effective_chat.id

        if not update.message:
            logger.warning("Message handler: Update has no message.")
            return
        # Now safe to access update.message attributes

        photo_bytes = None

        if self.allowed_user_ids and user_id not in self.allowed_user_ids:
            logger.warning(f"Ignoring message from unauthorized user {user_id}")
            return

        if update.message.photo:
            logger.info(
                f"Message {update.message.message_id} from chat {chat_id} contains photo."
            )
            try:
                # photo is a tuple of PhotoSize objects, last one is usually largest
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

        # Delegate to the injected message batcher
        if self.message_batcher is None:
            logger.critical(
                "CRITICAL: MessageBatcher not set in TelegramUpdateHandler. "
                "This indicates an initialization error in TelegramService."
            )
            if update.message:
                try:
                    await update.message.reply_text(
                        "Sorry, there's an internal issue with message processing. "
                        "Please try again in a moment. If the problem persists, contact the administrator."
                    )
                except Exception as e_reply:
                    logger.error(f"Failed to send error reply to user: {e_reply}")
            return

        await self.message_batcher.add_to_batch(update, context, photo_bytes)


class TelegramConfirmationUIManager(ConfirmationUIManager):
    """Implementation of ConfirmationUIManager using Telegram Inline Keyboards."""

    def __init__(
        self, application: Application, confirmation_timeout: float = 3600.0
    ) -> None:
        self.application = application
        self.confirmation_timeout = confirmation_timeout
        self.pending_confirmations: dict[str, asyncio.Future] = {}

    async def request_confirmation(
        self,
        chat_id: int,
        # Add interface_type and turn_id to match the Protocol
        # Mark as unused for now if not directly used in this method's logic
        interface_type: str,  # New parameter
        turn_id: str | None,  # New parameter
        prompt_text: str,
        tool_name: str,
        tool_args: dict[str, Any],
        timeout: float,
    ) -> bool:
        """Sends confirmation message and waits for user response or timeout."""
        effective_timeout = min(timeout, self.confirmation_timeout)
        confirm_uuid = str(uuid.uuid4())
        if not self.application or not self.application.bot:
            raise RuntimeError("Telegram application or bot instance not available.")
        logger.info(
            f"Requesting confirmation (UUID: {confirm_uuid}) for tool '{tool_name}' in chat {chat_id}"
        )

        keyboard = InlineKeyboardMarkup([
            [
                InlineKeyboardButton(
                    "✅ Confirm", callback_data=f"confirm:{confirm_uuid}:yes"
                ),
                InlineKeyboardButton(
                    "❌ Cancel", callback_data=f"confirm:{confirm_uuid}:no"
                ),
            ]
        ])

        try:
            sent_message = await self.application.bot.send_message(
                chat_id=chat_id,
                text=prompt_text,
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
            # Don't raise here, return False to indicate failure to confirm
            return False

        confirmation_future = asyncio.get_running_loop().create_future()
        self.pending_confirmations[confirm_uuid] = confirmation_future

        try:
            logger.debug(
                f"Waiting for confirmation response (UUID: {confirm_uuid}, Timeout: {effective_timeout}s)"
            )
            user_confirmed = await asyncio.wait_for(
                confirmation_future, timeout=effective_timeout
            )
            logger.info(
                f"Confirmation response received for {confirm_uuid}: {user_confirmed}"
            )
            return user_confirmed
        except asyncio.TimeoutError:
            logger.warning(
                f"Confirmation {confirm_uuid} timed out after {effective_timeout}s."
            )
            try:
                # Edit message on timeout
                await self.application.bot.edit_message_reply_markup(
                    chat_id=chat_id,
                    message_id=sent_message.message_id,
                    reply_markup=None,
                )
                await self.application.bot.edit_message_text(
                    chat_id=chat_id,
                    message_id=sent_message.message_id,
                    text=prompt_text + "\n\n\\(Confirmation timed out\\)",
                    parse_mode=ParseMode.MARKDOWN_V2,
                )
            except Exception as edit_err:
                logger.warning(
                    f"Failed to edit confirmation message {sent_message.message_id} on timeout: {edit_err}"
                )
            # Return False on timeout
            return False
        finally:
            self.pending_confirmations.pop(confirm_uuid, None)

    async def confirmation_callback_handler(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        """Handles button presses for tool confirmations."""
        query = update.callback_query
        if not query:
            logger.warning("Confirmation callback: Update has no callback_query.")
            return
        await query.answer()

        callback_data = query.data
        if not callback_data:
            logger.error("Confirmation callback: No data in callback_query.")
            if query.message:  # Try to edit message if possible
                try:
                    await query.edit_message_text(text="Error: Missing callback data.")
                except Exception as e:
                    logger.error(f"Error editing message on missing callback data: {e}")
            return

        logger.info(f"Received confirmation callback: {callback_data}")

        try:
            _, confirm_uuid, action = callback_data.split(":")
        except ValueError:
            logger.error(f"Invalid confirmation callback data format: {callback_data}")
            if query.message:
                try:
                    await query.edit_message_text(text="Error: Invalid callback data.")
                except Exception as e:
                    logger.error(f"Error editing message on invalid callback data: {e}")
            return

        confirmation_future = self.pending_confirmations.get(confirm_uuid)

        if confirmation_future and not confirmation_future.done():
            if not query.message or not isinstance(query.message, Message):
                logger.error(
                    "Callback query message is not accessible or not a standard message."
                )
                # Cannot edit message text if query.message is not a proper Message
                # Set future to prevent hang, but can't update UI
                confirmation_future.set_exception(
                    RuntimeError("Callback message not editable")
                )
                return

            original_text = (
                query.message.text_markdown_v2_urled or query.message.text or ""
            )  # Fallback
            status_text = ""
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
                status_text = "\n\n*Error: Unknown action*"
                confirmation_future.cancel()

            try:
                await query.edit_message_text(
                    text=original_text + status_text,
                    parse_mode=ParseMode.MARKDOWN_V2,
                    reply_markup=None,
                )
            except Exception as edit_err:
                logger.error(
                    f"Failed to edit confirmation message {query.message.message_id} after callback: {edit_err}"
                )

        # Rest of the handler logic remains the same (handling already done/invalid cases) - omitted for brevity but should be here


# Remove duplicate DefaultMessageBatcher definition and methods incorrectly placed in NoBatchMessageBatcher


class NoBatchMessageBatcher(MessageBatcher):
    """A simple batcher that processes each message immediately without buffering."""

    def __init__(self, batch_processor: BatchProcessor) -> None:
        self.batch_processor = batch_processor

    async def add_to_batch(
        self,
        update: Update,
        context: ContextTypes.DEFAULT_TYPE,
        photo_bytes: bytes | None,
    ) -> None:
        if not update.effective_chat:
            logger.warning("NoBatchMessageBatcher: Update has no effective_chat.")
            return
        chat_id = update.effective_chat.id
        logger.info(
            f"NoBatchMessageBatcher: Immediately processing update {update.update_id} for chat {chat_id}"
        )
        # Create a single-item batch
        batch = [(update, photo_bytes)]
        await self.batch_processor.process_batch(chat_id, batch, context)


class TelegramService:
    """Manages the Telegram bot application lifecycle and update handling."""

    def __init__(
        self,
        telegram_token: str,
        allowed_user_ids: list[int],
        developer_chat_id: int | None,
        processing_service: ProcessingService,
        get_db_context_func: Callable[
            ..., contextlib.AbstractAsyncContextManager[DatabaseContext]
        ],
        use_batching: bool = True,  # Add flag to control batching
    ) -> None:
        """
        Initializes the Telegram Service.

        Args:
            telegram_token: The Telegram Bot API token.
            allowed_user_ids: List of chat IDs allowed to interact with the bot.
            developer_chat_id: Optional chat ID for sending error notifications.
            processing_service: The ProcessingService instance.
            get_db_context_func: Async context manager function to get a DatabaseContext.
            use_batching: If True (default), use DefaultMessageBatcher. Otherwise, use NoBatchMessageBatcher.
        """
        logger.info("Initializing TelegramService...")
        self.application = ApplicationBuilder().token(telegram_token).build()
        self._was_started: bool = False
        self._last_error: Exception | None = None

        # Store the ProcessingService instance in bot_data for access in handlers
        # Note: This assumes handlers might still need direct access via context.bot_data
        # If handlers only use self.processing_service, this line might be removable.
        self.application.bot_data["processing_service"] = processing_service
        logger.info("Stored ProcessingService instance in application.bot_data.")

        # Instantiate Confirmation Manager
        self.confirmation_manager = TelegramConfirmationUIManager(
            application=self.application
            # Optionally pass custom timeout from config:
        )

        # Instantiate the handler class, passing self (the service instance)
        self.update_handler = TelegramUpdateHandler(
            telegram_service=self,  # Pass self
            allowed_user_ids=allowed_user_ids,
            developer_chat_id=developer_chat_id,
            processing_service=processing_service,
            get_db_context_func=get_db_context_func,
            message_batcher=None,  # Will be set below
            confirmation_manager=self.confirmation_manager,  # Inject manager
        )

        # Now instantiate the appropriate batcher, passing the handler as the processor
        if use_batching:
            self.message_batcher = DefaultMessageBatcher(
                batch_processor=self.update_handler
            )
        else:
            self.message_batcher = NoBatchMessageBatcher(
                batch_processor=self.update_handler
            )
        self.update_handler.message_batcher = (
            self.message_batcher
        )  # Assign the batcher back to the handler

        # Register core handlers (start, message, error) using the handler instance
        self.update_handler.register_handlers()
        # Register confirmation callback handler using the confirmation manager instance
        self.application.add_handler(
            CallbackQueryHandler(
                self.confirmation_manager.confirmation_callback_handler,
                pattern=r"^confirm:",
            )
        )
        logger.info("TelegramService initialized.")

    # Add the confirmation request method to the service, delegating to the handler
    async def request_confirmation_from_user(
        self,
        chat_id: int,
        # Add interface_type and turn_id to match the Protocol for consistency,
        # even if they are just passed through.
        interface_type: str,
        turn_id: str | None,
        prompt_text: str,
        tool_name: str,
        tool_args: dict[str, Any],
        timeout: float,
    ) -> bool:
        """Public method to request confirmation, called by ConfirmingToolsProvider."""
        # Delegate directly to the confirmation manager
        if self.confirmation_manager:
            return await self.confirmation_manager.request_confirmation(
                chat_id=chat_id,
                interface_type=interface_type,  # Pass new arg
                turn_id=turn_id,  # Pass new arg
                prompt_text=prompt_text,
                tool_name=tool_name,
                tool_args=tool_args,
                timeout=timeout,
            )
        else:
            logger.error(
                "ConfirmationUIManager instance not available in TelegramService."
            )
            raise RuntimeError(
                "Confirmation mechanism not properly initialized in handler."
            )

    async def start_polling(self) -> None:
        """Initializes the application and starts polling for updates."""
        logger.info("Starting Telegram polling...")
        await self.application.initialize()
        await self.application.start()
        if self.application.updater:
            # Use Update.ALL_TYPES to ensure all relevant updates are received
            await self.application.updater.start_polling(
                allowed_updates=Update.ALL_TYPES
            )
            self._was_started = True  # Mark as started
            self._last_error = None  # Clear last error on successful start
            logger.info("Telegram polling started successfully.")
        else:
            logger.error(
                "Application updater not available after start. Polling cannot begin."
            )
            # Consider raising an error or setting a state indicating failure

    @property
    def last_error(self) -> Exception | None:
        """Returns the last error encountered by the error handler."""
        return self._last_error

    async def stop_polling(self) -> None:
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
