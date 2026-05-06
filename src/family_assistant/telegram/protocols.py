from __future__ import annotations

from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable

if TYPE_CHECKING:
    from telegram import Update
    from telegram.ext import ContextTypes

    from family_assistant.telegram.types import AttachmentData
    from family_assistant.tools.types import ConfirmationOutcome


@runtime_checkable
class BatchProcessor(Protocol):
    """Protocol defining the interface for processing a batch of messages."""

    async def process_batch(
        self,
        chat_id: int,
        batch: list[tuple[Update, list[AttachmentData] | None]],
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
        attachments: list[AttachmentData] | None,
    ) -> None:
        """Adds an update to the batch and triggers processing if necessary."""
        ...


@runtime_checkable
class ConfirmationUIManager(Protocol):
    """Protocol defining the interface for requesting user confirmation."""

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
        """
        Requests confirmation from the user via the UI.

        Returns an approved outcome if confirmed, or a terminal outcome otherwise.
        """
        ...

    async def send_existing_confirmation_request(
        self,
        conversation_id: str,
        request_id: str,
        prompt_text: str,
    ) -> ConfirmationOutcome:
        """
        Presents an existing durable confirmation request without waiting for it.

        Returns a completed outcome once the confirmation UI has been delivered,
        or a failed outcome if delivery failed.
        """
        ...
