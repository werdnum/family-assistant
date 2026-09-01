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
    from family_assistant.security.taint import TaintMetadata
    from family_assistant.services.notifier import Notifier
    from family_assistant.storage.database import (
        Database,
        DatabaseExecutor,
        DatabaseTransaction,
    )
    from family_assistant.storage.repositories.confirmation_requests import (
        ConfirmationRequestRow,
        ConfirmationStatus,
    )
    from family_assistant.tools.types import (
        ToolArgumentsView,
        ToolCallReviewAuthorization,
    )

logger = logging.getLogger(__name__)

CONFIRMATION_TOOL_EXECUTION_TASK_TYPE = "confirmation_tool_execution"
DURABLE_CONFIRMATION_STATUS_POLL_SECONDS = 5.0
DURABLE_CONFIRMATION_EXECUTION_WAIT_SECONDS = 1800.0


def _review_authorization_fields(
    *,
    authorization: ToolCallReviewAuthorization | None,
    tool_name: str,
    tool_call_id: str | None,
    tool_args: ToolArgumentsView,
) -> tuple[str | None, str | None, str | None]:
    """Return persisted policy fields only for the exact reviewed call."""
    if authorization is None:
        return None, None, None
    if (
        authorization.tool_name != tool_name
        or authorization.tool_args != tool_args
        or authorization.call_id not in {"", tool_call_id or ""}
    ):
        logger.warning(
            "Ignoring mismatched tool-call review authorization for durable %s",
            tool_name,
        )
        return None, None, None
    return (
        authorization.sink_class,
        authorization.static_policy_reason,
        authorization.taint_policy_reason,
    )


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
        db: Database,
        notifier: Notifier | None = None,
    ) -> None:
        self._db = db
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
        processing_profile_id: str | None = None,
        origin_interface_type: str | None = None,
        origin_conversation_id: str | None = None,
        taint_state_json: TaintMetadata | None = None,
        sink_class: str | None = None,
        static_policy_reason: str | None = None,
        taint_policy_reason: str | None = None,
        tool_call_review_authorization: ToolCallReviewAuthorization | None = None,
        decision_only: bool = False,
    ) -> ConfirmationRequestRow:
        """Create a durable pending confirmation request.

        ``decision_only`` records (durably) that approval should resume a caller
        executing the tool inline rather than enqueueing a background execution
        task — see :meth:`_approve`.
        """
        if tool_call_review_authorization is not None:
            (
                sink_class,
                static_policy_reason,
                taint_policy_reason,
            ) = _review_authorization_fields(
                authorization=tool_call_review_authorization,
                tool_name=tool_name,
                tool_call_id=tool_call_id,
                tool_args=tool_args,
            )

        request_id = f"confirm_{uuid.uuid4().hex[:12]}"
        request = await self._db.confirmation_requests.create(
            request_id=request_id,
            target_user_id=target_user_id,
            tool_name=tool_name,
            tool_args=tool_args,
            tool_call_id=tool_call_id,
            source_message_internal_id=source_message_internal_id,
            confirmation_prompt=confirmation_prompt,
            expires_at=expires_at,
            processing_profile_id=processing_profile_id,
            origin_interface_type=origin_interface_type,
            origin_conversation_id=origin_conversation_id,
            taint_state_json=taint_state_json,
            sink_class=sink_class,
            static_policy_reason=static_policy_reason,
            taint_policy_reason=taint_policy_reason,
            decision_only=decision_only,
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
            await self._notifier.send_notification(
                user_identifier=target_user_id,
                title="Confirmation needed",
                body=confirmation_prompt[:150],
                db_context=self._db,
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
        return await self._approve(
            request_id=request_id,
            approving_user_id=approving_user_id,
            approving_interface=approving_interface,
            enqueue_execution=True,
        )

    async def approve_without_enqueueing_execution(
        self,
        *,
        request_id: str,
        approving_user_id: str,
        approving_interface: str,
    ) -> ConfirmationRequestRow:
        """Approve a pending request whose caller will execute the tool inline."""
        return await self._approve(
            request_id=request_id,
            approving_user_id=approving_user_id,
            approving_interface=approving_interface,
            enqueue_execution=False,
        )

    async def _approve(
        self,
        *,
        request_id: str,
        approving_user_id: str,
        approving_interface: str,
        enqueue_execution: bool,
    ) -> ConfirmationRequestRow:
        """Approve a pending request and optionally enqueue background execution atomically."""

        async def _body(txn: DatabaseTransaction) -> ConfirmationRequestRow:
            request = await self._get_authorized_request(
                db=txn,
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

            # The durable decision_only flag is authoritative: a decision-only
            # request (a delegated run resumed inline on approval) must never
            # enqueue a background execution task, even if this approval is
            # handled by a process/restart that lost the in-memory waiter and
            # therefore reached the enqueueing path.
            should_enqueue = enqueue_execution and not request["decision_only"]
            execution_task_id = (
                f"confirmation_tool_execution:{request_id}" if should_enqueue else None
            )
            approved = await txn.confirmation_requests.approve_pending(
                request_id=request_id,
                resolving_user_id=approving_user_id,
                resolving_interface=approving_interface,
                execution_task_id=execution_task_id,
                now=now,
            )
            if approved is None:
                refreshed = await txn.confirmation_requests.get(request_id)
                if refreshed is None:
                    raise ConfirmationNotFoundError(
                        f"Confirmation request {request_id} not found"
                    )
                return self._handle_concurrent_resolution(refreshed, "approved", now)

            if execution_task_id is not None:
                await txn.tasks.enqueue(
                    task_id=execution_task_id,
                    task_type=CONFIRMATION_TOOL_EXECUTION_TASK_TYPE,
                    payload={"confirmation_request_id": request_id},
                    original_task_id=execution_task_id,
                    max_retries_override=0,
                )
            return approved

        return await self._db.atomic(_body)

    async def reject(
        self,
        *,
        request_id: str,
        rejecting_user_id: str,
        rejecting_interface: str,
    ) -> ConfirmationRequestRow:
        """Reject a pending confirmation request."""
        request = await self._get_authorized_request(
            db=self._db,
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

        rejected = await self._db.confirmation_requests.reject_pending(
            request_id=request_id,
            resolving_user_id=rejecting_user_id,
            resolving_interface=rejecting_interface,
            now=now,
        )
        if rejected is None:
            refreshed = await self._db.confirmation_requests.get(request_id)
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
        return await self._db.confirmation_requests.list_pending_for_user(user_id)

    async def get_for_user(
        self,
        *,
        request_id: str,
        user_id: str,
    ) -> ConfirmationRequestRow:
        """Get a confirmation request after checking target-user authorization."""
        return await self._get_authorized_request(
            db=self._db,
            request_id=request_id,
            user_id=user_id,
        )

    async def mark_expired(self, *, now: datetime) -> int:
        """Expire pending requests whose deadline has passed."""
        if now.tzinfo is None or now.utcoffset() is None:
            raise ValueError("now must be timezone-aware")
        now = now.astimezone(UTC)
        return await self._db.confirmation_requests.mark_expired(now=now)

    @staticmethod
    async def _get_authorized_request(
        *,
        db: DatabaseExecutor,
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
    db_context: Database,
    target_user_id: str,
    tool_name: str,
    tool_call_id: str | None,
    tool_args: ToolArgumentsView,
    confirmation_prompt: str,
    timeout_seconds: float,
    turn_id: str | None,
    now: datetime,
    processing_profile_id: str | None = None,
    origin_interface_type: str | None = None,
    origin_conversation_id: str | None = None,
    taint_state_json: TaintMetadata | None = None,
    sink_class: str | None = None,
    static_policy_reason: str | None = None,
    taint_policy_reason: str | None = None,
    tool_call_review_authorization: ToolCallReviewAuthorization | None = None,
) -> ConfirmationRequestRow:
    """Record a durable pending confirmation for a tool that needs approval.

    Shared by callers that persist a confirmation for later approval rather than
    waiting on a live channel (email intake, the non-streaming chat API, and the
    durable half of the streaming chat path). Resolves the originating user
    message from ``turn_id`` so the approval threads back to the right
    conversation. ``processing_profile_id`` and the origin interface/conversation
    record the requesting context so the deferred execution runs under the same
    profile and conversation, which matters for callers (automation scripts)
    whose background turn has no source message row.
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
        processing_profile_id=processing_profile_id,
        origin_interface_type=origin_interface_type,
        origin_conversation_id=origin_conversation_id,
        taint_state_json=taint_state_json,
        sink_class=sink_class,
        static_policy_reason=static_policy_reason,
        taint_policy_reason=taint_policy_reason,
        tool_call_review_authorization=tool_call_review_authorization,
    )
