from __future__ import annotations

import asyncio
import logging
import uuid
from typing import TYPE_CHECKING, Any

import telegramify_markdown
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Message
from telegram.constants import ParseMode

from family_assistant.telegram.protocols import ConfirmationUIManager

if TYPE_CHECKING:
    from telegram import Update
    from telegram.ext import Application, ContextTypes

logger = logging.getLogger(__name__)


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
        conversation_id: str,
        interface_type: str,
        turn_id: str | None,
        prompt_text: str,
        tool_name: str,
        # ast-grep-ignore: no-dict-any - Legacy code - needs structured types
        tool_args: dict[str, Any],
        timeout: float,
    ) -> bool:
        """Sends confirmation message and waits for user response or timeout."""
        effective_timeout = min(timeout, self.confirmation_timeout)
        confirm_uuid = str(uuid.uuid4())
        if not self.application or not self.application.bot:
            raise RuntimeError("Telegram application or bot instance not available.")

        try:
            chat_id_int = int(conversation_id)
        except ValueError:
            logger.error(
                f"Invalid conversation_id for Telegram confirmation: '{conversation_id}'. Must be integer convertible."
            )
            return False

        logger.info(
            f"Requesting confirmation (UUID: {confirm_uuid}) for tool '{tool_name}' in chat {chat_id_int}"
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
            escaped_prompt_text = telegramify_markdown.markdownify(prompt_text)
            sent_message = await self.application.bot.send_message(
                chat_id=chat_id_int,
                text=escaped_prompt_text,
                parse_mode=ParseMode.MARKDOWN_V2,
                reply_markup=keyboard,
            )
            logger.debug(
                f"Confirmation message sent (Message ID: {sent_message.message_id})"
            )
        except Exception as send_err:
            logger.error(
                f"Failed to send confirmation message to chat {chat_id_int}: {send_err}",
                exc_info=True,
            )
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
                await self.application.bot.edit_message_reply_markup(
                    chat_id=chat_id_int,
                    message_id=sent_message.message_id,
                    reply_markup=None,
                )
                await self.application.bot.edit_message_text(
                    chat_id=chat_id_int,
                    message_id=sent_message.message_id,
                    text=escaped_prompt_text + "\n\n\\(Confirmation timed out\\)",
                    parse_mode=ParseMode.MARKDOWN_V2,
                )
            except Exception as edit_err:
                logger.warning(
                    f"Failed to edit confirmation message {sent_message.message_id} on timeout: {edit_err}"
                )
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
            if query.message:
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
                confirmation_future.set_exception(
                    RuntimeError("Callback message not editable")
                )
                return

            original_text = (
                query.message.text_markdown_v2_urled or query.message.text or ""
            )
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
