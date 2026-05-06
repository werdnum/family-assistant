"""Protocol for services that can receive delegated requests.

Both local ProcessingService and remote A2A services implement this
protocol, allowing the delegation tool and registry to work with either.
"""

from __future__ import annotations

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
