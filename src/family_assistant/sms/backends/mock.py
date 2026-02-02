import logging

from family_assistant.sms.protocol import SMSBackend

logger = logging.getLogger(__name__)


class MockSMSBackend(SMSBackend):
    """Mock SMS backend for testing."""

    async def send_sms(self, to_number: str, message: str) -> str | None:
        """Logs the SMS instead of sending it."""
        logger.info(f"[MOCK SMS] To: {to_number}, Message: {message}")
        return "mock_id"
