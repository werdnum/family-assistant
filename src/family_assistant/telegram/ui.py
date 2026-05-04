from __future__ import annotations

import asyncio
import logging
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Message
from telegram.constants import ParseMode
from telegram.error import BadRequest, TelegramError

from family_assistant.services.confirmation_service import (
    DURABLE_CONFIRMATION_EXECUTION_WAIT_SECONDS,
    DURABLE_CONFIRMATION_STATUS_POLL_SECONDS,
    ConfirmationAlreadyResolvedError,
    ConfirmationAuthorizationError,
    ConfirmationError,
    ConfirmationExpiredError,
    ConfirmationNotFoundError,
)
from family_assistant.telegram.markdown_utils import convert_to_telegram_markdown
from family_assistant.telegram.protocols import ConfirmationUIManager
from family_assistant.tools.types import ConfirmationOutcome

if TYPE_CHECKING:
    from telegram import Update
    from telegram.ext import Application, ContextTypes

    from family_assistant.services.confirmation_service import ConfirmationService
    from family_assistant.services.confirmation_waiters import (
        ConfirmationResultWaiterRegistry,
    )

logger = logging.getLogger(__name__)


def _is_durable_confirmation_request_id(request_id: str) -> bool:
    """Return whether a request ID came from durable confirmation storage."""
    return request_id.startswith("confirm_")


@dataclass(frozen=True)
class PendingTelegramConfirmation:
    """Process-local confirmation futures for a Telegram confirmation message."""

    decision_future: asyncio.Future[ConfirmationOutcome]
    execution_future: asyncio.Future[ConfirmationOutcome] | None


class TelegramConfirmationUIManager(ConfirmationUIManager):
    """Implementation of ConfirmationUIManager using Telegram Inline Keyboards."""

    def __init__(
        self,
        application: Application,
        confirmation_timeout: float = 3600.0,
        confirmation_service: ConfirmationService | None = None,
        confirmation_result_waiters: ConfirmationResultWaiterRegistry | None = None,
    ) -> None:
        self.application = application
        self.confirmation_timeout = confirmation_timeout
        self.confirmation_service = confirmation_service
        self.confirmation_result_waiters = confirmation_result_waiters
        self.pending_confirmations: dict[str, PendingTelegramConfirmation] = {}

    def _unregister_execution_future(
        self,
        request_id: str,
        execution_future: asyncio.Future[ConfirmationOutcome] | None,
    ) -> None:
        if (
            self.confirmation_result_waiters is not None
            and execution_future is not None
        ):
            self.confirmation_result_waiters.unregister(request_id, execution_future)

    async def request_confirmation(
        self,
        conversation_id: str,
        interface_type: str,
        turn_id: str | None,
        prompt_text: str,
        tool_name: str,
        # ast-grep-ignore: no-dict-any - tool args have varying keys per tool
        tool_args: dict[str, Any],
        timeout: float,
        target_user_id: str | None = None,
        tool_call_id: str | None = None,
        source_message_internal_id: int | None = None,
    ) -> ConfirmationOutcome:
        """Sends confirmation message and waits for user response or timeout."""
        effective_timeout = min(timeout, self.confirmation_timeout)
        confirmation_result_waiters = self.confirmation_result_waiters
        durable_confirmation = (
            self.confirmation_service is not None
            and confirmation_result_waiters is not None
            and target_user_id is not None
            and tool_call_id is not None
        )
        if not self.application or not self.application.bot:
            raise RuntimeError("Telegram application or bot instance not available.")

        try:
            chat_id_int = int(conversation_id)
        except ValueError:
            logger.error(
                f"Invalid conversation_id for Telegram confirmation: '{conversation_id}'. Must be integer convertible."
            )
            return ConfirmationOutcome(
                kind="failed",
                result="Invalid Telegram conversation id for confirmation.",
            )

        if durable_confirmation:
            if target_user_id is None or tool_call_id is None:
                raise RuntimeError(
                    "Durable confirmation missing target user or call id"
                )
            assert self.confirmation_service is not None
            assert confirmation_result_waiters is not None
            request = await self.confirmation_service.create_request(
                target_user_id=target_user_id,
                tool_name=tool_name,
                tool_args=tool_args,
                tool_call_id=tool_call_id,
                source_message_internal_id=source_message_internal_id,
                confirmation_prompt=prompt_text,
                expires_at=datetime.now(UTC) + timedelta(seconds=effective_timeout),
            )
            confirm_uuid = request["id"]
            execution_future = confirmation_result_waiters.register(confirm_uuid)
        else:
            confirm_uuid = str(uuid.uuid4())
            execution_future = None
        decision_future: asyncio.Future[ConfirmationOutcome] = (
            asyncio.get_running_loop().create_future()
        )

        async def get_durable_request_status() -> str | None:
            if (
                not durable_confirmation
                or self.confirmation_service is None
                or target_user_id is None
            ):
                return None
            try:
                refreshed_request = await self.confirmation_service.get_for_user(
                    request_id=confirm_uuid,
                    user_id=target_user_id,
                )
            except ConfirmationNotFoundError:
                logger.warning(
                    "Durable confirmation %s disappeared while waiting",
                    confirm_uuid,
                )
                return "missing"
            except ConfirmationAuthorizationError:
                logger.warning(
                    "User lost access to durable confirmation %s while waiting",
                    confirm_uuid,
                )
                return "unauthorized"
            except ConfirmationError as exc:
                logger.warning(
                    "Could not read durable confirmation %s while waiting: %s",
                    confirm_uuid,
                    exc,
                )
                return "error"
            return refreshed_request["status"]

        async def wait_for_execution_result() -> ConfirmationOutcome:
            if execution_future is None:
                raise RuntimeError(
                    f"Durable confirmation {confirm_uuid} has no execution future"
                )
            try:
                return await asyncio.wait_for(
                    asyncio.shield(execution_future),
                    timeout=DURABLE_CONFIRMATION_EXECUTION_WAIT_SECONDS,
                )
            except TimeoutError:
                return ConfirmationOutcome(
                    kind="failed",
                    result=(
                        f"Error executing approved tool '{tool_name}': "
                        "background execution did not complete in time."
                    ),
                )

        async def reject_unsent_confirmation() -> None:
            if (
                durable_confirmation
                and self.confirmation_service is not None
                and target_user_id is not None
            ):
                try:
                    await self.confirmation_service.reject(
                        request_id=confirm_uuid,
                        rejecting_user_id=target_user_id,
                        rejecting_interface="telegram",
                    )
                finally:
                    self._unregister_execution_future(confirm_uuid, execution_future)

        try:
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

            # Convert to Telegram MarkdownV2 with bug fixes
            text_to_send, parse_mode_str = convert_to_telegram_markdown(prompt_text)

            try:
                sent_message = await self.application.bot.send_message(
                    chat_id=chat_id_int,
                    text=text_to_send,
                    parse_mode=ParseMode.MARKDOWN_V2 if parse_mode_str else None,
                    reply_markup=keyboard,
                )
                logger.debug(
                    f"Confirmation message sent (Message ID: {sent_message.message_id})"
                )
            except BadRequest as parse_err:
                # Defense-in-depth: If Telegram still rejects due to parse errors, fall back to plain text
                if "Can't parse entities" in str(parse_err) and parse_mode_str:
                    logger.warning(
                        f"Telegram rejected MarkdownV2 confirmation message (parse error): {parse_err}. Falling back to plain text.",
                        exc_info=False,
                    )
                    try:
                        sent_message = await self.application.bot.send_message(
                            chat_id=chat_id_int,
                            text=prompt_text,
                            parse_mode=None,
                            reply_markup=keyboard,
                        )
                        text_to_send = prompt_text  # Update for later use
                        logger.debug(
                            f"Confirmation message sent in plain text (Message ID: {sent_message.message_id})"
                        )
                    except TelegramError as fallback_err:
                        logger.error(
                            f"Failed to send plain text confirmation message: {fallback_err}",
                            exc_info=True,
                        )
                        await reject_unsent_confirmation()
                        return ConfirmationOutcome(
                            kind="failed",
                            result="Failed to send confirmation message.",
                        )
                else:
                    logger.error(
                        f"Failed to send confirmation message to chat {chat_id_int}: {parse_err}",
                        exc_info=True,
                    )
                    await reject_unsent_confirmation()
                    return ConfirmationOutcome(
                        kind="failed",
                        result="Failed to send confirmation message.",
                    )
            except TelegramError as send_err:
                logger.error(
                    f"Failed to send confirmation message to chat {chat_id_int}: {send_err}",
                    exc_info=True,
                )
                await reject_unsent_confirmation()
                return ConfirmationOutcome(
                    kind="failed",
                    result="Failed to send confirmation message.",
                )

            self.pending_confirmations[confirm_uuid] = PendingTelegramConfirmation(
                decision_future=decision_future,
                execution_future=execution_future,
            )

            async def edit_confirmation_status(status_text: str) -> None:
                try:
                    await self.application.bot.edit_message_text(
                        chat_id=chat_id_int,
                        message_id=sent_message.message_id,
                        text=text_to_send + status_text,
                        parse_mode=ParseMode.MARKDOWN_V2,
                        reply_markup=None,
                    )
                except TelegramError as edit_err:
                    logger.warning(
                        f"Failed to edit confirmation message {sent_message.message_id}: {edit_err}"
                    )

            logger.debug(
                f"Waiting for confirmation response (UUID: {confirm_uuid}, Timeout: {effective_timeout}s)"
            )
            deadline = asyncio.get_running_loop().time() + effective_timeout
            while True:
                remaining = deadline - asyncio.get_running_loop().time()
                if remaining <= 0:
                    break

                wait_futures: set[asyncio.Future[ConfirmationOutcome]] = {
                    decision_future
                }
                if execution_future is not None:
                    wait_futures.add(execution_future)
                done, _ = await asyncio.wait(
                    wait_futures,
                    timeout=min(DURABLE_CONFIRMATION_STATUS_POLL_SECONDS, remaining),
                    return_when=asyncio.FIRST_COMPLETED,
                )

                if decision_future in done:
                    decision_outcome = decision_future.result()
                    logger.info(
                        f"Confirmation response received for {confirm_uuid}: {decision_outcome.kind}"
                    )
                    if not durable_confirmation:
                        return decision_outcome
                    if decision_outcome.kind != "approved":
                        return decision_outcome
                    self.pending_confirmations.pop(confirm_uuid, None)
                    return await wait_for_execution_result()
                if execution_future in done:
                    durable_status = await get_durable_request_status()
                    if durable_status == "approved":
                        await edit_confirmation_status("\n\n*Resolved externally* ✅")
                    elif durable_status == "rejected":
                        await edit_confirmation_status("\n\n*Resolved externally* ❌")
                    return execution_future.result()

                durable_status = await get_durable_request_status()
                if durable_status == "approved" and execution_future is not None:
                    await edit_confirmation_status("\n\n*Resolved externally* ✅")
                    self.pending_confirmations.pop(confirm_uuid, None)
                    return await wait_for_execution_result()
                if durable_status == "rejected":
                    await edit_confirmation_status("\n\n*Resolved externally* ❌")
                    return ConfirmationOutcome(kind="rejected")
                if durable_status in {"expired", "missing", "unauthorized", "error"}:
                    await edit_confirmation_status(
                        "\n\n*Error: confirmation could not be resolved*"
                    )
                    return ConfirmationOutcome(
                        kind="failed",
                        result="Confirmation request could not be resolved.",
                    )

            logger.warning(
                f"Confirmation {confirm_uuid} timed out after {effective_timeout}s."
            )
            durable_status = await get_durable_request_status()
            if durable_status == "approved" and execution_future is not None:
                await edit_confirmation_status("\n\n*Resolved externally* ✅")
                self.pending_confirmations.pop(confirm_uuid, None)
                return await wait_for_execution_result()
            if durable_status == "rejected":
                await edit_confirmation_status("\n\n*Resolved externally* ❌")
                return ConfirmationOutcome(kind="rejected")
            if durable_status in {"missing", "unauthorized", "error"}:
                await edit_confirmation_status(
                    "\n\n*Error: confirmation could not be resolved*"
                )
                return ConfirmationOutcome(
                    kind="failed",
                    result="Confirmation request could not be resolved.",
                )
            if durable_confirmation and self.confirmation_service is not None:
                await self.confirmation_service.mark_expired(now=datetime.now(UTC))
            try:
                await self.application.bot.edit_message_reply_markup(
                    chat_id=chat_id_int,
                    message_id=sent_message.message_id,
                    reply_markup=None,
                )
                await self.application.bot.edit_message_text(
                    chat_id=chat_id_int,
                    message_id=sent_message.message_id,
                    text=text_to_send + "\n\n\\(Confirmation timed out\\)",
                    parse_mode=ParseMode.MARKDOWN_V2,
                )
            except TelegramError as edit_err:
                logger.warning(
                    f"Failed to edit confirmation message {sent_message.message_id} on timeout: {edit_err}"
                )
            return ConfirmationOutcome(kind="timed_out")
        finally:
            self.pending_confirmations.pop(confirm_uuid, None)
            if durable_confirmation and execution_future is not None:
                self._unregister_execution_future(confirm_uuid, execution_future)

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
                except TelegramError as e:
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
                except TelegramError as e:
                    logger.error(f"Error editing message on invalid callback data: {e}")
            return

        pending_confirmation = self.pending_confirmations.get(confirm_uuid)

        if not query.message or not isinstance(query.message, Message):
            logger.error(
                "Callback query message is not accessible or not a standard message."
            )
            if pending_confirmation and not pending_confirmation.decision_future.done():
                pending_confirmation.decision_future.set_exception(
                    RuntimeError("Callback message not editable")
                )
            return

        original_text = query.message.text_markdown_v2_urled or query.message.text or ""
        status_text = ""
        try:
            if action == "yes":
                logger.debug(f"Approving confirmation result for {confirm_uuid}")
                await self._approve_confirmation(
                    confirm_uuid,
                    query.from_user.id if query.from_user else None,
                    pending_confirmation,
                )
                status_text = "\n\n*Confirmed* ✅"
            elif action == "no":
                logger.debug(f"Rejecting confirmation result for {confirm_uuid}")
                await self._reject_confirmation(
                    confirm_uuid,
                    query.from_user.id if query.from_user else None,
                    pending_confirmation,
                )
                status_text = "\n\n*Cancelled* ❌"
            else:
                logger.warning(
                    f"Unknown action '{action}' in confirmation callback {confirm_uuid}"
                )
                status_text = "\n\n*Error: Unknown action*"
                if (
                    pending_confirmation
                    and not pending_confirmation.decision_future.done()
                ):
                    pending_confirmation.decision_future.set_result(
                        ConfirmationOutcome(kind="failed", result="Unknown action")
                    )
        except ConfirmationAuthorizationError as exc:
            logger.warning("Could not resolve Telegram confirmation: %s", exc)
            return
        except ConfirmationExpiredError as exc:
            logger.warning("Could not resolve Telegram confirmation: %s", exc)
            status_text = "\n\n*Error: confirmation expired*"
            if pending_confirmation and not pending_confirmation.decision_future.done():
                pending_confirmation.decision_future.set_result(
                    ConfirmationOutcome(kind="timed_out")
                )
        except ConfirmationAlreadyResolvedError as exc:
            logger.warning("Could not resolve Telegram confirmation: %s", exc)
            status_text = "\n\n*Notice: confirmation already resolved*"
        except ConfirmationNotFoundError as exc:
            logger.warning("Could not resolve Telegram confirmation: %s", exc)
            status_text = "\n\n*Error: confirmation could not be resolved*"
            if pending_confirmation and not pending_confirmation.decision_future.done():
                pending_confirmation.decision_future.set_result(
                    ConfirmationOutcome(kind="failed", result=str(exc))
                )
        except ConfirmationError as exc:
            logger.error("Could not resolve Telegram confirmation: %s", exc)
            status_text = "\n\n*Error: confirmation could not be resolved*"
            if pending_confirmation and not pending_confirmation.decision_future.done():
                pending_confirmation.decision_future.set_result(
                    ConfirmationOutcome(kind="failed", result=str(exc))
                )

        try:
            await query.edit_message_text(
                text=original_text + status_text,
                parse_mode=ParseMode.MARKDOWN_V2,
                reply_markup=None,
            )
        except TelegramError as edit_err:
            logger.error(
                f"Failed to edit confirmation message {query.message.message_id} after callback: {edit_err}"
            )

    async def _approve_confirmation(
        self,
        request_id: str,
        telegram_user_id: int | None,
        pending: PendingTelegramConfirmation | None,
    ) -> None:
        decision_future = pending.decision_future if pending else None
        if self.confirmation_service is None or not _is_durable_confirmation_request_id(
            request_id
        ):
            if decision_future and not decision_future.done():
                decision_future.set_result(ConfirmationOutcome(kind="approved"))
            return
        if telegram_user_id is None:
            raise ConfirmationAuthorizationError(
                f"Telegram confirmation {request_id} has no approving user"
            )
        await self.confirmation_service.approve_and_enqueue_execution(
            request_id=request_id,
            approving_user_id=str(telegram_user_id),
            approving_interface="telegram",
        )
        if decision_future and not decision_future.done():
            decision_future.set_result(ConfirmationOutcome(kind="approved"))

    async def _reject_confirmation(
        self,
        request_id: str,
        telegram_user_id: int | None,
        pending: PendingTelegramConfirmation | None,
    ) -> None:
        decision_future = pending.decision_future if pending else None
        if self.confirmation_service is None or not _is_durable_confirmation_request_id(
            request_id
        ):
            if decision_future and not decision_future.done():
                decision_future.set_result(ConfirmationOutcome(kind="rejected"))
            return
        if telegram_user_id is None:
            raise ConfirmationAuthorizationError(
                f"Telegram confirmation {request_id} has no rejecting user"
            )
        await self.confirmation_service.reject(
            request_id=request_id,
            rejecting_user_id=str(telegram_user_id),
            rejecting_interface="telegram",
        )
        if decision_future and not decision_future.done():
            decision_future.set_result(ConfirmationOutcome(kind="rejected"))
        if self.confirmation_result_waiters is not None:
            self.confirmation_result_waiters.resolve_rejected(request_id)
