from __future__ import annotations

from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable

if TYPE_CHECKING:
    from telegram import Update
    from telegram.ext import ContextTypes

    from family_assistant.telegram.types import AttachmentData


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
        chat_id: int,
        prompt_text: str,
        tool_name: str,
        # ast-grep-ignore: no-dict-any - Legacy code - needs structured types
        tool_args: dict[str, Any],
        timeout: float,
    ) -> bool:
        """
        Requests confirmation from the user via the UI.

        Returns True if confirmed, False if denied or timed out.
        """
        ...
