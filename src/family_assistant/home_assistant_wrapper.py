"""
Wrapper for Home Assistant API client to handle special cases like binary responses.
"""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, TypedDict

import aiohttp

if TYPE_CHECKING:
    import homeassistant_api

logger = logging.getLogger(__name__)


class EntityMetadata(TypedDict):
    """Home Assistant entity with metadata."""

    entity_id: str
    name: str
    area_name: str | None
    device_id: str | None
    device_name: str | None


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
        entity_cache_ttl_seconds: int = 120,
    ) -> None:
        """
        Initialize the wrapper.

        Args:
            api_url: Base URL of Home Assistant (e.g., "http://localhost:8123")
            token: Long-lived access token
            client: The underlying homeassistant_api.Client instance
            verify_ssl: Whether to verify SSL certificates
            entity_cache_ttl_seconds: TTL for entity list cache in seconds (default: 120)
        """
        self.api_url = api_url.rstrip("/")  # Store base URL without trailing slash
        self.token = token
        self._client = client
        self.verify_ssl = verify_ssl
        self._entity_cache_ttl_seconds = entity_cache_ttl_seconds

        # Cache for entity list (populated by async_get_entity_list_with_metadata)
        self._entity_cache: list[EntityMetadata] | None = None
        self._entity_cache_timestamp: datetime | None = None
        self._entity_cache_lock = asyncio.Lock()

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

    async def async_get_entity_list_with_metadata(
        self, *, force_refresh: bool = False
    ) -> list[EntityMetadata]:
        """
        Get list of all entities with metadata (area, device, etc.) using template rendering.

        Results are cached for the configured TTL to avoid repeated template renders.
        Uses a lock to prevent concurrent cache refreshes (thundering herd problem).

        Args:
            force_refresh: If True, bypass cache and fetch fresh data

        Returns:
            List of entity dictionaries with keys: entity_id, name, area_name, device_id, device_name

        Raises:
            Exception: If template rendering fails
        """
        now = datetime.now(UTC)

        # Fast path: Check if cache is valid without lock
        if (
            not force_refresh
            and self._entity_cache is not None
            and self._entity_cache_timestamp is not None
        ):
            age_seconds = (now - self._entity_cache_timestamp).total_seconds()
            if age_seconds < self._entity_cache_ttl_seconds:
                logger.debug(
                    f"Using cached entity list (age: {age_seconds:.1f}s, TTL: {self._entity_cache_ttl_seconds}s)"
                )
                return self._entity_cache  # type: ignore[return-value]

        # Slow path: Acquire lock and refresh cache
        async with self._entity_cache_lock:
            # Double-check cache validity after acquiring lock
            # (another coroutine might have refreshed it)
            now = datetime.now(UTC)
            if (
                not force_refresh
                and self._entity_cache is not None
                and self._entity_cache_timestamp is not None
            ):
                age_seconds = (now - self._entity_cache_timestamp).total_seconds()
                if age_seconds < self._entity_cache_ttl_seconds:
                    logger.debug(
                        f"Using cached entity list (refreshed by another coroutine, age: {age_seconds:.1f}s)"
                    )
                    return self._entity_cache  # type: ignore[return-value]

            # Fetch entities via template rendering
            template = """[
{% for state in states %}
  {
    "entity_id": "{{ state.entity_id }}",
    "name": {{ state.name | tojson }},
    "area_name": {{ area_name(state.entity_id) | default(none, true) | tojson }},
    "device_id": {{ device_id(state.entity_id) | default(none, true) | tojson }},
    "device_name": {{ device_name(state.entity_id) | default(none, true) | tojson }}
  }{% if not loop.last %},{% endif %}
{% endfor %}
]"""

            logger.debug("Fetching entity list via template rendering")
            rendered_json = await self.async_get_rendered_template(template=template)
            entities = json.loads(rendered_json)

            # Update cache
            self._entity_cache = entities
            self._entity_cache_timestamp = now
            logger.info(
                f"Cached {len(entities)} entities (TTL: {self._entity_cache_ttl_seconds}s)"
            )

            return entities

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
