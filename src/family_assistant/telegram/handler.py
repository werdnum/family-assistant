from __future__ import annotations

import asyncio
import base64
import contextlib
import html
import io
import logging
import os
import traceback
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any

import telegramify_markdown
from sqlalchemy import update as sqlalchemy_update
from telegram import (
    ForceReply,
    Message,
    MessageOriginChannel,
    MessageOriginChat,
    MessageOriginHiddenUser,
    MessageOriginUser,
    Update,
)
from telegram.constants import ChatAction, ParseMode
from telegram.error import Conflict
from telegram.ext import (
    CallbackContext,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from family_assistant.indexing.processors.text_processors import TextChunker
from family_assistant.storage.message_history import (
    message_history_table,  # For error handling db update
)
from family_assistant.tools.confirmation import TOOL_CONFIRMATION_RENDERERS

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Callable

    from family_assistant.interfaces import ChatInterface
    from family_assistant.processing import ProcessingService
    from family_assistant.storage.context import DatabaseContext
    from family_assistant.telegram.protocols import MessageBatcher
    from family_assistant.telegram.service import TelegramService
    from family_assistant.telegram.ui import TelegramConfirmationUIManager

logger = logging.getLogger(__name__)

TELEGRAM_MAX_MESSAGE_LENGTH = 4000


class TelegramUpdateHandler:  # Renamed from TelegramBotHandler
    """Handles specific Telegram updates (messages, commands) and delegates processing."""  # noqa: E501

    def __init__(
        self,
        telegram_service: TelegramService,  # Accept the service instance
        allowed_user_ids: list[int],
        developer_chat_id: int | None,
        processing_service: ProcessingService,  # Use string quote for forward reference
        get_db_context_func: Callable[
            ..., contextlib.AbstractAsyncContextManager[DatabaseContext]
        ],
        message_batcher: MessageBatcher
        | None,  # Inject the batcher, can be None initially
        confirmation_manager: TelegramConfirmationUIManager,  # Inject confirmation manager
    ) -> None:
        """Initializes the TelegramUpdateHandler.

        Args:
            telegram_service: The parent TelegramService instance.
            allowed_user_ids: List of chat IDs allowed to interact with the bot.
            developer_chat_id: Chat ID for developer notifications.
            processing_service: The processing service for handling interactions.
            get_db_context_func: Function to get database context.
            message_batcher: Message batcher for grouping messages.
            confirmation_manager: Manager for tool confirmation UI.
        """
        # Check for debug mode environment variable
        # Task event notification is now handled automatically in storage layer
        self.debug_mode = (
            os.environ.get("ASSISTANT_DEBUG_MODE", "false").lower() == "true"
        )
        logger.info(f"Debug mode enabled: {self.debug_mode}")

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
        # Storage operations are now accessed via DatabaseContext
        self.text_chunker = TextChunker(
            chunk_size=TELEGRAM_MAX_MESSAGE_LENGTH,
            chunk_overlap=50,  # Small overlap to maintain context across messages
            separators=("\n\n", "\n", ". ", " ", ""),
        )

    def _get_chat_interfaces(self) -> dict[str, ChatInterface] | None:
        """Get chat_interfaces registry from FastAPI app state for cross-interface messaging.

        Returns:
            Dictionary mapping interface types to ChatInterface instances, or None if unavailable
        """
        if self.telegram_service.fastapi_app:
            return getattr(
                self.telegram_service.fastapi_app.state, "chat_interfaces", None
            )
        return None

    async def _send_message_chunks(
        self,
        context: ContextTypes.DEFAULT_TYPE,
        chat_id: int,
        text: str,
        parse_mode: ParseMode | None,
        reply_to_message_id: int | None,
        reply_markup: ForceReply | None = None,
    ) -> Message | None:
        """Sends a message, splitting it into chunks if it's too long."""
        first_sent_message: Message | None = None
        if not text:  # Do not send empty messages
            logger.warning(
                f"Attempted to send empty message to chat {chat_id}. Aborting."
            )
            return None

        if len(text) > TELEGRAM_MAX_MESSAGE_LENGTH:
            logger.info(
                f"Message to chat {chat_id} exceeds {TELEGRAM_MAX_MESSAGE_LENGTH} chars. Splitting."
            )
            chunks = self.text_chunker._chunk_text_natively(text)
            if not chunks:
                logger.warning(
                    f"TextChunker returned no chunks for a long message to chat {chat_id}. Original text length: {len(text)}"
                )
                return None

            for i, chunk_text in enumerate(chunks):
                current_reply_to_id = reply_to_message_id if i == 0 else None
                current_reply_markup = reply_markup if i == 0 else None
                try:
                    sent_msg = await context.bot.send_message(
                        chat_id=chat_id,
                        text=chunk_text,
                        parse_mode=parse_mode,
                        reply_to_message_id=current_reply_to_id,
                        reply_markup=current_reply_markup,
                    )
                    if i == 0:
                        first_sent_message = sent_msg
                    if len(chunks) > 1 and i < len(chunks) - 1:
                        await asyncio.sleep(0.2)  # 200ms delay
                except Exception as e_chunk:
                    logger.error(
                        f"Failed to send chunk {i + 1}/{len(chunks)} to {chat_id}: {e_chunk}",
                        exc_info=True,
                    )
                    if (
                        i == 0
                    ):  # If the first chunk fails, we can't return a message object
                        return None
            return first_sent_message
        else:
            # Message is within limit, send as a single part
            try:
                sent_msg = await context.bot.send_message(
                    chat_id=chat_id,
                    text=text,
                    parse_mode=parse_mode,
                    reply_to_message_id=reply_to_message_id,
                    reply_markup=reply_markup,
                )
                return sent_msg
            except Exception as e:
                logger.error(
                    f"Failed to send single message to {chat_id}: {e}",
                    exc_info=True,
                )
                return None

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

    async def process_batch(
        self,
        chat_id: int,
        batch: list[tuple[Update, bytes | None]],
        context: ContextTypes.DEFAULT_TYPE,
    ) -> None:
        """Processes the message buffer for a given chat."""
        logger.debug(f"Starting process_batch for chat_id {chat_id}")

        if not batch:
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
        user_message_id: int | None = None
        logger.debug(
            f"Extracted user='{user_name}', reply_target_id={reply_target_message_id} from last update."
        )

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
        trigger_attachments: list[dict[str, Any]] | None = None

        if first_photo_bytes:
            try:
                attachment_metadata = await self.telegram_service.attachment_registry._store_file_only(
                    file_content=first_photo_bytes,
                    filename=f"telegram_photo_{datetime.now(timezone.utc).strftime('%Y%m%d_%H%M%S')}.jpg",
                    content_type="image/jpeg",
                )

                trigger_content_parts.append({
                    "type": "image_url",
                    "image_url": {"url": attachment_metadata.content_url},
                })

                trigger_attachments = [
                    {
                        "type": "image",
                        "content_url": attachment_metadata.content_url,
                        "name": attachment_metadata.description,
                        "size": attachment_metadata.size,
                        "content_type": attachment_metadata.mime_type,
                        "attachment_id": attachment_metadata.attachment_id,
                    }
                ]

                logger.info(
                    f"Stored Telegram photo as attachment: {attachment_metadata.attachment_id}"
                )
            except Exception as img_err:
                logger.error(
                    f"Error storing photo from batch: {img_err}", exc_info=True
                )
                await context.bot.send_message(
                    chat_id, "Error processing image in batch."
                )
                trigger_content_parts = [text_content_part]

        sent_assistant_message: Message | None = None
        processing_error_traceback: str | None = None
        logger.debug(f"Proceeding with trigger content and user '{user_name}'.")

        interface_type = "telegram"
        conversation_id = str(chat_id)

        try:
            selected_processing_service: ProcessingService = self.processing_service

            if not selected_processing_service:
                logger.error(
                    "Default ProcessingService not available in handler. Cannot generate response."
                )
                await context.bot.send_message(
                    chat_id, "Internal error: Default processing service unavailable."
                )
                return

            db_context_getter = self.get_db_context()
            async with db_context_getter as db_context:
                thread_root_id_for_turn: int | None = None
                replied_to_db_msg = None

                if replied_to_interface_id:
                    try:
                        replied_to_db_msg = (
                            await db_context.message_history.get_by_interface_id(
                                interface_type=interface_type,
                                interface_message_id=replied_to_interface_id,
                            )
                        )
                        if replied_to_db_msg:
                            thread_root_id_for_turn = replied_to_db_msg.get(
                                "thread_root_id"
                            ) or replied_to_db_msg.get("internal_id")
                            logger.info(
                                f"Determined thread_root_id {thread_root_id_for_turn} from replied-to message {replied_to_interface_id}"
                            )

                            original_profile_id = replied_to_db_msg.get(
                                "processing_profile_id"
                            )
                            if original_profile_id:
                                logger.info(
                                    f"Replied-to message (ID: {replied_to_interface_id}) has processing_profile_id: {original_profile_id}"
                                )
                                profile_specific_service = self.telegram_service.processing_services_registry.get(
                                    original_profile_id
                                )
                                if profile_specific_service:
                                    selected_processing_service = (
                                        profile_specific_service
                                    )
                                    logger.info(
                                        f"Switched to ProcessingService for profile '{original_profile_id}' for this reply."
                                    )
                                else:
                                    logger.warning(
                                        f"Profile ID '{original_profile_id}' from replied-to message not found in registry. "
                                        f"Falling back to default processing service ('{selected_processing_service.service_config.id}')."
                                    )
                            else:
                                logger.info(
                                    f"Replied-to message (ID: {replied_to_interface_id}) does not have a specific profile_id. "
                                    f"Using default processing service ('{selected_processing_service.service_config.id}')."
                                )
                        else:
                            logger.warning(
                                f"Could not find replied-to message {replied_to_interface_id} in DB. "
                                f"Using default processing service ('{selected_processing_service.service_config.id}')."
                            )
                    except Exception as thread_err:
                        logger.error(
                            f"Error determining thread root ID or profile from reply: {thread_err}",
                            exc_info=True,
                        )
                else:
                    logger.info(
                        f"Not a reply. Using default processing service ('{selected_processing_service.service_config.id}')."
                    )

                trigger_interface_message_id: str | None = None
                history_user_content = combined_text.strip()
                if first_photo_bytes:
                    history_user_content += " [Image(s) Attached]"

                user_message_id = (
                    last_update.message.message_id if last_update.message else None
                )
                user_message_timestamp = (
                    last_update.message.date
                    if last_update.message
                    else datetime.now(timezone.utc)
                )

                if user_message_id:
                    trigger_interface_message_id = str(user_message_id)
                    await db_context.message_history.add(
                        interface_type=interface_type,
                        conversation_id=conversation_id,
                        interface_message_id=str(user_message_id),
                        turn_id=None,
                        thread_root_id=thread_root_id_for_turn,
                        timestamp=user_message_timestamp,
                        role="user",
                        content=history_user_content,
                        tool_calls=None,
                        reasoning_info=None,
                        error_traceback=None,
                        tool_call_id=None,
                    )
                else:
                    logger.warning(
                        f"Could not get user message ID for chat {chat_id} to save to history."
                    )

                async with self._typing_notifications(context, chat_id):
                    async def confirmation_callback_wrapper(
                        interface_type: str,
                        conversation_id: str,
                        turn_id: str | None,
                        tool_name: str,
                        call_id: str,
                        tool_args: dict[str, Any],
                        timeout_seconds: float,
                    ) -> bool:
                        logger.debug("confirmation_callback_wrapper called!")
                        renderer = TOOL_CONFIRMATION_RENDERERS.get(tool_name)
                        if renderer:
                            prompt_text = renderer(tool_args)
                        else:
                            prompt_text = f"Confirm execution of tool: {tool_name}"

                        result = await self.confirmation_manager.request_confirmation(
                            conversation_id=conversation_id,
                            interface_type=interface_type,
                            turn_id=turn_id,
                            prompt_text=prompt_text,
                            tool_name=tool_name,
                            tool_args=tool_args,
                            timeout=timeout_seconds,
                        )
                        return result

                    chat_interfaces = self._get_chat_interfaces()

                    result = await selected_processing_service.handle_chat_interaction(
                        db_context=db_context,
                        interface_type=interface_type,
                        conversation_id=conversation_id,
                        trigger_content_parts=trigger_content_parts,
                        trigger_interface_message_id=trigger_interface_message_id,
                        user_name=user_name,
                        replied_to_interface_id=replied_to_interface_id,
                        chat_interface=self.telegram_service.chat_interface,
                        chat_interfaces=chat_interfaces,
                        request_confirmation_callback=confirmation_callback_wrapper,
                        trigger_attachments=trigger_attachments,
                    )

                    final_llm_content_to_send = result.text_reply
                    last_assistant_internal_id = result.assistant_message_internal_id
                    _final_reasoning_info = result.reasoning_info
                    processing_error_traceback = result.error_traceback
                    response_attachment_ids = result.attachment_ids

                force_reply_markup = ForceReply(selective=False)

                if final_llm_content_to_send:
                    try:
                        converted_markdown = telegramify_markdown.markdownify(
                            final_llm_content_to_send
                        )
                        sent_assistant_message = await self._send_message_chunks(
                            context=context,
                            chat_id=chat_id,
                            text=converted_markdown,
                            parse_mode=ParseMode.MARKDOWN_V2,
                            reply_to_message_id=reply_target_message_id,
                            reply_markup=force_reply_markup,
                        )
                    except Exception as md_err:
                        logger.error(
                            f"Failed to convert markdown: {md_err}. Sending plain text.",
                            exc_info=True,
                        )
                        sent_assistant_message = await self._send_message_chunks(
                            context=context,
                            chat_id=chat_id,
                            text=final_llm_content_to_send,
                            parse_mode=None,
                            reply_to_message_id=reply_target_message_id,
                            reply_markup=force_reply_markup,
                        )

                    if (
                        sent_assistant_message
                        and last_assistant_internal_id is not None
                    ):
                        try:
                            await db_context.message_history.update_interface_id(
                                internal_id=last_assistant_internal_id,
                                interface_message_id=str(
                                    sent_assistant_message.message_id
                                ),
                            )
                            logger.info(
                                f"Updated interface_message_id for internal_id {last_assistant_internal_id} to {sent_assistant_message.message_id}"
                            )
                        except Exception as update_err:
                            logger.error(
                                f"Failed to update interface_message_id for internal_id {last_assistant_internal_id}: {update_err}",
                                exc_info=True,
                            )
                    elif sent_assistant_message:
                        logger.warning(
                            f"Sent assistant message {sent_assistant_message.message_id} but couldn't find its internal_id ({last_assistant_internal_id}) to update."
                        )

                    if response_attachment_ids:
                        try:
                            await (
                                self.telegram_service.chat_interface._send_attachments(
                                    chat_id=chat_id,
                                    attachment_ids=response_attachment_ids,
                                    reply_to_msg_id=reply_target_message_id,
                                )
                            )
                        except Exception as attachment_err:
                            logger.error(
                                f"Failed to send attachments {response_attachment_ids}: {attachment_err}",
                                exc_info=True,
                            )
                elif processing_error_traceback and reply_target_message_id:
                    error_message_to_send = (
                        "Sorry, something went wrong while processing your request."
                    )
                    if self.debug_mode:
                        logger.info(f"Sending DEBUG error traceback to chat {chat_id}")
                        error_message_to_send = (
                            "Encountered error during processing \\(debug mode\\):\n"
                            f"<pre>{html.escape(processing_error_traceback)}</pre>"
                        )
                    else:
                        logger.info(f"Sending generic error message to chat {chat_id}")

                    await self._send_message_chunks(
                        context=context,
                        chat_id=chat_id,
                        text=error_message_to_send,
                        parse_mode=(ParseMode.HTML if self.debug_mode else None),
                        reply_to_message_id=reply_target_message_id,
                        reply_markup=force_reply_markup,
                    )
                else:
                    logger.warning(
                        "Received empty response from LLM (and no processing error detected)."
                    )
                    if reply_target_message_id:
                        await self._send_message_chunks(
                            context=context,
                            chat_id=chat_id,
                            text="Sorry, I couldn't process that request.",
                            parse_mode=None,
                            reply_to_message_id=reply_target_message_id,
                            reply_markup=force_reply_markup,
                        )

        except Exception as e:
            logger.exception(
                f"Unhandled error in process_chat_queue for chat {chat_id}: {e}",
                exc_info=True,
            )
            if not processing_error_traceback:
                processing_error_traceback = traceback.format_exc()
            if reply_target_message_id:
                with contextlib.suppress(
                    Exception
                ):
                    error_text_to_send_unhandled = (
                        f"An unexpected error occurred \\(debug mode\\):\n<pre>{html.escape(processing_error_traceback)}</pre>"
                        if self.debug_mode and processing_error_traceback
                        else "Sorry, an unexpected error occurred."
                    )
                    await self._send_message_chunks(
                        context=context,
                        chat_id=chat_id,
                        text=error_text_to_send_unhandled,
                        parse_mode=(
                            ParseMode.HTML
                            if self.debug_mode and processing_error_traceback
                            else None
                        ),
                        reply_to_message_id=reply_target_message_id,
                    )
                    logger.info(
                        f"Sent {'debug' if self.debug_mode else 'generic'} unexpected error message to chat {chat_id} via _send_message_chunks"
                    )

            if processing_error_traceback and user_message_id:
                try:
                    async with self.get_db_context() as db_ctx_err:
                        user_msg_record = (
                            await db_ctx_err.message_history.get_by_interface_id(
                                interface_type=interface_type,
                                interface_message_id=str(user_message_id),
                            )
                        )
                        if user_msg_record and user_msg_record.get("internal_id"):
                            stmt = (
                                sqlalchemy_update(message_history_table)
                                .where(
                                    message_history_table.c.internal_id
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

            raise e

    def _serialize_update_for_error_log(
        self, update_obj: object
    ) -> str | dict[str, Any]:
        """
        Serializes the update object for error logging.
        Returns a dict if it's an Update instance, otherwise a string.
        """
        if isinstance(update_obj, Update):
            return update_obj.to_dict()
        return str(update_obj)

    async def error_handler(self, update: object, context: CallbackContext) -> None:
        """Log the error, store it in the service, and notify the developer."""
        error = context.error
        logger.error(f"Exception while handling an update: {error}", exc_info=error)

        if self.telegram_service:
            self.telegram_service._last_error = error
            if isinstance(error, Conflict):
                logger.critical(
                    f"Telegram Conflict error detected: {error}. Polling will likely stop."
                )

        if error:
            tb_list = traceback.format_exception(None, error, error.__traceback__)
            tb_string = "".join(tb_list)
        else:
            tb_string = "No exception context available."

        update_repr = self._serialize_update_for_error_log(update)
        logger.debug(
            f"Error details for update {update_repr}: {tb_string}"
        )
        logger.warning("Error notification to developer has been removed.")

    async def handle_unknown_command(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        """Handles unrecognized commands."""
        if not update.effective_user:
            logger.warning("Unknown command: Update has no effective_user.")
            return
        user_id = update.effective_user.id

        if not update.message:
            logger.warning("Unknown command: Update has no message.")
            return

        if self.allowed_user_ids and user_id not in self.allowed_user_ids:
            logger.warning(
                f"Unauthorized unknown command from chat_id {user_id}: {update.message.text}"
            )
            return

        logger.info(
            f"Received unknown command from user {user_id}: {update.message.text}"
        )
        await update.message.reply_text(
            "Sorry, I didn't recognize that command. Type /start to see what I can do."
        )

    def register_handlers(self) -> None:
        """Registers the necessary Telegram handlers with the application."""
        application = self.telegram_service.application

        application.add_handler(CommandHandler("start", self.start))

        if self.telegram_service.slash_command_to_profile_id_map:
            for command_str in self.telegram_service.slash_command_to_profile_id_map:
                command_name = command_str.lstrip("/")
                application.add_handler(
                    CommandHandler(command_name, self.handle_generic_slash_command)
                )
                logger.info(f"Registered CommandHandler for /{command_name}")

        application.add_handler(
            MessageHandler(filters.COMMAND, self.handle_unknown_command)
        )
        logger.info("Registered MessageHandler for unknown commands.")

        application.add_handler(
            MessageHandler(
                (filters.TEXT | filters.PHOTO) & ~filters.COMMAND, self.message_handler
            )
        )

        application.add_error_handler(self.error_handler)
        logger.info(
            "Telegram handlers registered (start, generic commands, unknown commands, message, error)."
        )

    async def handle_generic_slash_command(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        """Handles generic slash commands mapped to processing profiles."""
        if not update.effective_user:
            logger.warning("Slash command: Update has no effective_user.")
            return
        user_id = update.effective_user.id

        if not update.effective_chat:
            logger.warning("Slash command: Update has no effective_chat.")
            return
        chat_id = update.effective_chat.id

        if not update.message or not update.message.text:
            logger.warning("Slash command: Update has no message or message text.")
            return

        if self.allowed_user_ids and user_id not in self.allowed_user_ids:
            logger.warning(f"Unauthorized slash command from user {user_id}")
            await update.message.reply_text(
                f"You're not authorized to use this command. User ID: `{user_id}`"
            )
            return

        message_text = update.message.text
        command_with_slash = message_text.split(maxsplit=1)[0]
        user_input_for_profile = " ".join(context.args or [])

        profile_id = self.telegram_service.slash_command_to_profile_id_map.get(
            command_with_slash
        )
        if not profile_id:
            logger.error(
                f"No profile_id found for command '{command_with_slash}'. This shouldn't happen if CommandHandler is correctly set up."
            )
            await update.message.reply_text(
                f"Error: Command '{command_with_slash}' is not configured correctly."
            )
            return

        targeted_processing_service = (
            self.telegram_service.processing_services_registry.get(profile_id)
        )
        if not targeted_processing_service:
            logger.error(
                f"ProcessingService for profile_id '{profile_id}' (command '{command_with_slash}') not found in registry."
            )
            await update.message.reply_text(
                f"Error: Service for command '{command_with_slash}' is unavailable."
            )
            return

        logger.info(
            f"Handling slash command '{command_with_slash}' for profile '{profile_id}'. User input: '{user_input_for_profile[:50]}...'"
        )

        photo_bytes = None
        if update.message.photo:
            logger.info(
                f"Slash command message {update.message.message_id} from chat {chat_id} contains photo."
            )
            try:
                photo_size = update.message.photo[-1]
                photo_file = await photo_size.get_file()
                with io.BytesIO() as buf:
                    await photo_file.download_to_memory(out=buf)
                    buf.seek(0)
                    photo_bytes = buf.read()
                logger.debug(
                    f"Photo from slash command message {update.message.message_id} loaded."
                )
            except Exception as img_err:
                logger.error(
                    f"Failed to process photo for slash command {update.message.message_id}: {img_err}",
                    exc_info=True,
                )
                await update.message.reply_text(
                    "Sorry, error processing attached image with command."
                )
                return

        text_part = {"type": "text", "text": user_input_for_profile}
        trigger_content_parts_for_profile: list[dict[str, Any]] = [text_part]
        if photo_bytes:
            try:
                base64_image = base64.b64encode(photo_bytes).decode("utf-8")
                mime_type = "image/jpeg"
                trigger_content_parts_for_profile.append({
                    "type": "image_url",
                    "image_url": {"url": f"data:{mime_type};base64,{base64_image}"},
                })
            except Exception as img_err_direct:
                logger.error(
                    f"Error encoding photo for slash command direct profile call: {img_err_direct}"
                )
                trigger_content_parts_for_profile = [text_part]

        reply_to_interface_id_str = (
            str(update.message.reply_to_message.message_id)
            if update.message.reply_to_message
            else None
        )

        async with self.get_db_context() as db_ctx:
            processing_error_traceback: str | None = None
            final_llm_content_to_send: str | None = None
            last_assistant_internal_id: int | None = None

            try:
                async def confirmation_callback_wrapper(
                    interface_type_cb: str,
                    conversation_id_cb: str,
                    turn_id_cb: str | None,
                    tool_name_cb: str,
                    call_id_cb: str,
                    tool_args_cb: dict[str, Any],
                    timeout_cb: float,
                ) -> bool:
                    renderer = TOOL_CONFIRMATION_RENDERERS.get(tool_name_cb)
                    if renderer:
                        prompt_text_cb = renderer(tool_args_cb)
                    else:
                        prompt_text_cb = f"Confirm execution of tool: {tool_name_cb}"

                    return await self.confirmation_manager.request_confirmation(
                        conversation_id=conversation_id_cb,
                        interface_type=interface_type_cb,
                        turn_id=turn_id_cb,
                        prompt_text=prompt_text_cb,
                        tool_name=tool_name_cb,
                        tool_args=tool_args_cb,
                        timeout=timeout_cb,
                    )

                chat_interfaces = self._get_chat_interfaces()

                async with self._typing_notifications(context, chat_id):
                    result = await targeted_processing_service.handle_chat_interaction(
                        db_context=db_ctx,
                        interface_type="telegram",
                        conversation_id=str(chat_id),
                        trigger_content_parts=trigger_content_parts_for_profile,
                        trigger_interface_message_id=str(update.message.message_id),
                        user_name=update.effective_user.full_name
                        if update.effective_user
                        else "Unknown User",
                        replied_to_interface_id=reply_to_interface_id_str,
                        chat_interface=self.telegram_service.chat_interface,
                        chat_interfaces=chat_interfaces,
                        request_confirmation_callback=confirmation_callback_wrapper,
                        trigger_attachments=None,
                    )

                    final_llm_content_to_send = result.text_reply
                    last_assistant_internal_id = result.assistant_message_internal_id
                    _final_reasoning_info = result.reasoning_info
                    processing_error_traceback = result.error_traceback
                    response_attachment_ids = result.attachment_ids

                force_reply_markup = ForceReply(selective=False)
                reply_target_message_id_for_bot = update.message.message_id

                if final_llm_content_to_send:
                    sent_assistant_message = None
                    try:
                        converted_markdown = telegramify_markdown.markdownify(
                            final_llm_content_to_send
                        )
                        sent_assistant_message = await self._send_message_chunks(
                            context=context,
                            chat_id=chat_id,
                            text=converted_markdown,
                            parse_mode=ParseMode.MARKDOWN_V2,
                            reply_to_message_id=reply_target_message_id_for_bot,
                            reply_markup=force_reply_markup,
                        )
                    except Exception as md_err:
                        logger.error(
                            f"Failed to convert markdown for slash command response: {md_err}. Sending plain text.",
                            exc_info=True,
                        )
                        sent_assistant_message = await self._send_message_chunks(
                            context=context,
                            chat_id=chat_id,
                            text=final_llm_content_to_send,
                            parse_mode=None,
                            reply_to_message_id=reply_target_message_id_for_bot,
                            reply_markup=force_reply_markup,
                        )

                    if (
                        sent_assistant_message
                        and last_assistant_internal_id is not None
                    ):
                        await db_ctx.message_history.update_interface_id(
                            internal_id=last_assistant_internal_id,
                            interface_message_id=str(sent_assistant_message.message_id),
                        )
                        logger.info(
                            f"Updated interface_message_id for internal_id {last_assistant_internal_id} to {sent_assistant_message.message_id} (slash command)"
                        )
                    elif sent_assistant_message:
                        logger.warning(
                            f"Sent assistant message {sent_assistant_message.message_id} (slash command) but couldn't find its internal_id ({last_assistant_internal_id}) to update."
                        )

                    if response_attachment_ids:
                        try:
                            await (
                                self.telegram_service.chat_interface._send_attachments(
                                    chat_id=chat_id,
                                    attachment_ids=response_attachment_ids,
                                    reply_to_msg_id=reply_target_message_id_for_bot,
                                )
                            )
                        except Exception as attachment_err:
                            logger.error(
                                f"Failed to send attachments {response_attachment_ids}: {attachment_err}",
                                exc_info=True,
                            )
                elif processing_error_traceback:
                    error_message_to_send = (
                        "Sorry, something went wrong while processing your command."
                    )
                    if self.debug_mode:
                        error_message_to_send = (
                            "Encountered error during slash command processing (debug mode):\n"
                            f"<pre>{html.escape(processing_error_traceback)}</pre>"
                        )
                    await self._send_message_chunks(
                        context=context,
                        chat_id=chat_id,
                        text=error_message_to_send,
                        parse_mode=(ParseMode.HTML if self.debug_mode else None),
                        reply_to_message_id=reply_target_message_id_for_bot,
                        reply_markup=force_reply_markup,
                    )
                else:
                    logger.warning(
                        "Slash command resulted in empty response and no processing error."
                    )
                    await self._send_message_chunks(
                        context=context,
                        chat_id=chat_id,
                        text="Sorry, I couldn't process that command.",
                        parse_mode=None,
                        reply_to_message_id=reply_target_message_id_for_bot,
                        reply_markup=force_reply_markup,
                    )
            except Exception as e:
                logger.exception(
                    f"Unhandled error in handle_generic_slash_command for chat {chat_id}: {e}",
                    exc_info=True,
                )
                if not processing_error_traceback:
                    processing_error_traceback = traceback.format_exc()

                with contextlib.suppress(Exception):
                    error_text_to_send_unhandled_cmd = (
                        f"An unexpected error occurred with your command (debug mode):\n<pre>{html.escape(processing_error_traceback)}</pre>"
                        if self.debug_mode and processing_error_traceback
                        else "Sorry, an unexpected error occurred with your command."
                    )
                    await self._send_message_chunks(
                        context=context,
                        chat_id=chat_id,
                        text=error_text_to_send_unhandled_cmd,
                        parse_mode=(
                            ParseMode.HTML
                            if self.debug_mode and processing_error_traceback
                            else None
                        ),
                        reply_to_message_id=update.message.message_id,
                    )
                if (
                    processing_error_traceback
                    and update.message
                    and update.message.message_id
                ):
                    try:
                        user_msg_record = (
                            await db_ctx.message_history.get_by_interface_id(
                                interface_type="telegram",
                                interface_message_id=str(update.message.message_id),
                            )
                        )
                        if user_msg_record and user_msg_record.get("internal_id"):
                            stmt = (
                                sqlalchemy_update(message_history_table)
                                .where(
                                    message_history_table.c.internal_id
                                    == user_msg_record["internal_id"]
                                )
                                .values(error_traceback=processing_error_traceback)
                            )
                            await db_ctx.execute_with_retry(stmt)
                            logger.info(
                                f"Saved error traceback to user message (slash command) internal_id {user_msg_record['internal_id']}"
                            )
                    except Exception as db_err_save:
                        logger.error(
                            f"Failed to save error traceback to DB for slash command in chat {chat_id}: {db_err_save}",
                            exc_info=True,
                        )
                raise

    async def message_handler(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
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