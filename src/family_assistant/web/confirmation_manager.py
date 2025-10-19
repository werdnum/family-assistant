"""Web UI confirmation manager for tool execution approval.

This module provides confirmation management for the web interface,
similar to the Telegram bot's confirmation functionality but adapted
for SSE-based communication.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import uuid
from dataclasses import dataclass
from datetime import datetime
from typing import Any

logger = logging.getLogger(__name__)


@dataclass
class PendingConfirmation:
    """Represents a pending tool confirmation request."""

    request_id: str
    tool_name: str
    # ast-grep-ignore: no-dict-any - Legacy code - needs structured types
    tool_args: dict[str, Any]
    confirmation_prompt: str
    future: asyncio.Future[bool]
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
            if not confirmation.future.done():
                confirmation.future.cancel()
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

                now = datetime.now()
                expired_ids = []

                for request_id, confirmation in self.pending_confirmations.items():
                    if (
                        now - confirmation.created_at
                    ).total_seconds() > confirmation.timeout_seconds:
                        expired_ids.append(request_id)

                for request_id in expired_ids:
                    confirmation = self.pending_confirmations.pop(request_id)
                    if not confirmation.future.done():
                        confirmation.future.set_result(False)  # Timeout = rejection
                        logger.info(
                            f"Confirmation request {request_id} for tool '{confirmation.tool_name}' "
                            f"timed out after {confirmation.timeout_seconds}s"
                        )

            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"Error in confirmation cleanup task: {e}")

    async def request_confirmation(
        self,
        conversation_id: str,
        interface_type: str,
        tool_name: str,
        # ast-grep-ignore: no-dict-any - Legacy code - needs structured types
        tool_args: dict[str, Any],
        confirmation_prompt: str,
        timeout_seconds: float = 3600.0,
    ) -> tuple[str, asyncio.Future[bool]]:
        """Request confirmation for a tool execution.

        Args:
            conversation_id: The conversation ID
            interface_type: The interface type (should be "web")
            tool_name: Name of the tool requiring confirmation
            tool_args: Arguments passed to the tool
            confirmation_prompt: Formatted prompt to show to the user
            timeout_seconds: Timeout for the confirmation request

        Returns:
            Tuple of (request_id, future that will resolve to approval status)
        """
        request_id = f"confirm_{uuid.uuid4().hex[:12]}"
        future: asyncio.Future[bool] = asyncio.Future()

        confirmation = PendingConfirmation(
            request_id=request_id,
            tool_name=tool_name,
            tool_args=tool_args,
            confirmation_prompt=confirmation_prompt,
            future=future,
            created_at=datetime.now(),
            timeout_seconds=timeout_seconds,
            conversation_id=conversation_id,
            interface_type=interface_type,
        )

        self.pending_confirmations[request_id] = confirmation

        logger.info(
            f"Created confirmation request {request_id} for tool '{tool_name}' "
            f"in conversation {conversation_id}"
        )

        return request_id, future

    async def handle_confirmation_response(
        self,
        request_id: str,
        approved: bool,
        conversation_id: str | None = None,
    ) -> bool:
        """Handle a confirmation response from the user.

        Args:
            request_id: The confirmation request ID
            approved: Whether the tool execution was approved
            conversation_id: Optional conversation ID for validation

        Returns:
            True if the confirmation was successfully processed, False otherwise
        """
        confirmation = self.pending_confirmations.get(request_id)

        if not confirmation:
            logger.warning(f"Confirmation request {request_id} not found")
            return False

        # Validate conversation ID if provided
        if conversation_id and confirmation.conversation_id != conversation_id:
            logger.warning(
                f"Conversation ID mismatch for confirmation {request_id}: "
                f"expected {confirmation.conversation_id}, got {conversation_id}"
            )
            return False

        # Remove from pending and resolve the future
        self.pending_confirmations.pop(request_id)

        if not confirmation.future.done():
            confirmation.future.set_result(approved)
            logger.info(
                f"Confirmation request {request_id} for tool '{confirmation.tool_name}' "
                f"was {'approved' if approved else 'rejected'}"
            )
            return True
        else:
            logger.warning(f"Confirmation request {request_id} was already resolved")
            return False

    def get_pending_confirmations(
        self,
        conversation_id: str | None = None,
        # ast-grep-ignore: no-dict-any - Legacy code - needs structured types
    ) -> list[dict[str, Any]]:
        """Get list of pending confirmations, optionally filtered by conversation.

        Args:
            conversation_id: Optional conversation ID to filter by

        Returns:
            List of pending confirmation details
        """
        confirmations = []

        for confirmation in self.pending_confirmations.values():
            if conversation_id and confirmation.conversation_id != conversation_id:
                continue

            confirmations.append({
                "request_id": confirmation.request_id,
                "tool_name": confirmation.tool_name,
                "confirmation_prompt": confirmation.confirmation_prompt,
                "created_at": confirmation.created_at.isoformat(),
                "timeout_seconds": confirmation.timeout_seconds,
                "conversation_id": confirmation.conversation_id,
            })

        return confirmations


# Global instance for the web application
web_confirmation_manager = WebConfirmationManager()
