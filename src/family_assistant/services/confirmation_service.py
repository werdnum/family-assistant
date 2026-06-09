"""Service for durable tool confirmation requests."""

from __future__ import annotations

import logging
import uuid
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

from family_assistant.services.notifier import (
    CONFIRMATION_CATEGORY,
    NotificationMetadata,
)

if TYPE_CHECKING:
    from collections.abc import Callable
    from contextlib import AbstractAsyncContextManager

    from family_assistant.services.notifier import Notifier
    from family_assistant.storage.context import DatabaseContext
    from family_assistant.storage.repositories.confirmation_requests import (
        ConfirmationRequestRow,
        ConfirmationStatus,
    )
    from family_assistant.tools.types import ToolArgumentsView

logger = logging.getLogger(__name__)

CONFIRMATION_TOOL_EXECUTION_TASK_TYPE = "confirmation_tool_execution"
DURABLE_CONFIRMATION_STATUS_POLL_SECONDS = 5.0
DURABLE_CONFIRMATION_EXECUTION_WAIT_SECONDS = 1800.0


class ConfirmationError(Exception):
    """Base class for confirmation service errors."""


class ConfirmationNotFoundError(ConfirmationError):
    """Raised when a confirmation request does not exist."""


class ConfirmationAuthorizationError(ConfirmationError):
    """Raised when a principal cannot resolve a confirmation request."""


class ConfirmationExpiredError(ConfirmationError):
    """Raised when a pending confirmation request has expired."""


class ConfirmationAlreadyResolvedError(ConfirmationError):
    """Raised when a request has already been resolved incompatibly."""


class ConfirmationService:
    """Service that owns durable confirmation transactions and authorization."""

    def __init__(
        self,
        *,
        db_context_factory: Callable[[], AbstractAsyncContextManager[DatabaseContext]],
        notifier: Notifier | None = None,
    ) -> None:
        self._db_context_factory = db_context_factory
        self._notifier = notifier

    async def create_request(
        self,
        *,
        target_user_id: str,
        tool_name: str,
        tool_args: ToolArgumentsView,
        tool_call_id: str | None,
        source_message_internal_id: int | None,
        confirmation_prompt: str,
        expires_at: datetime,
    ) -> ConfirmationRequestRow:
        """Create a durable pending confirmation request."""
        request_id = f"confirm_{uuid.uuid4().hex[:12]}"
        async with self._db_context_factory() as db:
            request = await db.confirmation_requests.create(
                request_id=request_id,
                target_user_id=target_user_id,
                tool_name=tool_name,
                tool_args=tool_args,
                tool_call_id=tool_call_id,
                source_message_internal_id=source_message_internal_id,
                confirmation_prompt=confirmation_prompt,
                expires_at=expires_at,
            )
        # Notify only after the request transaction has committed, so a recipient that immediately
        # approves/rejects (on a separate connection) can resolve the request_id.
        await self._notify_pending(target_user_id, request_id, confirmation_prompt)
        return request

    async def _notify_pending(
        self,
        target_user_id: str,
        request_id: str,
        confirmation_prompt: str,
    ) -> None:
        """Send a push notification for a newly created pending confirmation."""
        if self._notifier is None or not self._notifier.enabled:
            return
        try:
            async with self._db_context_factory() as db:
                await self._notifier.send_notification(
                    user_identifier=target_user_id,
                    title="Confirmation needed",
                    body=confirmation_prompt[:150],
                    db_context=db,
                    metadata=NotificationMetadata(
                        category=CONFIRMATION_CATEGORY,
                        request_id=request_id,
                    ),
                )
        except Exception:
            logger.warning(
                "Failed to send confirmation push notification", exc_info=True
            )

    async def approve_and_enqueue_execution(
        self,
        *,
        request_id: str,
        approving_user_id: str,
        approving_interface: str,
    ) -> ConfirmationRequestRow:
        """Approve a pending request and enqueue its execution atomically."""
        async with self._db_context_factory() as db:
            request = await self._get_authorized_request(
                db=db,
                request_id=request_id,
                user_id=approving_user_id,
            )
            if request["status"] == "approved":
                return request
            if request["status"] != "pending":
                self._raise_if_not_pending(request)

            now = datetime.now(UTC)
            if request["expires_at"] <= now:
                raise ConfirmationExpiredError(
                    f"Confirmation request {request_id} has expired"
                )

            execution_task_id = f"confirmation_tool_execution:{request_id}"
            approved = await db.confirmation_requests.approve_pending(
                request_id=request_id,
                resolving_user_id=approving_user_id,
                resolving_interface=approving_interface,
                execution_task_id=execution_task_id,
                now=now,
            )
            if approved is None:
                refreshed = await db.confirmation_requests.get(request_id)
                if refreshed is None:
                    raise ConfirmationNotFoundError(
                        f"Confirmation request {request_id} not found"
                    )
                return self._handle_concurrent_resolution(refreshed, "approved", now)

            await db.tasks.enqueue(
                task_id=execution_task_id,
                task_type=CONFIRMATION_TOOL_EXECUTION_TASK_TYPE,
                payload={"confirmation_request_id": request_id},
                original_task_id=execution_task_id,
                max_retries_override=0,
            )
            return approved

    async def reject(
        self,
        *,
        request_id: str,
        rejecting_user_id: str,
        rejecting_interface: str,
    ) -> ConfirmationRequestRow:
        """Reject a pending confirmation request."""
        async with self._db_context_factory() as db:
            request = await self._get_authorized_request(
                db=db,
                request_id=request_id,
                user_id=rejecting_user_id,
            )
            if request["status"] == "rejected":
                return request
            if request["status"] != "pending":
                self._raise_if_not_pending(request)

            now = datetime.now(UTC)
            if request["expires_at"] <= now:
                raise ConfirmationExpiredError(
                    f"Confirmation request {request_id} has expired"
                )

            rejected = await db.confirmation_requests.reject_pending(
                request_id=request_id,
                resolving_user_id=rejecting_user_id,
                resolving_interface=rejecting_interface,
                now=now,
            )
            if rejected is None:
                refreshed = await db.confirmation_requests.get(request_id)
                if refreshed is None:
                    raise ConfirmationNotFoundError(
                        f"Confirmation request {request_id} not found"
                    )
                return self._handle_concurrent_resolution(refreshed, "rejected", now)
            return rejected

    async def list_pending_for_user(
        self,
        *,
        user_id: str,
    ) -> list[ConfirmationRequestRow]:
        """List pending requests for a user."""
        async with self._db_context_factory() as db:
            return await db.confirmation_requests.list_pending_for_user(user_id)

    async def get_for_user(
        self,
        *,
        request_id: str,
        user_id: str,
    ) -> ConfirmationRequestRow:
        """Get a confirmation request after checking target-user authorization."""
        async with self._db_context_factory() as db:
            return await self._get_authorized_request(
                db=db,
                request_id=request_id,
                user_id=user_id,
            )

    async def mark_expired(self, *, now: datetime) -> int:
        """Expire pending requests whose deadline has passed."""
        if now.tzinfo is None or now.utcoffset() is None:
            raise ValueError("now must be timezone-aware")
        now = now.astimezone(UTC)
        async with self._db_context_factory() as db:
            return await db.confirmation_requests.mark_expired(now=now)

    @staticmethod
    async def _get_authorized_request(
        *,
        db: DatabaseContext,
        request_id: str,
        user_id: str,
    ) -> ConfirmationRequestRow:
        request = await db.confirmation_requests.get(request_id)
        if request is None:
            raise ConfirmationNotFoundError(
                f"Confirmation request {request_id} not found"
            )
        if request["target_user_id"] != user_id:
            raise ConfirmationAuthorizationError(
                f"User {user_id} cannot resolve confirmation request {request_id}"
            )
        return request

    @staticmethod
    def _raise_if_not_pending(request: ConfirmationRequestRow) -> None:
        if request["status"] == "expired":
            raise ConfirmationExpiredError(
                f"Confirmation request {request['id']} has expired"
            )
        raise ConfirmationAlreadyResolvedError(
            f"Confirmation request {request['id']} is already {request['status']}"
        )

    @staticmethod
    def _handle_concurrent_resolution(
        request: ConfirmationRequestRow,
        expected_status: ConfirmationStatus,
        now: datetime,
    ) -> ConfirmationRequestRow:
        if request["status"] == expected_status:
            return request
        if request["status"] == "pending" and request["expires_at"] <= now:
            raise ConfirmationExpiredError(
                f"Confirmation request {request['id']} has expired"
            )
        if request["status"] == "expired":
            raise ConfirmationExpiredError(
                f"Confirmation request {request['id']} has expired"
            )
        raise ConfirmationAlreadyResolvedError(
            f"Confirmation request {request['id']} is already {request['status']}"
        )


async def create_durable_confirmation(
    *,
    confirmation_service: ConfirmationService,
    db_context: DatabaseContext,
    target_user_id: str,
    tool_name: str,
    tool_call_id: str | None,
    tool_args: ToolArgumentsView,
    confirmation_prompt: str,
    timeout_seconds: float,
    turn_id: str | None,
    now: datetime,
) -> ConfirmationRequestRow:
    """Record a durable pending confirmation for a tool that needs approval.

    Shared by callers that persist a confirmation for later approval rather than
    waiting on a live channel (email intake, the non-streaming chat API, and the
    durable half of the streaming chat path). Resolves the originating user
    message from ``turn_id`` so the approval threads back to the right
    conversation.
    """
    source_message_internal_id: int | None = None
    if turn_id is not None:
        source_row = await db_context.message_history.get_user_row_by_turn_id(turn_id)
        if source_row is not None:
            source_message_internal_id = source_row["internal_id"]

    return await confirmation_service.create_request(
        target_user_id=target_user_id,
        tool_name=tool_name,
        tool_args=tool_args,
        tool_call_id=tool_call_id,
        source_message_internal_id=source_message_internal_id,
        confirmation_prompt=confirmation_prompt,
        expires_at=now + timedelta(seconds=timeout_seconds),
    )
