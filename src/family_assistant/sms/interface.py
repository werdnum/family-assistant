import logging
from typing import TYPE_CHECKING

from family_assistant.interfaces import ChatInterface

if TYPE_CHECKING:
    from family_assistant.sms.service import SMSService

logger = logging.getLogger(__name__)


class SMSChatInterface(ChatInterface):
    """ChatInterface implementation for SMS."""

    def __init__(self, sms_service: "SMSService") -> None:
        self.sms_service = sms_service

    async def send_message(
        self,
        conversation_id: str,
        text: str,
        parse_mode: str | None = None,
        reply_to_interface_id: str | None = None,
        attachment_ids: list[str] | None = None,
    ) -> str | None:
        """
        Sends an SMS message to the specified phone number (conversation_id).
        """
        # SMS doesn't support rich formatting or attachments natively in a simple way
        # So we just send the text.
        return await self.sms_service.backend.send_sms(
            to_number=conversation_id, message=text
        )
