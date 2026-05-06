"""Remote A2A service implementing DelegatableService.

Translates handle_chat_interaction calls into A2A message/send
requests to a remote agent.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Literal

from family_assistant.a2a.client import A2AClientError
from family_assistant.a2a.result_converter import a2a_task_to_chat_result

if TYPE_CHECKING:
    from family_assistant.a2a.client import A2AClientWrapper
    from family_assistant.interfaces import ChatInterface
    from family_assistant.llm.content_parts import ContentPartDict
    from family_assistant.llm.messages import MessageAttachmentMetadata
    from family_assistant.processing.types import (
        ChatInteractionResult,
        RemoteServiceConfig,
        RequestConfirmationCallback,
    )
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
    ) -> ChatInteractionResult:
        """Send the request to the remote A2A agent and return the result."""
        from family_assistant.processing.types import (  # noqa: PLC0415 - runtime import for .error()
            ChatInteractionResult as CIR,
        )

        context_id = (
            f"{subconversation_id}:{self._service_config.id}"
            if subconversation_id
            else f"{conversation_id}:{self._service_config.id}"
        )

        logger.info(
            "Delegating to remote A2A agent '%s' (context_id=%s)",
            self._service_config.id,
            context_id,
        )
        _ = confirmation_ui_managers

        try:
            task = await self._client.send_message(
                trigger_content_parts,
                context_id=context_id,
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

    async def close(self) -> None:
        """Close the underlying A2A client."""
        await self._client.close()
