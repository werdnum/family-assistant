from __future__ import annotations

from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable

if TYPE_CHECKING:
    from telegram import Update
    from telegram.ext import ContextTypes

    from family_assistant.security.taint import TaintMetadata
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

    async def notify_pending_media_group(
        self,
        chat_id: int,
        media_group_id: str,
        context: ContextTypes.DEFAULT_TYPE,
    ) -> None:
        """Pre-arm the batcher to wait for a Telegram media group message.

        Should be called at the very start of update handling—before slow
        operations like attachment downloads—when ``update.message.media_group_id``
        is set. This ensures the batcher waits long enough for all members of the
        album to be added, so the assistant receives them as a single message
        rather than several fragmented ones.
        """
        ...

    async def cancel_pending_media_group(
        self,
        chat_id: int,
        media_group_id: str,
        context: ContextTypes.DEFAULT_TYPE,
    ) -> None:
        """Roll back a previous ``notify_pending_media_group`` call.

        Should be called when the update handler aborts before reaching
        ``add_to_batch`` (e.g. attachment download failed), so the batcher
        does not keep treating the chat as having an outstanding album
        download — which would otherwise extend the next batch's flush
        delay all the way out to ``media_group_max_wait_seconds``.
        """
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
        wait_for_durable_execution: bool = True,
        taint_state_json: TaintMetadata | None = None,
        processing_profile_id: str | None = None,
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
