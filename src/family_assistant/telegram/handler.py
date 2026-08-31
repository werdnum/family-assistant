from __future__ import annotations

import asyncio
import base64
import contextlib
import html
import io
import logging
import os
import traceback
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, cast

from opentelemetry import trace
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
from telegram.error import BadRequest, Conflict
from telegram.ext import (
    CallbackContext,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from family_assistant.llm.messages import (
    ContentPartDict,
    image_url_content,
    text_content,
)
from family_assistant.processing import ProcessingService
from family_assistant.processing.types import MidTurnUserInput
from family_assistant.services.user_identity import (
    ResolvedUserIdentity,
    UserIdentityResolutionError,
    UserIdentityResolver,
)
from family_assistant.storage.message_history import (
    message_history_table,  # For error handling db update
)
from family_assistant.telegram.chunking import (
    CHUNK_SEND_DELAY_SECONDS,
    TELEGRAM_MAX_MESSAGE_LENGTH,
    split_message_text,
)
from family_assistant.telegram.markdown_utils import convert_to_telegram_markdown
from family_assistant.telegram.rich_messages import (
    is_rich_message_compatibility_error,
    send_rich_message,
    should_attempt_rich_message,
)
from family_assistant.telegram.types import AttachmentData, TriggerAttachment
from family_assistant.tools.confirmation import (
    TOOL_CONFIRMATION_RENDERERS,
    append_review_reason_to_confirmation,
)

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    from family_assistant.interfaces import ChatInterface
    from family_assistant.storage.database import Database
    from family_assistant.telegram.protocols import (
        ConfirmationUIManager,
        MessageBatcher,
    )
    from family_assistant.telegram.service import TelegramService
    from family_assistant.telegram.ui import TelegramConfirmationUIManager
    from family_assistant.tools.types import ConfirmationOutcome, ToolExecutionContext

logger = logging.getLogger(__name__)
tracer = trace.get_tracer(__name__)


@dataclass
class _QueuedMidTurnUpdate:
    update: Update
    attachments: list[AttachmentData] | None
    user_input: MidTurnUserInput
    user_input_consumed: bool = False

    @property
    def needs_follow_up_batch_processing(self) -> bool:
        return bool(self.attachments)


class TelegramMidTurnController:
    """Tracks live user updates for one active Telegram turn."""

    def __init__(self) -> None:
        self._lock = asyncio.Lock()
        self._queued_updates: list[_QueuedMidTurnUpdate] = []
        self._interrupted = False

    def request_interrupt(self) -> None:
        self._interrupted = True

    def should_interrupt(self) -> bool:
        return self._interrupted

    async def add_update(
        self,
        update: Update,
        attachments: list[AttachmentData] | None,
        user_input: MidTurnUserInput,
    ) -> None:
        async with self._lock:
            self._queued_updates.append(
                _QueuedMidTurnUpdate(
                    update=update,
                    attachments=attachments,
                    user_input=user_input,
                )
            )

    async def drain_pending_mid_turn_inputs(self) -> list[MidTurnUserInput]:
        async with self._lock:
            pending = [
                item for item in self._queued_updates if not item.user_input_consumed
            ]
            for item in pending:
                item.user_input_consumed = True
            return [item.user_input for item in pending]

    async def pop_unconsumed_batch(
        self,
    ) -> list[tuple[Update, list[AttachmentData] | None]]:
        async with self._lock:
            pending = [
                item
                for item in self._queued_updates
                if not item.user_input_consumed or item.needs_follow_up_batch_processing
            ]
            self._queued_updates = []
            return [(item.update, item.attachments) for item in pending]


class TelegramUpdateHandler:  # Renamed from TelegramBotHandler
    """Handles specific Telegram updates (messages, commands) and delegates processing."""

    def __init__(
        self,
        telegram_service: TelegramService,  # Accept the service instance
        user_identity_resolver: UserIdentityResolver,
        processing_service: ProcessingService,  # Use string quote for forward reference
        database: Database,
        message_batcher: MessageBatcher
        | None,  # Inject the batcher, can be None initially
        confirmation_manager: TelegramConfirmationUIManager,  # Inject confirmation manager
    ) -> None:
        """Initializes the TelegramUpdateHandler.

        Args:
            telegram_service: The parent TelegramService instance.
            user_identity_resolver: Resolver for Telegram user authorization.
            processing_service: The processing service for handling interactions.
            database: Handle for this deployment's database.
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
        self.user_identity_resolver = user_identity_resolver
        self.processing_service = processing_service  # Store the service instance
        self.database = database
        self.message_batcher = message_batcher  # Store the injected batcher
        self.confirmation_manager: TelegramConfirmationUIManager = (
            confirmation_manager  # Store the injected manager
        )
        self._active_mid_turns: dict[int, TelegramMidTurnController] = {}
        self._active_processing_tasks: dict[int, asyncio.Task[None]] = {}

    def _resolve_telegram_user(
        self, telegram_user_id: int
    ) -> ResolvedUserIdentity | None:
        try:
            return self.user_identity_resolver.resolve_telegram_user(telegram_user_id)
        except UserIdentityResolutionError as exc:
            logger.warning("Unauthorized Telegram user %s: %s", telegram_user_id, exc)
            return None

    async def _cancel_pending_media_group_if_any(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        """Roll back the matching ``notify_pending_media_group`` call.

        Used by the message handler's exception paths to keep the batcher's
        outstanding-download counter accurate when attachment processing
        aborts before reaching ``add_to_batch``.
        """
        if (
            self.message_batcher is None
            or update.effective_chat is None
            or update.message is None
            or update.message.media_group_id is None
        ):
            return
        await self.message_batcher.cancel_pending_media_group(
            update.effective_chat.id, update.message.media_group_id, context
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

    def _get_confirmation_ui_managers(
        self,
    ) -> dict[str, ConfirmationUIManager] | None:
        """Get confirmation UI manager registry from FastAPI app state."""
        if self.telegram_service.fastapi_app:
            return getattr(
                self.telegram_service.fastapi_app.state,
                "confirmation_ui_managers",
                None,
            )
        return None

    def _build_mid_turn_user_input(
        self,
        update: Update,
        attachments: list[AttachmentData] | None,
        user_name: str,
    ) -> MidTurnUserInput | None:
        """Convert a live Telegram update into steering text for the active turn."""
        if update.message is None:
            return None

        text = update.message.caption or update.message.text or ""
        text = text.strip()
        attachment_descriptions = [
            attachment.filename for attachment in attachments or []
        ]
        if attachment_descriptions:
            attachment_text = "Attachments included in this live update: " + ", ".join(
                attachment_descriptions
            )
            text = f"{text}\n\n{attachment_text}".strip()

        if not text:
            return None

        return MidTurnUserInput(
            content=text,
            interface_message_id=str(update.message.message_id),
            user_name=user_name,
        )

    async def _route_mid_turn_update_if_active(
        self,
        update: Update,
        context: ContextTypes.DEFAULT_TYPE,
        chat_id: int,
        user_name: str,
        attachments: list[AttachmentData] | None,
    ) -> bool:
        """Queue an update as active-turn guidance when a Telegram turn is running."""
        controller = self._active_mid_turns.get(chat_id)
        if controller is None:
            return False

        user_input = self._build_mid_turn_user_input(update, attachments, user_name)
        if user_input is None:
            return False

        await controller.add_update(update, attachments, user_input)
        if update.message is not None:
            await context.bot.send_message(
                chat_id=chat_id,
                text="Got it. I'll apply that to the current response.",
                reply_to_message_id=update.message.message_id,
            )
        return True

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
        chunks = split_message_text(text)
        if not chunks:  # Do not send empty messages
            logger.warning(
                f"Attempted to send empty message to chat {chat_id}. Aborting."
            )
            return None
        if len(chunks) > 1:
            logger.info(
                f"Message to chat {chat_id} exceeds {TELEGRAM_MAX_MESSAGE_LENGTH} chars. "
                f"Sending as {len(chunks)} messages."
            )

        first_sent_message: Message | None = None
        for i, chunk_text in enumerate(chunks):
            sent_msg = await context.bot.send_message(
                chat_id=chat_id,
                text=chunk_text,
                parse_mode=parse_mode,
                reply_to_message_id=reply_to_message_id if i == 0 else None,
                reply_markup=reply_markup if i == 0 else None,
            )
            if i == 0:
                first_sent_message = sent_msg
            if i < len(chunks) - 1:
                await asyncio.sleep(CHUNK_SEND_DELAY_SECONDS)
        return first_sent_message

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
                except Exception as e:
                    # Typing indicators are non-critical UX niceties - don't fail the message flow
                    # This also handles telegram-test-api which doesn't support sendChatAction
                    logger.debug(f"Could not send chat action (non-critical): {e}")
                with contextlib.suppress(TimeoutError):
                    await asyncio.wait_for(stop_event.wait(), timeout=4.5)

        typing_task = asyncio.create_task(typing_loop())
        try:
            yield
        finally:
            stop_event.set()
            with contextlib.suppress(TimeoutError):
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

        if self._resolve_telegram_user(user_id) is None:
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
        batch: list[tuple[Update, list[AttachmentData] | None]],
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
        resolved_user = (
            self._resolve_telegram_user(user.id) if user is not None else None
        )
        if resolved_user is None:
            logger.warning("Ignoring batch from unauthorized Telegram user")
            return

        with tracer.start_as_current_span(
            "telegram.process_batch",
            attributes={
                "telegram.chat_id": str(chat_id),
                "telegram.batch_size": len(batch),
                "conversation.interface": "telegram",
                "conversation.user": user_name,
            },
        ):
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
            all_attachments: list[AttachmentData] = []
            forward_context = ""

            for update_item, attachments in batch:
                if update_item.message:
                    text = update_item.message.caption or update_item.message.text or ""
                    if text:
                        all_texts.append(text)

                    if attachments:
                        all_attachments.extend(attachments)
                        logger.debug(
                            f"Found {len(attachments)} attachment(s) in batch from message {update_item.message.message_id}"
                        )

                    if update_item.message.forward_origin:
                        origin = update_item.message.forward_origin
                        original_sender_name = "Unknown Sender"
                        if isinstance(origin, MessageOriginUser):
                            original_sender_name = (
                                origin.sender_user.first_name or "User"
                            )
                        elif isinstance(origin, MessageOriginHiddenUser):
                            original_sender_name = (
                                origin.sender_user_name or "Hidden User"
                            )
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
            trigger_content_parts: list[ContentPartDict] = [
                text_content(formatted_user_text_content)
            ]

            # Initialize trigger_attachments as None, will convert to list if successful attachments exist
            trigger_attachments: list[TriggerAttachment] | None = None

            if all_attachments:
                valid_attachments: list[TriggerAttachment] = []

                # Register user attachments with database record for cross-turn access
                db_context = self.database
                user_id_str = resolved_user.user_id

                for attachment in all_attachments:
                    try:
                        attachment_metadata = await self.telegram_service.attachment_registry.register_user_attachment(
                            db_context=db_context,
                            content=attachment.content,
                            filename=attachment.filename,
                            mime_type=attachment.mime_type,
                            conversation_id=str(chat_id),
                            # Don't pass message_id here - the message_history entry
                            # doesn't exist yet and we use the internal DB ID, not
                            # the Telegram message ID
                            user_id=user_id_str,
                            description=attachment.description
                            or f"Telegram attachment from {user_name}",
                        )

                        # Pass images, videos, audio, and PDFs to LLM for processing
                        # Gemini supports all of these; other providers may gracefully
                        # handle or ignore unsupported types
                        mime_type = attachment_metadata.mime_type
                        if attachment_metadata.content_url and (
                            mime_type.startswith("image/")
                            or mime_type.startswith("video/")
                            or mime_type.startswith("audio/")
                            or mime_type == "application/pdf"
                        ):
                            trigger_content_parts.append(
                                image_url_content(attachment_metadata.content_url)
                            )

                        # Classify attachment type for metadata
                        if mime_type.startswith("image/"):
                            attachment_type = "image"
                        elif mime_type.startswith("video/"):
                            attachment_type = "video"
                        elif mime_type.startswith("audio/"):
                            attachment_type = "audio"
                        else:
                            attachment_type = "file"

                        attachment_dict = {
                            "type": attachment_type,
                            "content_url": attachment_metadata.content_url,
                            "name": attachment_metadata.description,
                            "size": attachment_metadata.size,
                            "content_type": attachment_metadata.mime_type,
                            "attachment_id": attachment_metadata.attachment_id,
                        }

                        # Explicitly cast to satisfy basedpyright strict type checking
                        # because TypedDict assignment from dict literal with optional/union types
                        # can sometimes be inferred too broadly.
                        valid_attachments.append(
                            cast("TriggerAttachment", attachment_dict)
                        )

                        # Use metadata dictionary to access original_filename safely if needed
                        # Or access the attribute we know AttachmentMetadata has
                        filename_log = attachment_metadata.metadata.get(
                            "original_filename", "unknown_filename"
                        )
                        logger.info(
                            f"Stored Telegram attachment: {attachment_metadata.attachment_id} ({filename_log})"
                        )
                    except Exception as attach_err:
                        logger.exception(
                            f"Error storing individual attachment '{attachment.filename}' from batch: {attach_err}"
                        )
                        # Continue to process other attachments
                        continue

                if valid_attachments:
                    trigger_attachments = valid_attachments
                elif all_attachments:
                    # All failed
                    await context.bot.send_message(
                        chat_id,
                        "Error: Could not process any of the attached files.",
                    )

            sent_assistant_message: Message | None = None
            processing_error_traceback: str | None = None
            pending_mid_turn_batch: list[
                tuple[Update, list[AttachmentData] | None]
            ] = []
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
                        chat_id,
                        "Internal error: Default processing service unavailable.",
                    )
                    return

                db_context = self.database
                thread_root_id_for_turn: int | None = None
                replied_to_db_msg = None

                if replied_to_interface_id:
                    try:
                        replied_to_db_msg = (
                            await db_context.message_history.get_row_by_interface_id(
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
                                if isinstance(
                                    profile_specific_service, ProcessingService
                                ):
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
                        logger.exception(
                            f"Error determining thread root ID or profile from reply: {thread_err}"
                        )
                else:
                    logger.info(
                        f"Not a reply. Using default processing service ('{selected_processing_service.service_config.id}')."
                    )

                trigger_interface_message_id: str | None = None

                user_message_id = (
                    last_update.message.message_id if last_update.message else None
                )

                if user_message_id:
                    trigger_interface_message_id = str(user_message_id)
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
                        # ast-grep-ignore: no-dict-any - tool args have varying keys per tool
                        tool_args: dict[str, Any],
                        timeout_seconds: float,
                        context: ToolExecutionContext,
                    ) -> ConfirmationOutcome:
                        logger.debug("confirmation_callback_wrapper called!")
                        # Allow custom renderers to override the prompt_text if available
                        renderer = TOOL_CONFIRMATION_RENDERERS.get(tool_name)
                        if renderer:
                            # Async renderer that fetches its own data from context
                            prompt_text = await renderer(tool_args, context)
                        else:
                            prompt_text = f"Confirm execution of tool: {tool_name}"
                        prompt_text = append_review_reason_to_confirmation(
                            prompt_text, context
                        )

                        source_message_internal_id = None
                        if turn_id is not None:
                            source_row = await context.db_context.message_history.get_user_row_by_turn_id(
                                turn_id
                            )
                            if source_row is not None:
                                source_message_internal_id = source_row["internal_id"]

                        taint_state_json = (
                            context.taint_tracker.snapshot().to_metadata()
                            if context.taint_tracker is not None
                            else None
                        )
                        result = await self.confirmation_manager.request_confirmation(
                            conversation_id=conversation_id,
                            interface_type=interface_type,
                            turn_id=turn_id,
                            prompt_text=prompt_text,
                            tool_name=tool_name,
                            tool_args=tool_args,
                            timeout=timeout_seconds,
                            target_user_id=resolved_user.user_id,
                            tool_call_id=call_id,
                            source_message_internal_id=source_message_internal_id,
                            taint_state_json=taint_state_json,
                            processing_profile_id=context.processing_profile_id,
                            tool_call_review_authorization=(
                                context.tool_call_review_authorization
                            ),
                        )
                        return result

                    chat_interfaces = self._get_chat_interfaces()
                    confirmation_ui_managers = self._get_confirmation_ui_managers()

                    mid_turn_controller = TelegramMidTurnController()
                    self._active_mid_turns[chat_id] = mid_turn_controller
                    current_task = asyncio.current_task()
                    if current_task is not None:
                        self._active_processing_tasks[chat_id] = current_task
                    try:
                        result = await selected_processing_service.handle_chat_interaction(
                            db_context=db_context,
                            interface_type=interface_type,
                            conversation_id=conversation_id,
                            trigger_content_parts=trigger_content_parts,
                            trigger_interface_message_id=trigger_interface_message_id,
                            user_name=user_name,
                            user_id=resolved_user.user_id,
                            replied_to_interface_id=replied_to_interface_id,
                            chat_interface=self.telegram_service.chat_interface,
                            chat_interfaces=chat_interfaces,
                            confirmation_ui_managers=confirmation_ui_managers,
                            request_confirmation_callback=confirmation_callback_wrapper,
                            trigger_attachments=trigger_attachments,  # type: ignore
                            mid_turn_input_provider=mid_turn_controller,
                        )
                    except asyncio.CancelledError:
                        logger.info(
                            "Telegram processing turn for chat %s was interrupted.",
                            chat_id,
                        )
                        return
                    finally:
                        if self._active_mid_turns.get(chat_id) is mid_turn_controller:
                            self._active_mid_turns.pop(chat_id, None)
                        if (
                            current_task is not None
                            and self._active_processing_tasks.get(chat_id)
                            is current_task
                        ):
                            self._active_processing_tasks.pop(chat_id, None)
                        if not mid_turn_controller.should_interrupt():
                            pending_mid_turn_batch = (
                                await mid_turn_controller.pop_unconsumed_batch()
                            )

                    final_llm_content_to_send = result.text_reply
                    last_assistant_internal_id = result.assistant_message_internal_id
                    _final_reasoning_info = result.reasoning_info
                    processing_error_traceback = result.error_traceback
                    response_attachment_ids = result.attachment_ids

                force_reply_markup = ForceReply(selective=False)

                if final_llm_content_to_send:
                    sent_assistant_message = None
                    if should_attempt_rich_message(final_llm_content_to_send):
                        try:
                            sent_assistant_message = await send_rich_message(
                                bot=context.bot,
                                chat_id=chat_id,
                                text=final_llm_content_to_send,
                                reply_to_message_id=reply_target_message_id,
                                reply_markup=force_reply_markup,
                            )
                            logger.info(
                                "Sent assistant response as Telegram rich message to chat %s.",
                                chat_id,
                            )
                        except Exception as rich_err:
                            if not is_rich_message_compatibility_error(rich_err):
                                raise
                            logger.info(
                                "Telegram rejected rich message (%s); falling back to standard sendMessage.",
                                rich_err,
                            )

                    if sent_assistant_message is None:
                        # Convert to Telegram MarkdownV2 with bug fixes
                        text_to_send, parse_mode = convert_to_telegram_markdown(
                            final_llm_content_to_send
                        )

                        try:
                            sent_assistant_message = await self._send_message_chunks(
                                context=context,
                                chat_id=chat_id,
                                text=text_to_send,
                                parse_mode=ParseMode.MARKDOWN_V2
                                if parse_mode
                                else None,
                                reply_to_message_id=reply_target_message_id,
                                reply_markup=force_reply_markup,
                            )
                        except BadRequest as parse_err:
                            # Defense-in-depth: If Telegram still rejects due to parse errors, fall back to plain text
                            if "Can't parse entities" in str(parse_err) and parse_mode:
                                logger.warning(
                                    f"Telegram rejected MarkdownV2 message (parse error): {parse_err}. Falling back to plain text.",
                                    exc_info=False,
                                )
                                sent_assistant_message = (
                                    await self._send_message_chunks(
                                        context=context,
                                        chat_id=chat_id,
                                        text=final_llm_content_to_send,
                                        parse_mode=None,
                                        reply_to_message_id=reply_target_message_id,
                                        reply_markup=force_reply_markup,
                                    )
                                )
                            else:
                                raise

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
                            logger.exception(
                                f"Failed to update interface_message_id for internal_id {last_assistant_internal_id}: {update_err}"
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
                                    on_behalf_of_user_id=resolved_user.user_id,
                                )
                            )
                        except Exception as attachment_err:
                            logger.exception(
                                f"Failed to send attachments {response_attachment_ids}: {attachment_err}"
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

                if pending_mid_turn_batch:
                    logger.info(
                        "Processing %d undrained mid-turn Telegram update(s) as a follow-up batch for chat %s.",
                        len(pending_mid_turn_batch),
                        chat_id,
                    )
                    await self.process_batch(
                        chat_id=chat_id,
                        batch=pending_mid_turn_batch,
                        context=context,
                    )

            except Exception as e:
                logger.exception(
                    f"Unhandled error in process_chat_queue for chat {chat_id}: {e}"
                )
                if not processing_error_traceback:
                    processing_error_traceback = traceback.format_exc()
                if reply_target_message_id:
                    with contextlib.suppress(Exception):
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
                        db_ctx_err = self.database
                        user_msg_record = (
                            await db_ctx_err.message_history.get_row_by_interface_id(
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
                            await db_ctx_err.execute(stmt)
                            logger.info(
                                f"Saved error traceback to user message internal_id {user_msg_record['internal_id']}"
                            )
                        else:
                            logger.error(
                                "Could not find user message record to attach error traceback."
                            )
                    except Exception as db_err_save:
                        logger.exception(
                            f"Failed to save error traceback to DB for chat {chat_id}: {db_err_save}"
                        )

                raise e

    def _serialize_update_for_error_log(
        self,
        update_obj: object,
        # ast-grep-ignore: no-dict-any - Telegram Update serialized to dict with dynamic fields
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
        logger.debug(f"Error details for update {update_repr}: {tb_string}")
        logger.warning("Error notification to developer has been removed.")

    async def interrupt_command(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        """Interrupt the currently running Telegram turn for this chat."""
        if not update.effective_user:
            logger.warning("Interrupt command: Update has no effective_user.")
            return
        if not update.effective_chat:
            logger.warning("Interrupt command: Update has no effective_chat.")
            return

        user_id = update.effective_user.id
        chat_id = update.effective_chat.id
        if self._resolve_telegram_user(user_id) is None:
            logger.warning("Unauthorized /interrupt from user %s", user_id)
            return

        controller = self._active_mid_turns.get(chat_id)
        task = self._active_processing_tasks.get(chat_id)
        if controller is not None:
            controller.request_interrupt()
        if task is not None and not task.done():
            task.cancel()
            logger.info("Interrupted active Telegram turn for chat %s", chat_id)
            await context.bot.send_message(
                chat_id=chat_id,
                text="Interrupted the current request.",
                reply_to_message_id=update.message.message_id
                if update.message is not None
                else None,
            )
            return

        await context.bot.send_message(
            chat_id=chat_id,
            text="There isn't an active request to interrupt.",
            reply_to_message_id=update.message.message_id
            if update.message is not None
            else None,
        )

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

        if self._resolve_telegram_user(user_id) is None:
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
        application.add_handler(CommandHandler("interrupt", self.interrupt_command))

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
                (
                    filters.TEXT
                    | filters.PHOTO
                    | filters.Document.ALL
                    | filters.VIDEO
                    | filters.AUDIO
                    | filters.VOICE
                )
                & ~filters.COMMAND,
                self.message_handler,
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

        resolved_user = self._resolve_telegram_user(user_id)
        if resolved_user is None:
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
        if (
            targeted_processing_service is not None
            and targeted_processing_service.kind == "remote"
        ):
            logger.error(
                f"Profile '{profile_id}' is a remote profile, cannot use for slash commands."
            )
            await update.message.reply_text(
                f"Error: Profile '{profile_id}' is a remote delegation-only profile."
            )
            return
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
                logger.exception(
                    f"Failed to process photo for slash command {update.message.message_id}: {img_err}"
                )
                await update.message.reply_text(
                    "Sorry, error processing attached image with command."
                )
                return

        trigger_content_parts_for_profile: list[ContentPartDict] = [
            text_content(user_input_for_profile)
        ]
        if photo_bytes:
            try:
                base64_image = base64.b64encode(photo_bytes).decode("utf-8")
                mime_type = "image/jpeg"
                data_url = f"data:{mime_type};base64,{base64_image}"
                trigger_content_parts_for_profile.append(image_url_content(data_url))
            except Exception as img_err_direct:
                logger.error(
                    f"Error encoding photo for slash command direct profile call: {img_err_direct}"
                )
                trigger_content_parts_for_profile = [
                    text_content(user_input_for_profile)
                ]

        reply_to_interface_id_str = (
            str(update.message.reply_to_message.message_id)
            if update.message.reply_to_message
            else None
        )

        db_ctx = self.database
        processing_error_traceback: str | None = None
        final_llm_content_to_send: str | None = None
        last_assistant_internal_id: int | None = None

        try:

            async def confirmation_callback_wrapper(
                interface_type: str,
                conversation_id: str,
                turn_id: str | None,
                tool_name: str,
                call_id: str,
                # ast-grep-ignore: no-dict-any - tool args have varying keys per tool
                tool_args: dict[str, Any],
                timeout_seconds: float,
                context: ToolExecutionContext,
            ) -> ConfirmationOutcome:
                renderer = TOOL_CONFIRMATION_RENDERERS.get(tool_name)
                if renderer:
                    # Async renderer that fetches its own data from context
                    prompt_text = await renderer(tool_args, context)
                else:
                    prompt_text = f"Confirm execution of tool: {tool_name}"
                prompt_text = append_review_reason_to_confirmation(prompt_text, context)

                source_message_internal_id = None
                if turn_id is not None:
                    source_row = await context.db_context.message_history.get_user_row_by_turn_id(
                        turn_id
                    )
                    if source_row is not None:
                        source_message_internal_id = source_row["internal_id"]

                taint_state_json = (
                    context.taint_tracker.snapshot().to_metadata()
                    if context.taint_tracker is not None
                    else None
                )

                return await self.confirmation_manager.request_confirmation(
                    conversation_id=conversation_id,
                    interface_type=interface_type,
                    turn_id=turn_id,
                    prompt_text=prompt_text,
                    tool_name=tool_name,
                    tool_args=tool_args,
                    timeout=timeout_seconds,
                    target_user_id=resolved_user.user_id,
                    tool_call_id=call_id,
                    source_message_internal_id=source_message_internal_id,
                    taint_state_json=taint_state_json,
                    processing_profile_id=context.processing_profile_id,
                    tool_call_review_authorization=(
                        context.tool_call_review_authorization
                    ),
                )

            chat_interfaces = self._get_chat_interfaces()
            confirmation_ui_managers = self._get_confirmation_ui_managers()

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
                    user_id=resolved_user.user_id,
                    replied_to_interface_id=reply_to_interface_id_str,
                    chat_interface=self.telegram_service.chat_interface,
                    chat_interfaces=chat_interfaces,
                    confirmation_ui_managers=confirmation_ui_managers,
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
                if should_attempt_rich_message(final_llm_content_to_send):
                    try:
                        sent_assistant_message = await send_rich_message(
                            bot=context.bot,
                            chat_id=chat_id,
                            text=final_llm_content_to_send,
                            reply_to_message_id=reply_target_message_id_for_bot,
                            reply_markup=force_reply_markup,
                        )
                        logger.info(
                            "Sent slash command response as Telegram rich message to chat %s.",
                            chat_id,
                        )
                    except Exception as rich_err:
                        if not is_rich_message_compatibility_error(rich_err):
                            raise
                        logger.info(
                            "Telegram rejected slash command rich message (%s); falling back to standard sendMessage.",
                            rich_err,
                        )

                if sent_assistant_message is None:
                    # Convert to Telegram MarkdownV2 with bug fixes
                    text_to_send, parse_mode = convert_to_telegram_markdown(
                        final_llm_content_to_send
                    )

                    try:
                        sent_assistant_message = await self._send_message_chunks(
                            context=context,
                            chat_id=chat_id,
                            text=text_to_send,
                            parse_mode=ParseMode.MARKDOWN_V2 if parse_mode else None,
                            reply_to_message_id=reply_target_message_id_for_bot,
                            reply_markup=force_reply_markup,
                        )
                    except BadRequest as parse_err:
                        # Defense-in-depth: If Telegram still rejects due to parse errors, fall back to plain text
                        if "Can't parse entities" in str(parse_err) and parse_mode:
                            logger.warning(
                                f"Telegram rejected MarkdownV2 message (parse error): {parse_err}. Falling back to plain text.",
                                exc_info=False,
                            )
                            sent_assistant_message = await self._send_message_chunks(
                                context=context,
                                chat_id=chat_id,
                                text=final_llm_content_to_send,
                                parse_mode=None,
                                reply_to_message_id=reply_target_message_id_for_bot,
                                reply_markup=force_reply_markup,
                            )
                        else:
                            raise

                if sent_assistant_message and last_assistant_internal_id is not None:
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
                        await self.telegram_service.chat_interface._send_attachments(
                            chat_id=chat_id,
                            attachment_ids=response_attachment_ids,
                            reply_to_msg_id=reply_target_message_id_for_bot,
                            on_behalf_of_user_id=resolved_user.user_id,
                        )
                    except Exception as attachment_err:
                        logger.exception(
                            f"Failed to send attachments {response_attachment_ids}: {attachment_err}"
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
                f"Unhandled error in handle_generic_slash_command for chat {chat_id}: {e}"
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
                        await db_ctx.message_history.get_row_by_interface_id(
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
                        await db_ctx.execute(stmt)
                        logger.info(
                            f"Saved error traceback to user message (slash command) internal_id {user_msg_record['internal_id']}"
                        )
                except Exception as db_err_save:
                    logger.exception(
                        f"Failed to save error traceback to DB for slash command in chat {chat_id}: {db_err_save}"
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

        attachments: list[AttachmentData] = []
        registry = self.telegram_service.attachment_registry
        # Photos, audio, voice notes and video only ever reach a model as media,
        # so they are held to the tighter multimodal bound. Checking it here means
        # an oversized recording is refused with its size instead of being
        # downloaded and then rejected at registration. Documents take their limit
        # from their own MIME type further down, since media can arrive as a file.
        max_media_size = registry.media_size_limit

        if self._resolve_telegram_user(user_id) is None:
            logger.warning(f"Ignoring message from unauthorized user {user_id}")
            return

        if (
            update.message.media_group_id is not None
            and self.message_batcher is not None
        ):
            await self.message_batcher.notify_pending_media_group(
                chat_id, update.message.media_group_id, context
            )

        try:
            # Handle Photos
            if update.message.photo:
                logger.info(
                    f"Message {update.message.message_id} from chat {chat_id} contains photo."
                )
                photo_size = update.message.photo[-1]
                # Photos in Telegram usually don't have file_size in the Update object immediately available
                # or it's reliable. However, get_file() will return an object with file_size.
                # But to avoid network call if possible, we check photo_size.file_size
                if photo_size.file_size and photo_size.file_size > max_media_size:
                    logger.warning(
                        f"Photo size {photo_size.file_size} exceeds limit {max_media_size}. Skipping."
                    )
                    await update.message.reply_text(
                        f"Skipping photo: File size exceeds the {max_media_size // 1024 // 1024}MB limit."
                    )
                else:
                    photo_file = await photo_size.get_file()
                    # Double check size from get_file() result
                    if photo_file.file_size and photo_file.file_size > max_media_size:
                        logger.warning(
                            f"Photo size {photo_file.file_size} exceeds limit {max_media_size}. Skipping."
                        )
                        await update.message.reply_text(
                            f"Skipping photo: File size exceeds the {max_media_size // 1024 // 1024}MB limit."
                        )
                    else:
                        with io.BytesIO() as buf:
                            await photo_file.download_to_memory(out=buf)
                            buf.seek(0)
                            photo_bytes = buf.read()
                            attachments.append(
                                AttachmentData(
                                    content=photo_bytes,
                                    filename=f"photo_{photo_size.file_id}.jpg",
                                    mime_type="image/jpeg",
                                    description=update.message.caption
                                    or "Telegram Photo",
                                )
                            )
                        logger.debug(
                            f"Photo from message {update.message.message_id} loaded into bytes."
                        )

            # Handle Documents (Files)
            if update.message.document:
                doc = update.message.document
                logger.info(
                    f"Message {update.message.message_id} from chat {chat_id} contains document: {doc.file_name} ({doc.file_size} bytes)."
                )

                # A video or recording sent as a file lands here rather than in the
                # media branches, so the limit follows the document's MIME type
                # instead of the branch it arrived in.
                doc_size_limit = registry.size_limit_for_mime(doc.mime_type)
                if doc.file_size and doc.file_size > doc_size_limit:
                    logger.warning(
                        f"Document size {doc.file_size} exceeds limit {doc_size_limit}. Skipping."
                    )
                    await update.message.reply_text(
                        f"Skipping document '{doc.file_name}': File size exceeds the {doc_size_limit // 1024 // 1024}MB limit."
                    )
                else:
                    doc_file = await doc.get_file()
                    if doc_file.file_size and doc_file.file_size > doc_size_limit:
                        logger.warning(
                            f"Document size {doc_file.file_size} exceeds limit {doc_size_limit}. Skipping."
                        )
                        await update.message.reply_text(
                            f"Skipping document '{doc.file_name}': File size exceeds the {doc_size_limit // 1024 // 1024}MB limit."
                        )
                    else:
                        with io.BytesIO() as buf:
                            await doc_file.download_to_memory(out=buf)
                            buf.seek(0)
                            doc_bytes = buf.read()
                            attachments.append(
                                AttachmentData(
                                    content=doc_bytes,
                                    filename=doc.file_name or f"document_{doc.file_id}",
                                    mime_type=doc.mime_type
                                    or "application/octet-stream",
                                    description=update.message.caption
                                    or f"Telegram Document: {doc.file_name}",
                                )
                            )
                        logger.debug(
                            f"Document from message {update.message.message_id} loaded."
                        )

            # Handle Audio
            if update.message.audio:
                audio = update.message.audio
                logger.info(
                    f"Message {update.message.message_id} from chat {chat_id} contains audio ({audio.file_size} bytes)."
                )

                if audio.file_size and audio.file_size > max_media_size:
                    logger.warning(
                        f"Audio size {audio.file_size} exceeds limit {max_media_size}. Skipping."
                    )
                    await update.message.reply_text(
                        f"Skipping audio: File size exceeds the {max_media_size // 1024 // 1024}MB limit."
                    )
                else:
                    audio_file = await audio.get_file()
                    if audio_file.file_size and audio_file.file_size > max_media_size:
                        logger.warning(
                            f"Audio size {audio_file.file_size} exceeds limit {max_media_size}. Skipping."
                        )
                        await update.message.reply_text(
                            f"Skipping audio: File size exceeds the {max_media_size // 1024 // 1024}MB limit."
                        )
                    else:
                        with io.BytesIO() as buf:
                            await audio_file.download_to_memory(out=buf)
                            buf.seek(0)
                            audio_bytes = buf.read()
                            attachments.append(
                                AttachmentData(
                                    content=audio_bytes,
                                    filename=audio.file_name
                                    or f"audio_{audio.file_id}.mp3",
                                    mime_type=audio.mime_type or "audio/mpeg",
                                    description=update.message.caption
                                    or f"Telegram Audio: {audio.title or 'Unknown Track'}",
                                )
                            )
                        logger.debug(
                            f"Audio from message {update.message.message_id} loaded."
                        )

            # Handle Voice notes. A separate Telegram type from AUDIO: a voice
            # note arrives as `message.voice` and is not matched by filters.AUDIO,
            # so without this the update never reaches this handler at all. It is
            # the everyday way a person sends speech, and the transcription
            # handoff exists for it.
            if update.message.voice:
                voice = update.message.voice
                logger.info(
                    f"Message {update.message.message_id} from chat {chat_id} contains a voice note ({voice.file_size} bytes)."
                )

                if voice.file_size and voice.file_size > max_media_size:
                    logger.warning(
                        f"Voice size {voice.file_size} exceeds limit {max_media_size}. Skipping."
                    )
                    await update.message.reply_text(
                        f"Skipping voice note: File size exceeds the {max_media_size // 1024 // 1024}MB limit."
                    )
                else:
                    voice_file = await voice.get_file()
                    # `Voice.file_size` is optional on the message, so the size
                    # returned by get_file() is the first reliable one. Checking it
                    # before downloading keeps an oversized note out of memory and
                    # gives the same explicit reply as the audio and video paths,
                    # rather than a generic error later at registration.
                    if voice_file.file_size and voice_file.file_size > max_media_size:
                        logger.warning(
                            f"Voice size {voice_file.file_size} exceeds limit {max_media_size}. Skipping."
                        )
                        await update.message.reply_text(
                            f"Skipping voice note: File size exceeds the {max_media_size // 1024 // 1024}MB limit."
                        )
                    else:
                        with io.BytesIO() as buf:
                            await voice_file.download_to_memory(out=buf)
                            buf.seek(0)
                            voice_bytes = buf.read()
                            attachments.append(
                                AttachmentData(
                                    content=voice_bytes,
                                    filename=f"voice_{voice.file_id}.ogg",
                                    # Telegram encodes voice notes as OGG/Opus.
                                    mime_type=voice.mime_type or "audio/ogg",
                                    description=update.message.caption
                                    or "Telegram voice note",
                                )
                            )
                        logger.debug(
                            f"Voice note from message {update.message.message_id} loaded."
                        )

            # Handle Video
            if update.message.video:
                video = update.message.video
                logger.info(
                    f"Message {update.message.message_id} from chat {chat_id} contains video ({video.file_size} bytes)."
                )

                if video.file_size and video.file_size > max_media_size:
                    logger.warning(
                        f"Video size {video.file_size} exceeds limit {max_media_size}. Skipping."
                    )
                    await update.message.reply_text(
                        f"Skipping video: File size exceeds the {max_media_size // 1024 // 1024}MB limit."
                    )
                else:
                    video_file = await video.get_file()
                    if video_file.file_size and video_file.file_size > max_media_size:
                        logger.warning(
                            f"Video size {video_file.file_size} exceeds limit {max_media_size}. Skipping."
                        )
                        await update.message.reply_text(
                            f"Skipping video: File size exceeds the {max_media_size // 1024 // 1024}MB limit."
                        )
                    else:
                        with io.BytesIO() as buf:
                            await video_file.download_to_memory(out=buf)
                            buf.seek(0)
                            video_bytes = buf.read()
                            attachments.append(
                                AttachmentData(
                                    content=video_bytes,
                                    filename=video.file_name
                                    or f"video_{video.file_id}.mp4",
                                    mime_type=video.mime_type or "video/mp4",
                                    description=update.message.caption
                                    or "Telegram Video",
                                )
                            )
                        logger.debug(
                            f"Video from message {update.message.message_id} loaded."
                        )

        except BadRequest as br_err:
            await self._cancel_pending_media_group_if_any(update, context)
            error_msg = str(br_err)
            if "file is too big" in error_msg.lower():
                logger.warning(
                    f"File too large in message {update.message.message_id}: {br_err}"
                )
                await update.message.reply_text(
                    "Sorry, that file is too large. Telegram limits file downloads to 20MB."
                )
            else:
                logger.exception(
                    f"BadRequest processing attachments for message {update.message.message_id}: {br_err}"
                )
                await update.message.reply_text(
                    "Sorry, error processing attached media."
                )
            return
        except Exception as img_err:
            await self._cancel_pending_media_group_if_any(update, context)
            logger.exception(
                f"Failed to process attachments for message {update.message.message_id}: {img_err}"
            )
            await update.message.reply_text("Sorry, error processing attached media.")
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

        user_name = update.effective_user.first_name or "Unknown User"
        if await self._route_mid_turn_update_if_active(
            update=update,
            context=context,
            chat_id=chat_id,
            user_name=user_name,
            attachments=attachments,
        ):
            return

        await self.message_batcher.add_to_batch(update, context, attachments or None)
