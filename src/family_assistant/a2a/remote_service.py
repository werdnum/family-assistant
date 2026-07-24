"""Remote A2A service implementing DelegatableService.

Translates handle_chat_interaction calls into A2A message/send
requests to a remote agent.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Literal

from family_assistant.a2a.client import A2AClientError
from family_assistant.a2a.result_converter import a2a_task_to_chat_result
from family_assistant.a2a.types import Task, TaskState
from family_assistant.processing.protocol import (
    PENDING,
    PendingPoll,
    RemoteSubmission,
)
from family_assistant.security.taint import A2A_TAINT_METADATA_KEY, TurnTaintState

if TYPE_CHECKING:
    from collections.abc import Sequence

    from family_assistant.a2a.client import A2AClientWrapper
    from family_assistant.interfaces import ChatInterface
    from family_assistant.llm.content_parts import ContentPartDict
    from family_assistant.llm.messages import MessageAttachmentMetadata
    from family_assistant.processing.types import (
        ChatInteractionResult,
        MidTurnInputProvider,
        RemoteServiceConfig,
        RequestConfirmationCallback,
    )
    from family_assistant.security.taint import TaintSource
    from family_assistant.storage.context import DatabaseContext
    from family_assistant.telegram.protocols import ConfirmationUIManager

logger = logging.getLogger(__name__)


class RemoteA2AService:
    """Implements DelegatableService by delegating to a remote A2A agent."""

    kind: Literal["remote"] = "remote"

    def __init__(
        self,
        service_config: RemoteServiceConfig,
        client: A2AClientWrapper,
    ) -> None:
        self._service_config = service_config
        self._client = client

    @property
    def service_config(self) -> RemoteServiceConfig:
        return self._service_config

    async def handle_chat_interaction(
        self,
        db_context: DatabaseContext,
        interface_type: str,
        conversation_id: str,
        trigger_content_parts: list[ContentPartDict],
        trigger_interface_message_id: str | None,
        user_name: str,
        user_id: str | None = None,
        replied_to_interface_id: str | None = None,
        chat_interface: ChatInterface | None = None,
        chat_interfaces: dict[str, ChatInterface] | None = None,
        confirmation_ui_managers: dict[str, ConfirmationUIManager] | None = None,
        request_confirmation_callback: RequestConfirmationCallback | None = None,
        trigger_attachments: list[MessageAttachmentMetadata] | None = None,
        subconversation_id: str | None = None,
        mid_turn_input_provider: MidTurnInputProvider | None = None,
        turn_id: str | None = None,
        thread_root_id: int | None = None,
        trigger_is_internal: bool = False,
        pinned_history_message_ids: list[int] | None = None,
        trigger_role: Literal["user", "system"] = "user",
        save_history_with_isolated_context: bool | None = None,
        initial_taint_sources: Sequence[TaintSource] | None = None,
    ) -> ChatInteractionResult:
        """Send the request to the remote A2A agent and return the result."""
        from family_assistant.processing.types import (  # noqa: PLC0415 - runtime import for .error()
            ChatInteractionResult as CIR,
        )

        context_id = self._context_id(conversation_id, subconversation_id)

        logger.info(
            "Delegating to remote A2A agent '%s' (context_id=%s)",
            self._service_config.id,
            context_id,
        )
        _ = confirmation_ui_managers
        _ = mid_turn_input_provider
        _ = turn_id
        _ = thread_root_id
        _ = trigger_is_internal
        _ = pinned_history_message_ids
        _ = trigger_role
        _ = save_history_with_isolated_context

        metadata: dict[str, object] | None = None
        if initial_taint_sources:
            metadata = _a2a_taint_metadata(initial_taint_sources)

        try:
            task = await self._client.send_message(
                trigger_content_parts,
                context_id=context_id,
                metadata=metadata,
            )
        except A2AClientError as exc:
            logger.error(
                "A2A client error for '%s': %s",
                self._service_config.id,
                exc,
            )
            return CIR.error(
                text_reply=f"Error communicating with remote agent '{self._service_config.id}': {exc}",
                error_traceback=str(exc),
            )

        return a2a_task_to_chat_result(task)

    def _context_id(self, conversation_id: str, subconversation_id: str | None) -> str:
        base = subconversation_id or conversation_id
        return f"{base}:{self._service_config.id}"

    def remote_context_id(
        self, conversation_id: str, subconversation_id: str | None
    ) -> str | None:
        """Deterministic A2A context id, known before submit."""
        return self._context_id(conversation_id, subconversation_id)

    async def submit_async(
        self,
        content_parts: list[ContentPartDict],
        *,
        conversation_id: str,
        subconversation_id: str | None,
        user_name: str,
        db_context: DatabaseContext,
        initial_taint_sources: Sequence[TaintSource] | None = None,
    ) -> RemoteSubmission:
        """Submit to the remote agent without blocking; the remote assigns the id.

        Per A2A spec §3.4.2 a client must not supply a task id when creating a
        task (a supplied id must reference an existing one), so this sends no
        task id and returns the remote-assigned id for the caller to persist and
        poll. If the remote returned a terminal task on submit (a synchronous
        agent that ignored ``blocking=false``), the converted result is returned
        in ``terminal_result`` so the caller can complete without polling.
        ``user_name`` and ``db_context`` are unused: the remote agent's own
        context_id already carries continuity, unlike a local pollable target.
        """
        _ = user_name
        _ = db_context
        context_id = self._context_id(conversation_id, subconversation_id)
        logger.info(
            "Submitting async request to remote A2A agent '%s' (context_id=%s)",
            self._service_config.id,
            context_id,
        )
        metadata = (
            _a2a_taint_metadata(initial_taint_sources)
            if initial_taint_sources
            else None
        )
        task = await self._client.submit(
            content_parts,
            context_id=context_id,
            metadata=metadata,
        )
        terminal_result = None if _is_pending(task) else a2a_task_to_chat_result(task)
        return RemoteSubmission(
            remote_task_id=task.id,
            remote_context_id=task.context_id,
            terminal_result=terminal_result,
        )

    async def poll_async(
        self,
        remote_task_id: str,
        remote_context_id: str | None,
    ) -> ChatInteractionResult | PendingPoll:
        """Poll the remote task once; return PENDING if it is not yet terminal."""
        _ = remote_context_id
        task = await self._client.get_task(remote_task_id)
        if _is_pending(task):
            return PENDING
        return a2a_task_to_chat_result(task)

    async def cancel_async(self, remote_task_id: str) -> None:
        """Best-effort cancellation of the remote task.

        Swallows every (non-cancellation) error, not just ``A2AClientError``: a
        malformed remote response can surface as a JSON/validation error outside
        the A2A error hierarchy, and a failed cancel must never abort the caller
        (which then fails the run) or leave it stranded.
        """
        try:
            await self._client.cancel_task(remote_task_id)
        except Exception as exc:
            logger.warning(
                "Failed to cancel remote A2A task %s on '%s': %s",
                remote_task_id,
                self._service_config.id,
                exc,
            )

    async def close(self) -> None:
        """Close the underlying A2A client."""
        await self._client.close()


def _is_pending(task: Task) -> bool:
    """Whether a remote task is still in progress (non-terminal)."""
    return task.status.state in {TaskState.submitted, TaskState.working}


def _a2a_taint_metadata(
    initial_taint_sources: Sequence[TaintSource],
) -> dict[str, object]:
    state = TurnTaintState.empty()
    for source in initial_taint_sources:
        state = state.add_source(source)
    return {A2A_TAINT_METADATA_KEY: state.to_metadata()}
