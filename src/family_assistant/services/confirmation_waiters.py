"""Process-local waiters for live durable confirmation continuations."""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING

from family_assistant.tools.types import ConfirmationOutcome, ToolResult

if TYPE_CHECKING:
    from family_assistant.security.taint import TaintMetadata

logger = logging.getLogger(__name__)


class ConfirmationResultWaiterRegistry:
    """Tracks live coroutines waiting for durable confirmation execution results."""

    def __init__(self) -> None:
        self._waiters: dict[str, asyncio.Future[ConfirmationOutcome]] = {}
        self._decision_only_requests: set[str] = set()

    def register(self, request_id: str) -> asyncio.Future[ConfirmationOutcome]:
        """Register a live waiter for a durable confirmation request."""
        existing = self._waiters.get(request_id)
        if existing is not None and not existing.done():
            raise RuntimeError(f"Confirmation waiter already registered: {request_id}")

        future: asyncio.Future[ConfirmationOutcome] = (
            asyncio.get_running_loop().create_future()
        )
        self._waiters[request_id] = future
        return future

    def unregister(
        self,
        request_id: str,
        future: asyncio.Future[ConfirmationOutcome] | None = None,
    ) -> None:
        """Remove a live waiter if it is still the current waiter."""
        if future is not None and self._waiters.get(request_id) is not future:
            return
        self._waiters.pop(request_id, None)
        self._decision_only_requests.discard(request_id)

    def mark_decision_only(self, request_id: str) -> None:
        """Mark a request whose approved tool executes in the waiting coroutine."""
        self._decision_only_requests.add(request_id)

    def is_decision_only(self, request_id: str) -> bool:
        """Return whether approval should resolve only the decision future."""
        return request_id in self._decision_only_requests

    def resolve_completed(
        self,
        request_id: str,
        result: str | ToolResult,
        *,
        taint_metadata: TaintMetadata,
    ) -> bool:
        """Resolve a live waiter with the executed tool result."""
        return self._resolve(
            request_id,
            ConfirmationOutcome(
                kind="completed",
                result=result,
                taint_metadata=taint_metadata,
            ),
        )

    def resolve_rejected(self, request_id: str) -> bool:
        """Resolve a live waiter as rejected."""
        return self._resolve(request_id, ConfirmationOutcome(kind="rejected"))

    def resolve_timed_out(self, request_id: str) -> bool:
        """Resolve a live waiter as timed out."""
        return self._resolve(request_id, ConfirmationOutcome(kind="timed_out"))

    def resolve_cancelled(self, request_id: str) -> bool:
        """Resolve a live waiter as cancelled."""
        return self._resolve(request_id, ConfirmationOutcome(kind="cancelled"))

    def resolve_failed(
        self,
        request_id: str,
        result: str | ToolResult,
        *,
        taint_metadata: TaintMetadata,
    ) -> bool:
        """Resolve a live waiter with a failed execution result."""
        return self._resolve(
            request_id,
            ConfirmationOutcome(
                kind="failed",
                result=result,
                taint_metadata=taint_metadata,
            ),
        )

    def _resolve(self, request_id: str, outcome: ConfirmationOutcome) -> bool:
        future = self._waiters.get(request_id)
        if future is None:
            return False
        if future.done():
            logger.info("Confirmation waiter %s was already resolved", request_id)
            return False
        future.set_result(outcome)
        return True
