"""Protocol for services that can receive delegated requests.

Both local ProcessingService and remote A2A services implement this
protocol, allowing the delegation tool and registry to work with either.
"""

from __future__ import annotations

import enum
from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal, Protocol, runtime_checkable

if TYPE_CHECKING:
    from family_assistant.interfaces import ChatInterface
    from family_assistant.llm.content_parts import ContentPartDict
    from family_assistant.llm.messages import MessageAttachmentMetadata
    from family_assistant.processing.types import (
        ChatInteractionResult,
        ProcessingServiceConfig,
        RemoteServiceConfig,
        RequestConfirmationCallback,
    )
    from family_assistant.storage.context import DatabaseContext
    from family_assistant.telegram.protocols import ConfirmationUIManager


@runtime_checkable
class DelegatableService(Protocol):
    """A service that can receive delegated requests.

    Implemented by both ProcessingService (local) and RemoteA2AService (remote).
    The delegate_to_service tool and registry use this interface.
    """

    @property
    def kind(self) -> Literal["local", "remote"]: ...

    @property
    def service_config(self) -> ProcessingServiceConfig | RemoteServiceConfig: ...

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
    ) -> ChatInteractionResult: ...


class PendingPoll(enum.Enum):
    """Sentinel returned by ``poll_async`` when the remote task is not terminal."""

    PENDING = "pending"


PENDING = PendingPoll.PENDING


@dataclass
class RemoteSubmission:
    """Result of submitting a request to a remote service without blocking.

    ``terminal_result`` is populated only when the remote returned a terminal
    task on submit (a synchronous remote that ignored ``blocking=false``), in
    which case the caller can complete immediately without polling.
    """

    remote_task_id: str
    remote_context_id: str | None
    terminal_result: ChatInteractionResult | None = None


@runtime_checkable
class PollableDelegationService(Protocol):
    """A delegatable service whose work runs remotely and is polled to terminal.

    Implemented by remote services (RemoteA2AService) that submit a request,
    return a remote task id, and are polled by the worker until the task is
    terminal — so a delegated run does not hold a worker for the whole remote
    duration and can re-attach after a restart. Local services do not implement
    this; the worker checks for the capability and falls back to the inline path.
    """

    @property
    def service_config(self) -> ProcessingServiceConfig | RemoteServiceConfig: ...

    def remote_context_id(
        self, conversation_id: str, subconversation_id: str | None
    ) -> str | None:
        """Deterministic remote context id for a delegation, known before submit."""
        ...

    async def submit_async(
        self,
        content_parts: list[ContentPartDict],
        *,
        conversation_id: str,
        subconversation_id: str | None,
    ) -> RemoteSubmission:
        """Submit without a client-supplied task id; the remote assigns one.

        Per A2A spec §3.4.2 a client must not supply a task id when creating a
        task, so the returned :class:`RemoteSubmission` carries the remote's
        assigned id for the caller to persist and poll.
        """
        ...

    async def poll_async(
        self,
        remote_task_id: str,
        remote_context_id: str | None,
    ) -> ChatInteractionResult | PendingPoll: ...

    async def cancel_async(self, remote_task_id: str) -> None: ...
