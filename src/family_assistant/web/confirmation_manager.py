"""Web UI confirmation manager for tool execution approval.

This module provides confirmation management for the web interface,
similar to the Telegram bot's confirmation functionality but adapted
for SSE-based communication.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from family_assistant.tools.types import ConfirmationOutcome

logger = logging.getLogger(__name__)


@dataclass
class PendingConfirmation:
    """Represents a pending tool confirmation request."""

    request_id: str
    tool_name: str
    # ast-grep-ignore: no-dict-any - Tool arguments vary per tool and cannot be statically typed
    tool_args: dict[str, Any]
    confirmation_prompt: str
    decision_future: asyncio.Future[ConfirmationOutcome]
    created_at: datetime
    timeout_seconds: float
    conversation_id: str
    interface_type: str


class WebConfirmationManager:
    """Manages tool confirmations for the web interface."""

    def __init__(self) -> None:
        """Initialize the confirmation manager."""
        self.pending_confirmations: dict[str, PendingConfirmation] = {}
        self._cleanup_task: asyncio.Task | None = None

    async def start(self) -> None:
        """Start the confirmation manager and its cleanup task."""
        if self._cleanup_task is None or self._cleanup_task.done():
            self._cleanup_task = asyncio.create_task(self._cleanup_expired())

    async def stop(self) -> None:
        """Stop the confirmation manager and cleanup task."""
        # Cancel all pending confirmation futures
        for request_id, confirmation in list(self.pending_confirmations.items()):
            if not confirmation.decision_future.done():
                confirmation.decision_future.cancel()
                logger.info(
                    f"Cancelled pending confirmation request {request_id} for tool '{confirmation.tool_name}'"
                )
        self.pending_confirmations.clear()

        # Cancel cleanup task
        if self._cleanup_task and not self._cleanup_task.done():
            self._cleanup_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._cleanup_task

    async def _cleanup_expired(self) -> None:
        """Periodically clean up expired confirmation requests."""
        while True:
            try:
                await asyncio.sleep(10)  # Check every 10 seconds

                now = datetime.now(UTC)
                expired_ids = []

                for request_id, confirmation in self.pending_confirmations.items():
                    if (
                        now - confirmation.created_at
                    ).total_seconds() > confirmation.timeout_seconds:
                        expired_ids.append(request_id)

                for request_id in expired_ids:
                    confirmation = self.pending_confirmations.pop(request_id)
                    if not confirmation.decision_future.done():
                        confirmation.decision_future.set_result(
                            ConfirmationOutcome(kind="timed_out")
                        )
                        logger.info(
                            f"Confirmation request {request_id} for tool '{confirmation.tool_name}' "
                            f"timed out after {confirmation.timeout_seconds}s"
                        )

            except asyncio.CancelledError:
                break

    async def request_confirmation(
        self,
        request_id: str,
        conversation_id: str,
        interface_type: str,
        tool_name: str,
        # ast-grep-ignore: no-dict-any - Tool arguments vary per tool and cannot be statically typed
        tool_args: dict[str, Any],
        confirmation_prompt: str,
        timeout_seconds: float = 3600.0,
    ) -> asyncio.Future[ConfirmationOutcome]:
        """Track a live confirmation request until the user decides.

        Args:
            request_id: Durable confirmation request ID
            conversation_id: The conversation ID
            interface_type: The interface type (should be "web")
            tool_name: Name of the tool requiring confirmation
            tool_args: Arguments passed to the tool
            confirmation_prompt: Formatted prompt to show to the user
            timeout_seconds: Timeout for the user decision

        Returns:
            Future that resolves when the user approves, rejects, or times out
        """
        decision_future: asyncio.Future[ConfirmationOutcome] = (
            asyncio.get_running_loop().create_future()
        )
        confirmation = PendingConfirmation(
            request_id=request_id,
            tool_name=tool_name,
            tool_args=tool_args,
            confirmation_prompt=confirmation_prompt,
            decision_future=decision_future,
            created_at=datetime.now(UTC),
            timeout_seconds=timeout_seconds,
            conversation_id=conversation_id,
            interface_type=interface_type,
        )

        self.pending_confirmations[request_id] = confirmation

        logger.info(
            f"Created confirmation request {request_id} for tool '{tool_name}' "
            f"in conversation {conversation_id}"
        )

        return decision_future

    def resolve_approved(self, request_id: str) -> bool:
        """Resolve the live user-decision future as approved."""
        return self._resolve_decision(request_id, ConfirmationOutcome(kind="approved"))

    def resolve_rejected(self, request_id: str) -> bool:
        """Resolve the live user-decision future as rejected."""
        return self._resolve_decision(request_id, ConfirmationOutcome(kind="rejected"))

    def _resolve_decision(
        self,
        request_id: str,
        outcome: ConfirmationOutcome,
    ) -> bool:
        confirmation = self.pending_confirmations.get(request_id)
        if confirmation is None or confirmation.decision_future.done():
            return False
        confirmation.decision_future.set_result(outcome)
        return True

    def remove_confirmation(self, request_id: str) -> None:
        """Remove a process-local pending confirmation."""
        self.pending_confirmations.pop(request_id, None)


# Global instance for the web application
web_confirmation_manager = WebConfirmationManager()
