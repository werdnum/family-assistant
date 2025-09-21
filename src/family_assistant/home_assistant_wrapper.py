"""
Wrapper for Home Assistant API client to handle special cases like binary responses.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

import aiohttp

if TYPE_CHECKING:
    import homeassistant_api

logger = logging.getLogger(__name__)


class HomeAssistantClientWrapper:
    """
    Wrapper around homeassistant_api.Client that provides additional functionality.

    This wrapper stores the raw API URL and token to make direct HTTP requests
    when needed (e.g., for binary responses that the library can't handle).
    """

    def __init__(
        self,
        api_url: str,
        token: str,
        client: homeassistant_api.Client,
        verify_ssl: bool = True,
    ) -> None:
        """
        Initialize the wrapper.

        Args:
            api_url: Base URL of Home Assistant (e.g., "http://localhost:8123")
            token: Long-lived access token
            client: The underlying homeassistant_api.Client instance
            verify_ssl: Whether to verify SSL certificates
        """
        self.api_url = api_url.rstrip("/")  # Store base URL without trailing slash
        self.token = token
        self._client = client
        self.verify_ssl = verify_ssl

    # Delegate methods to underlying client
    async def async_get_rendered_template(self, template: str) -> str:
        """
        Render a Home Assistant Jinja2 template.

        Args:
            template: The Jinja2 template string

        Returns:
            The rendered template result
        """
        return await self._client.async_get_rendered_template(template=template)

    async def async_get_states(self) -> tuple[Any, ...]:
        """
        Get all entity states from Home Assistant.

        Returns:
            List of entity states
        """
        return await self._client.async_get_states()

    async def async_request(self, method: str, path: str, **kwargs: Any) -> Any:  # noqa: ANN401
        """
        Make a request using the underlying client.

        This method exists for compatibility but should be avoided for binary responses.

        Args:
            method: HTTP method
            path: API path
            **kwargs: Additional arguments

        Returns:
            The response from the API
        """
        return await self._client.async_request(method=method, path=path, **kwargs)

    async def async_get_camera_snapshot(self, camera_entity_id: str) -> bytes:
        """
        Get raw binary camera snapshot without text decoding.

        This method bypasses the homeassistant_api library's response processing
        to get raw binary data, which is necessary for image content.

        Args:
            camera_entity_id: The entity ID of the camera

        Returns:
            Raw bytes of the camera snapshot image

        Raises:
            aiohttp.ClientError: If the request fails
        """

        # Build the full URL for the camera proxy endpoint
        url = f"{self.api_url}/api/camera_proxy/{camera_entity_id}"
        headers = {
            "Authorization": f"Bearer {self.token}",
            "Content-Type": "application/json",
        }

        # Create connector based on SSL verification setting
        connector = None
        if not self.verify_ssl:
            connector = aiohttp.TCPConnector(ssl=False)

        timeout = aiohttp.ClientTimeout(total=30)

        async with (
            aiohttp.ClientSession(connector=connector, timeout=timeout) as session,
            session.get(url, headers=headers) as response,
        ):
            response.raise_for_status()
            return await response.read()
