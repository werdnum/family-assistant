"""
Wrapper for Home Assistant API client to handle special cases like binary responses.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime  # noqa: TC003 - needed at runtime for parsing timestamps
from typing import TYPE_CHECKING, Any

import aiohttp

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator

    import homeassistant_api

logger = logging.getLogger(__name__)


@dataclass
class StateItem:
    """Represents a single state in entity history."""

    state: str
    # ast-grep-ignore: no-dict-any - HA state attributes are dynamic
    attributes: dict[str, Any]
    last_changed: datetime | None
    last_updated: datetime | None


@dataclass
class HistoryItem:
    """Represents history for a single entity."""

    entity_id: str
    states: list[StateItem]


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

    async def async_get_entity_histories(
        self,
        entities: list[str] | None = None,
        start_timestamp: datetime | None = None,
        end_timestamp: datetime | None = None,
        significant_changes_only: bool = False,
    ) -> AsyncGenerator[HistoryItem, None]:
        """
        Get entity state histories from Home Assistant.

        Args:
            entities: Optional list of entity IDs to retrieve history for
            start_timestamp: Optional start of history period
            end_timestamp: Optional end of history period
            significant_changes_only: If true, only significant state changes

        Yields:
            History objects containing entity state history

        Raises:
            Exception: If the history retrieval fails
        """
        # Build the path - if start_timestamp provided, use it in path
        if start_timestamp:
            start_iso = start_timestamp.isoformat()
            path = f"/history/period/{start_iso}"
        else:
            path = "/history/period"

        # Build query parameters
        # ast-grep-ignore: no-dict-any - API params require flexibility
        params: dict[str, Any] = {}
        if entities:
            params["filter_entity_id"] = ",".join(entities)
        if end_timestamp:
            params["end_time"] = end_timestamp.isoformat()
        if significant_changes_only:
            params["significant_changes_only"] = "1"

        # Make the request using the underlying client
        # The API returns a list of lists, where each inner list contains state history for one entity
        response = await self.async_request(method="get", path=path, params=params)

        # Response is a list of lists of state dicts
        # Each top-level list item represents one entity's history
        if not isinstance(response, list):
            return

        for entity_history in response:
            if not entity_history:  # Skip empty histories
                continue

            # Extract entity_id from first state
            entity_id = entity_history[0].get("entity_id", "") if entity_history else ""

            # Convert state dicts to StateItem objects
            states = []
            for state_dict in entity_history:
                # Parse timestamps
                last_changed_str = state_dict.get("last_changed")
                last_updated_str = state_dict.get("last_updated")

                last_changed = (
                    datetime.fromisoformat(last_changed_str.replace("Z", "+00:00"))
                    if last_changed_str
                    else None
                )
                last_updated = (
                    datetime.fromisoformat(last_updated_str.replace("Z", "+00:00"))
                    if last_updated_str
                    else None
                )

                state_item = StateItem(
                    state=state_dict.get("state", ""),
                    attributes=state_dict.get("attributes", {}),
                    last_changed=last_changed,
                    last_updated=last_updated,
                )
                states.append(state_item)

            yield HistoryItem(entity_id=entity_id, states=states)
