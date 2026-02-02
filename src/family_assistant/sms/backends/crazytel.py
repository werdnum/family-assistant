import logging

import httpx

from family_assistant.sms.protocol import SMSBackend

logger = logging.getLogger(__name__)


class CrazyTelBackend(SMSBackend):
    """CrazyTel SMS backend implementation."""

    def __init__(self, api_key: str, from_number: str) -> None:
        self.api_key = api_key
        self.from_number = from_number
        self.api_url = "https://sms.crazytel.net.au/api/v1/sms/send"

    async def send_sms(self, to_number: str, message: str) -> str | None:
        """Sends an SMS via CrazyTel API."""
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }
        payload = {
            "to": to_number,
            "from": self.from_number,
            "message": message,
        }

        async with httpx.AsyncClient() as client:
            try:
                response = await client.post(
                    self.api_url, headers=headers, json=payload
                )
                response.raise_for_status()
                data = response.json()
                # CrazyTel response format for success is usually like {"status":"success","message_id":"..."}
                # Based on the documentation snippet provided, I'll assume success if status_code is 2xx.
                logger.info(f"SMS sent successfully to {to_number}")
                return data.get("message_id") or "success"
            except httpx.HTTPStatusError as e:
                logger.error(f"Failed to send SMS to {to_number}: {e.response.text}")
                return None
            except Exception as e:
                logger.error(f"Error sending SMS to {to_number}: {e}")
                return None
