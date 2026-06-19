"""Durable web confirmation UI manager.

Implements the :class:`ConfirmationUIManager` protocol for the ``web`` interface
so confirmations work both for a live streaming turn and for background runs
(e.g. asynchronous profile delegation executed by the TaskWorker), which have no
live request to host an inline confirmation closure.

The flow mirrors what the chat turn producer used to inline: a durable
confirmation record (so the decision survives client disconnects, is delivered
via push notification, and is discoverable via the pending-confirmations API),
an in-memory decision future resolved by ``POST /v1/chat/confirm_tool``, and an
execution future resolved when the approved tool finishes. Live SSE subscribers
additionally receive ``tool_confirmation_request`` / ``tool_confirmation_result``
hub events when a stream hub is available.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any

from family_assistant.services.confirmation_service import (
    DURABLE_CONFIRMATION_EXECUTION_WAIT_SECONDS,
    ConfirmationAuthorizationError,
    ConfirmationError,
    ConfirmationNotFoundError,
)
from family_assistant.services.confirmation_wait import (
    ConfirmationWaitStrategy,
    wait_for_confirmation_resolution,
)
from family_assistant.tools.types import ConfirmationOutcome
from family_assistant.web.confirmation_manager import web_confirmation_manager

if TYPE_CHECKING:
    from family_assistant.services.confirmation_service import ConfirmationService
    from family_assistant.services.confirmation_waiters import (
        ConfirmationResultWaiterRegistry,
    )
    from family_assistant.web.conversation_stream_hub import ConversationStreamHub


class WebConfirmationUIManager:
    """Deliver and await tool confirmations for the web interface."""

    def __init__(
        self,
        *,
        confirmation_service: ConfirmationService,
        confirmation_result_waiters: ConfirmationResultWaiterRegistry,
        stream_hub: ConversationStreamHub | None,
    ) -> None:
        self._confirmation_service = confirmation_service
        self._confirmation_result_waiters = confirmation_result_waiters
        self._stream_hub = stream_hub

    async def request_confirmation(
        self,
        conversation_id: str,
        interface_type: str,
        turn_id: str | None,
        prompt_text: str,
        tool_name: str,
        # ast-grep-ignore: no-dict-any - confirmation protocol carries arbitrary tool arguments
        tool_args: dict[str, Any],
        timeout: float,
        target_user_id: str | None = None,
        tool_call_id: str | None = None,
        source_message_internal_id: int | None = None,
        wait_for_durable_execution: bool = True,
    ) -> ConfirmationOutcome:
        """Create, deliver and await a durable web confirmation."""
        if target_user_id is None:
            return ConfirmationOutcome(
                kind="failed",
                result=(
                    f"Cannot request web confirmation for tool '{tool_name}': "
                    "no target user is associated with the request."
                ),
            )

        expires_at = datetime.now(UTC) + timedelta(seconds=timeout)
        durable_request = await self._confirmation_service.create_request(
            target_user_id=target_user_id,
            tool_name=tool_name,
            tool_args=tool_args,
            tool_call_id=tool_call_id,
            source_message_internal_id=source_message_internal_id,
            confirmation_prompt=prompt_text,
            expires_at=expires_at,
            decision_only=not wait_for_durable_execution,
        )
        request_id = durable_request["id"]
        if wait_for_durable_execution:
            execution_future = self._confirmation_result_waiters.register(request_id)
        else:
            execution_future = None
            self._confirmation_result_waiters.mark_decision_only(request_id)

        async def get_durable_status() -> str | None:
            try:
                refreshed = await self._confirmation_service.get_for_user(
                    request_id=request_id, user_id=target_user_id
                )
            except ConfirmationNotFoundError:
                return "missing"
            except ConfirmationAuthorizationError:
                return "unauthorized"
            except ConfirmationError:
                return "error"
            return refreshed["status"]

        async def wait_for_execution_result() -> ConfirmationOutcome:
            if execution_future is None:
                return ConfirmationOutcome(kind="approved")
            try:
                return await asyncio.wait_for(
                    asyncio.shield(execution_future),
                    timeout=DURABLE_CONFIRMATION_EXECUTION_WAIT_SECONDS,
                )
            except TimeoutError:
                return ConfirmationOutcome(
                    kind="failed",
                    result=(
                        f"Error executing approved tool '{tool_name}': background "
                        "execution did not complete in time."
                    ),
                )

        async def publish_request() -> None:
            if self._stream_hub is None:
                return
            await self._stream_hub.publish(
                conversation_id,
                "tool_confirmation_request",
                turn_id=turn_id,
                payload={
                    "request_id": request_id,
                    "tool_name": tool_name,
                    "tool_call_id": tool_call_id,
                    "confirmation_prompt": prompt_text,
                    "timeout_seconds": timeout,
                    "args": tool_args,
                },
            )

        async def publish_result(*, approved: bool) -> None:
            if self._stream_hub is None:
                return
            await self._stream_hub.publish(
                conversation_id,
                "tool_confirmation_result",
                turn_id=turn_id,
                payload={"request_id": request_id, "approved": approved},
            )

        async def on_decision(decision_outcome: ConfirmationOutcome) -> None:
            if decision_outcome.kind == "timed_out":
                await self._confirmation_service.mark_expired(now=datetime.now(UTC))
            if decision_outcome.kind != "approved":
                await publish_result(approved=False)

        async def on_execution_done(execution_outcome: ConfirmationOutcome) -> None:
            await publish_result(
                approved=execution_outcome.kind in {"completed", "failed"}
            )

        async def on_resolved_approved() -> None:
            await publish_result(approved=True)
            web_confirmation_manager.remove_confirmation(request_id)

        async def on_resolved_rejected() -> None:
            await publish_result(approved=False)

        async def on_timed_out() -> None:
            await self._confirmation_service.mark_expired(now=datetime.now(UTC))
            await publish_result(approved=False)

        try:
            decision_future = await web_confirmation_manager.request_confirmation(
                request_id=request_id,
                conversation_id=conversation_id,
                interface_type=interface_type,
                tool_name=tool_name,
                tool_args=tool_args,
                confirmation_prompt=prompt_text,
                timeout_seconds=timeout,
            )
            await publish_request()
            return await wait_for_confirmation_resolution(
                ConfirmationWaitStrategy(
                    decision=decision_future,
                    execution=execution_future,
                    durable=True,
                    get_durable_status=get_durable_status,
                    wait_for_execution_result=wait_for_execution_result,
                    on_decision=on_decision,
                    on_execution_done=on_execution_done,
                    on_decision_approved=on_resolved_approved,
                    on_resolved_approved=on_resolved_approved,
                    on_resolved_rejected=on_resolved_rejected,
                    on_resolved_failed=on_resolved_rejected,
                    on_timed_out=on_timed_out,
                ),
                timeout_seconds=timeout,
            )
        finally:
            web_confirmation_manager.remove_confirmation(request_id)
            self._confirmation_result_waiters.unregister(request_id, execution_future)

    async def send_existing_confirmation_request(
        self,
        conversation_id: str,
        request_id: str,
        prompt_text: str,
    ) -> ConfirmationOutcome:
        """Re-deliver an existing durable confirmation to live web subscribers."""
        if self._stream_hub is not None:
            await self._stream_hub.publish(
                conversation_id,
                "tool_confirmation_request",
                turn_id=None,
                payload={
                    "request_id": request_id,
                    "confirmation_prompt": prompt_text,
                },
            )
        return ConfirmationOutcome(kind="completed")
