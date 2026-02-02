from typing import Protocol


class SMSBackend(Protocol):
    """Protocol defining the interface for SMS backends."""

    async def send_sms(self, to_number: str, message: str) -> str | None:
        """
        Sends an SMS message.

        Args:
            to_number: The destination phone number.
            message: The message text to send.

        Returns:
            The backend-specific message ID if successful, else None.
        """
        ...
