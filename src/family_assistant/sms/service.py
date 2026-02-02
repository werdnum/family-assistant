import logging
from typing import TYPE_CHECKING, cast

from family_assistant.interfaces import ChatInterface
from family_assistant.llm.messages import ContentPartDict, text_content
from family_assistant.sms.interface import SMSChatInterface
from family_assistant.sms.protocol import SMSBackend

if TYPE_CHECKING:
    from family_assistant.processing import ProcessingService
    from family_assistant.storage.context import DatabaseContext

logger = logging.getLogger(__name__)


class SMSService:
    """Service to coordinate SMS inbound and outbound interactions."""

    def __init__(
        self,
        backend: SMSBackend,
        processing_service: "ProcessingService",
        allowed_numbers: list[str] | None = None,
        profile_id: str = "sms_assistant",
        processing_services_registry: dict[str, "ProcessingService"] | None = None,
    ) -> None:
        self.backend = backend
        self.processing_service = processing_service
        self.allowed_numbers = allowed_numbers
        self.profile_id = profile_id
        self.processing_services_registry = processing_services_registry
        self.chat_interface = SMSChatInterface(self)

    async def handle_inbound_sms(
        self,
        db_context: "DatabaseContext",
        from_number: str,
        to_number: str,
        text: str,
        chat_interfaces: dict[str, ChatInterface] | None = None,
    ) -> None:
        """
        Processes an incoming SMS message.
        """
        logger.info(f"Received SMS from {from_number} to {to_number}: {text}")

        if self.allowed_numbers and from_number not in self.allowed_numbers:
            logger.warning(f"Ignoring SMS from unauthorized number: {from_number}")
            return

        # Prepare for processing
        interface_type = "sms"
        conversation_id = from_number  # Use sender's number as conversation ID

        # Select the correct processing service
        selected_processing_service = self.processing_service
        if (
            self.processing_services_registry
            and self.profile_id in self.processing_services_registry
        ):
            selected_processing_service = self.processing_services_registry[
                self.profile_id
            ]

        trigger_content_parts = [text_content(text)]

        # Call handle_chat_interaction
        result = await selected_processing_service.handle_chat_interaction(
            db_context=db_context,
            interface_type=interface_type,
            conversation_id=conversation_id,
            trigger_content_parts=cast("list[ContentPartDict]", trigger_content_parts),
            trigger_interface_message_id=None,  # We don't have a reliable message ID from SMS usually
            user_name=f"User {from_number}",
            replied_to_interface_id=None,
            chat_interface=self.chat_interface,
            chat_interfaces=chat_interfaces,
            trigger_attachments=None,
        )

        if result.text_reply:
            logger.info(f"Sending SMS reply to {from_number}")
            await self.chat_interface.send_message(
                conversation_id=conversation_id, text=result.text_reply
            )
        elif result.error_traceback:
            logger.error(
                f"Error processing SMS from {from_number}: {result.error_traceback}"
            )
            await self.chat_interface.send_message(
                conversation_id=conversation_id,
                text="Sorry, I encountered an error while processing your request.",
            )
