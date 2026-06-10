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
    ConfirmationAlreadyResolvedError,
    ConfirmationAuthorizationError,
    ConfirmationError,
    ConfirmationExpiredError,
    ConfirmationNotFoundError,
)
from family_assistant.services.confirmation_wait import (
    ConfirmationWaitStrategy,
    wait_for_confirmation_resolution,
)
from family_assistant.services.user_identity import (
    UserIdentityResolutionError,
    UserIdentityResolver,
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
TELEGRAM_CONFIRMATION_MESSAGE_LIMIT = 3800
TELEGRAM_CONFIRMATION_TRUNCATION_NOTICE = (
    "\n\n[Confirmation details truncated for Telegram. Review the full "
    "pending confirmation before approving.]"
)


def _is_durable_confirmation_request_id(request_id: str) -> bool:
    """Return whether a request ID came from durable confirmation storage."""
    return request_id.startswith("confirm_")


@dataclass(frozen=True)
class PendingTelegramConfirmation:
    """Process-local confirmation futures for a Telegram confirmation message."""

    decision_future: asyncio.Future[ConfirmationOutcome]
    execution_future: asyncio.Future[ConfirmationOutcome] | None


@dataclass(frozen=True)
class SentTelegramConfirmationMessage:
    """Telegram message state needed for later status edits."""

    message: Message
    text: str
    parse_mode: str | None


@dataclass(frozen=True)
class TelegramConfirmationSendFailure:
    """Failure reason from a Telegram confirmation send attempt."""

    message: str


type TelegramConfirmationSendResult = (
    SentTelegramConfirmationMessage | TelegramConfirmationSendFailure
)


def _truncate_confirmation_prompt_for_telegram(prompt_text: str) -> str:
    """Keep confirmation messages under Telegram's single-message limit."""
    if len(prompt_text) <= TELEGRAM_CONFIRMATION_MESSAGE_LIMIT:
        return prompt_text
    max_prompt_chars = TELEGRAM_CONFIRMATION_MESSAGE_LIMIT - len(
        TELEGRAM_CONFIRMATION_TRUNCATION_NOTICE
    )
    return prompt_text[:max_prompt_chars] + TELEGRAM_CONFIRMATION_TRUNCATION_NOTICE


class TelegramConfirmationUIManager(ConfirmationUIManager):
    """Implementation of ConfirmationUIManager using Telegram Inline Keyboards."""

    def __init__(
        self,
        application: Application,
        confirmation_timeout: float = 3600.0,
        confirmation_service: ConfirmationService | None = None,
        confirmation_result_waiters: ConfirmationResultWaiterRegistry | None = None,
        user_identity_resolver: UserIdentityResolver | None = None,
    ) -> None:
        self.application = application
        self.confirmation_timeout = confirmation_timeout
        self.confirmation_service = confirmation_service
        self.confirmation_result_waiters = confirmation_result_waiters
        self.user_identity_resolver = user_identity_resolver
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

    def _resolve_callback_user_id(self, telegram_user_id: int | None) -> str:
        if telegram_user_id is None:
            raise ConfirmationAuthorizationError("Telegram callback has no user")
        if self.user_identity_resolver is None:
            return str(telegram_user_id)
        try:
            return self.user_identity_resolver.resolve_telegram_user(
                telegram_user_id
            ).user_id
        except UserIdentityResolutionError as exc:
            raise ConfirmationAuthorizationError(str(exc)) from exc

    async def _send_confirmation_message(
        self,
        *,
        chat_id: int,
        request_id: str,
        prompt_text: str,
    ) -> TelegramConfirmationSendResult:
        keyboard = InlineKeyboardMarkup([
            [
                InlineKeyboardButton(
                    "✅ Confirm", callback_data=f"confirm:{request_id}:yes"
                ),
                InlineKeyboardButton(
                    "❌ Cancel", callback_data=f"confirm:{request_id}:no"
                ),
            ]
        ])

        prompt_text_to_send = _truncate_confirmation_prompt_for_telegram(prompt_text)
        if prompt_text_to_send == prompt_text:
            text_to_send, parse_mode_str = convert_to_telegram_markdown(
                prompt_text_to_send
            )
            parse_mode = ParseMode.MARKDOWN_V2 if parse_mode_str else None
        else:
            text_to_send = prompt_text_to_send
            parse_mode = None

        try:
            message = await self.application.bot.send_message(
                chat_id=chat_id,
                text=text_to_send,
                parse_mode=parse_mode,
                reply_markup=keyboard,
            )
            return SentTelegramConfirmationMessage(
                message=message,
                text=text_to_send,
                parse_mode=parse_mode,
            )
        except BadRequest as parse_err:
            if "Can't parse entities" in str(parse_err) and parse_mode is not None:
                logger.warning(
                    "Telegram rejected MarkdownV2 confirmation message "
                    "(parse error): %s. Falling back to plain text.",
                    parse_err,
                    exc_info=False,
                )
                try:
                    message = await self.application.bot.send_message(
                        chat_id=chat_id,
                        text=prompt_text_to_send,
                        parse_mode=None,
                        reply_markup=keyboard,
                    )
                    return SentTelegramConfirmationMessage(
                        message=message,
                        text=prompt_text_to_send,
                        parse_mode=None,
                    )
                except TelegramError as fallback_err:
                    message = (
                        "Failed to send plain text confirmation message: "
                        f"{fallback_err}"
                    )
                    logger.error(
                        message,
                        exc_info=True,
                    )
                    return TelegramConfirmationSendFailure(message=message)
            message = (
                f"Failed to send confirmation message to chat {chat_id}: {parse_err}"
            )
            logger.error(
                message,
                exc_info=True,
            )
            return TelegramConfirmationSendFailure(message=message)
        except TelegramError as send_err:
            message = (
                f"Failed to send confirmation message to chat {chat_id}: {send_err}"
            )
            logger.error(
                message,
                exc_info=True,
            )
            return TelegramConfirmationSendFailure(message=message)

    async def send_existing_confirmation_request(
        self,
        conversation_id: str,
        request_id: str,
        prompt_text: str,
    ) -> ConfirmationOutcome:
        """Send an existing durable confirmation with the standard Telegram UI."""
        if not self.application or not self.application.bot:
            raise RuntimeError("Telegram application or bot instance not available.")

        try:
            chat_id_int = int(conversation_id)
        except ValueError:
            logger.error(
                "Invalid conversation_id for Telegram confirmation: %r. "
                "Must be integer convertible.",
                conversation_id,
            )
            return ConfirmationOutcome(
                kind="failed",
                result="Invalid Telegram conversation id for confirmation.",
            )

        sent_message = await self._send_confirmation_message(
            chat_id=chat_id_int,
            request_id=request_id,
            prompt_text=prompt_text,
        )
        if isinstance(sent_message, TelegramConfirmationSendFailure):
            return ConfirmationOutcome(
                kind="failed",
                result=sent_message.message,
            )
        logger.debug(
            "Existing durable confirmation %s sent to Telegram message %s",
            request_id,
            sent_message.message.message_id,
        )
        return ConfirmationOutcome(
            kind="completed",
            result=f"Confirmation request {request_id} was sent to Telegram.",
        )

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

            sent_confirmation = await self._send_confirmation_message(
                chat_id=chat_id_int,
                request_id=confirm_uuid,
                prompt_text=prompt_text,
            )
            if isinstance(sent_confirmation, TelegramConfirmationSendFailure):
                await reject_unsent_confirmation()
                return ConfirmationOutcome(
                    kind="failed",
                    result=sent_confirmation.message,
                )
            sent_message = sent_confirmation.message
            text_to_send = sent_confirmation.text
            parse_mode = sent_confirmation.parse_mode
            logger.debug(
                "Confirmation message sent (Message ID: %s)",
                sent_message.message_id,
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
                        parse_mode=parse_mode,
                        reply_markup=None,
                    )
                except TelegramError as edit_err:
                    logger.warning(
                        f"Failed to edit confirmation message {sent_message.message_id}: {edit_err}"
                    )

            def confirmation_status_suffix(
                markdown_suffix: str,
                plain_suffix: str,
            ) -> str:
                return markdown_suffix if parse_mode is not None else plain_suffix

            async def edit_resolved_externally_approved() -> None:
                await edit_confirmation_status(
                    confirmation_status_suffix(
                        "\n\n*Resolved externally* ✅",
                        "\n\nResolved externally ✅",
                    )
                )

            async def edit_resolved_externally_rejected() -> None:
                await edit_confirmation_status(
                    confirmation_status_suffix(
                        "\n\n*Resolved externally* ❌",
                        "\n\nResolved externally ❌",
                    )
                )

            async def on_decision(decision_outcome: ConfirmationOutcome) -> None:
                logger.info(
                    f"Confirmation response received for {confirm_uuid}: {decision_outcome.kind}"
                )

            async def on_decision_approved() -> None:
                self.pending_confirmations.pop(confirm_uuid, None)

            async def on_execution_done(
                execution_outcome: ConfirmationOutcome,
            ) -> None:
                durable_status = await get_durable_request_status()
                if durable_status == "approved":
                    await edit_resolved_externally_approved()
                elif durable_status == "rejected":
                    await edit_resolved_externally_rejected()

            async def on_resolved_approved() -> None:
                await edit_resolved_externally_approved()
                self.pending_confirmations.pop(confirm_uuid, None)

            async def on_resolved_rejected() -> None:
                await edit_resolved_externally_rejected()

            async def on_resolved_failed() -> None:
                await edit_confirmation_status(
                    confirmation_status_suffix(
                        "\n\n*Error: confirmation could not be resolved*",
                        "\n\nError: confirmation could not be resolved",
                    )
                )

            async def on_timed_out() -> None:
                logger.warning(
                    f"Confirmation {confirm_uuid} timed out after {effective_timeout}s."
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
                        text=text_to_send
                        + confirmation_status_suffix(
                            "\n\n\\(Confirmation timed out\\)",
                            "\n\n(Confirmation timed out)",
                        ),
                        parse_mode=parse_mode,
                    )
                except TelegramError as edit_err:
                    logger.warning(
                        f"Failed to edit confirmation message {sent_message.message_id} on timeout: {edit_err}"
                    )

            logger.debug(
                f"Waiting for confirmation response (UUID: {confirm_uuid}, Timeout: {effective_timeout}s)"
            )
            return await wait_for_confirmation_resolution(
                ConfirmationWaitStrategy(
                    decision=decision_future,
                    execution=execution_future,
                    durable=durable_confirmation,
                    get_durable_status=get_durable_request_status,
                    wait_for_execution_result=wait_for_execution_result,
                    on_decision=on_decision,
                    on_execution_done=on_execution_done,
                    on_decision_approved=on_decision_approved,
                    on_resolved_approved=on_resolved_approved,
                    on_resolved_rejected=on_resolved_rejected,
                    on_resolved_failed=on_resolved_failed,
                    on_timed_out=on_timed_out,
                ),
                timeout_seconds=effective_timeout,
            )
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

        original_text_markdown = (
            query.message.text_markdown_v2_urled or query.message.text or ""
        )
        original_text_plain = query.message.text or original_text_markdown
        status_text = ""
        plain_status_text = ""
        try:
            if action == "yes":
                logger.debug(f"Approving confirmation result for {confirm_uuid}")
                await self._approve_confirmation(
                    confirm_uuid,
                    query.from_user.id if query.from_user else None,
                    pending_confirmation,
                )
                status_text = "\n\n*Confirmed* ✅"
                plain_status_text = "\n\nConfirmed ✅"
            elif action == "no":
                logger.debug(f"Rejecting confirmation result for {confirm_uuid}")
                await self._reject_confirmation(
                    confirm_uuid,
                    query.from_user.id if query.from_user else None,
                    pending_confirmation,
                )
                status_text = "\n\n*Cancelled* ❌"
                plain_status_text = "\n\nCancelled ❌"
            else:
                logger.warning(
                    f"Unknown action '{action}' in confirmation callback {confirm_uuid}"
                )
                status_text = "\n\n*Error: Unknown action*"
                plain_status_text = "\n\nError: Unknown action"
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
            plain_status_text = "\n\nError: confirmation expired"
            if pending_confirmation and not pending_confirmation.decision_future.done():
                pending_confirmation.decision_future.set_result(
                    ConfirmationOutcome(kind="timed_out")
                )
        except ConfirmationAlreadyResolvedError as exc:
            logger.warning("Could not resolve Telegram confirmation: %s", exc)
            status_text = "\n\n*Notice: confirmation already resolved*"
            plain_status_text = "\n\nNotice: confirmation already resolved"
        except ConfirmationNotFoundError as exc:
            logger.warning("Could not resolve Telegram confirmation: %s", exc)
            status_text = "\n\n*Error: confirmation could not be resolved*"
            plain_status_text = "\n\nError: confirmation could not be resolved"
            if pending_confirmation and not pending_confirmation.decision_future.done():
                pending_confirmation.decision_future.set_result(
                    ConfirmationOutcome(kind="failed", result=str(exc))
                )
        except ConfirmationError as exc:
            logger.error("Could not resolve Telegram confirmation: %s", exc)
            status_text = "\n\n*Error: confirmation could not be resolved*"
            plain_status_text = "\n\nError: confirmation could not be resolved"
            if pending_confirmation and not pending_confirmation.decision_future.done():
                pending_confirmation.decision_future.set_result(
                    ConfirmationOutcome(kind="failed", result=str(exc))
                )

        try:
            await query.edit_message_text(
                text=original_text_markdown + status_text,
                parse_mode=ParseMode.MARKDOWN_V2,
                reply_markup=None,
            )
        except BadRequest as edit_err:
            logger.warning(
                "Failed to edit confirmation message %s as MarkdownV2 after "
                "callback: %s. Falling back to plain text.",
                query.message.message_id,
                edit_err,
            )
            try:
                await query.edit_message_text(
                    text=original_text_plain + plain_status_text,
                    parse_mode=None,
                    reply_markup=None,
                )
            except TelegramError as fallback_edit_err:
                logger.error(
                    "Failed to edit confirmation message %s as plain text after "
                    "callback: %s",
                    query.message.message_id,
                    fallback_edit_err,
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
        await self.confirmation_service.approve_and_enqueue_execution(
            request_id=request_id,
            approving_user_id=self._resolve_callback_user_id(telegram_user_id),
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
        await self.confirmation_service.reject(
            request_id=request_id,
            rejecting_user_id=self._resolve_callback_user_id(telegram_user_id),
            rejecting_interface="telegram",
        )
        if decision_future and not decision_future.done():
            decision_future.set_result(ConfirmationOutcome(kind="rejected"))
        if self.confirmation_result_waiters is not None:
            self.confirmation_result_waiters.resolve_rejected(request_id)
