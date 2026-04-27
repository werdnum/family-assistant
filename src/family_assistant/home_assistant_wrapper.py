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


# Home Assistant action payloads and entity state dicts have arbitrary,
# action-specific shapes (any HA service or response field). We expose them
# as plain JSON-compatible dicts because the LLM constructs them dynamically
# and we just pass them through to/from the HA API.
# ast-grep-ignore: no-dict-any - HA action payloads are action-specific
ActionPayload = dict[str, Any]
# ast-grep-ignore: no-dict-any - HA state dicts have arbitrary attribute payloads
HAStateDict = dict[str, Any]


class ActionCallResult(TypedDict):
    """Result of a Home Assistant action (service) call."""

    changed_states: list[HAStateDict]
    response: ActionPayload


class ActionCatalogEntry(TypedDict):
    """A single available action discovered from Home Assistant.

    Sourced live from ``GET /api/services``, so it always matches the
    integrations currently installed on the connected HA instance.
    """

    domain: str
    action: str
    name: str | None
    description: str | None
    # ast-grep-ignore: no-dict-any - HA action field schemas are action-specific
    fields: dict[str, Any]
    # ast-grep-ignore: no-dict-any - HA target selector block is action-specific
    target: dict[str, Any] | None
    supports_response: bool


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

    async def async_call_action(
        self,
        domain: str,
        action: str,
        service_data: ActionPayload | None = None,
        *,
        return_response: bool = False,
    ) -> ActionCallResult:
        """
        Call a Home Assistant action (formerly known as a "service call").

        Args:
            domain: The action domain (e.g., "light", "switch", "climate").
            action: The action name within the domain (e.g., "turn_on").
            service_data: Optional payload for the action. May include
                ``entity_id`` (str or list), a ``target`` block, and any
                action-specific fields.
            return_response: If True, request the action's response payload
                from Home Assistant (only supported for actions that declare
                ``supports_response``).

        Returns:
            A dict with keys:
              - ``changed_states``: list of entity state dicts that changed
              - ``response``: the action response payload (only when
                ``return_response`` is True; otherwise an empty dict).
        """
        payload = dict(service_data) if service_data else {}

        if return_response:
            states, response = await self._client.async_trigger_service_with_response(
                domain=domain,
                service=action,
                **payload,
            )
        else:
            states = await self._client.async_trigger_service(
                domain=domain,
                service=action,
                **payload,
            )
            response = {}

        changed_states = [json.loads(state.model_dump_json()) for state in states]
        return ActionCallResult(
            changed_states=changed_states,
            response=dict(response),
        )

    async def async_get_action_catalog(
        self, *, domain: str | None = None
    ) -> list[ActionCatalogEntry]:
        """Fetch the live catalog of available actions from Home Assistant.

        The catalog is sourced directly from ``GET /api/services`` so it always
        reflects the integrations currently installed on the connected HA
        instance — there is no static schema to keep in sync.

        Args:
            domain: Optional domain to narrow the result (e.g. ``"light"``).
                When omitted, every domain on the HA instance is returned.

        Returns:
            A list of :py:class:`ActionCatalogEntry` dicts. Each entry includes
            the action's ``domain``, ``action`` (a.k.a. service id),
            ``description``, the ``fields`` schema (the per-parameter selectors
            HA exposes), the optional ``target`` selector block, and a
            ``supports_response`` flag indicating whether ``return_response``
            is meaningful for this action.
        """
        domains = await self._client.async_get_domains()
        entries: list[ActionCatalogEntry] = []
        for domain_obj in domains.values():
            if domain is not None and domain_obj.domain_id != domain:
                continue
            for service in domain_obj.services.values():
                # ast-grep-ignore: no-dict-any - HA action field schemas are action-specific JSON payloads
                fields_dump: dict[str, Any] = {}
                if service.fields:
                    for field_name, field_value in service.fields.items():
                        fields_dump[field_name] = json.loads(
                            field_value.model_dump_json(exclude_none=True)
                        )
                # ast-grep-ignore: no-dict-any - HA target selector block is action-specific JSON
                target_dump: dict[str, Any] | None = None
                if service.target is not None:
                    target_dump = json.loads(
                        service.target.model_dump_json(exclude_none=True)
                    )
                response_dump = (
                    json.loads(service.response.model_dump_json(exclude_none=True))
                    if service.response is not None
                    else None
                )
                entries.append(
                    ActionCatalogEntry(
                        domain=domain_obj.domain_id,
                        action=service.service_id,
                        name=service.name,
                        description=service.description,
                        fields=fields_dump,
                        target=target_dump,
                        supports_response=response_dump is not None,
                    )
                )
        entries.sort(key=lambda e: (e["domain"], e["action"]))
        return entries

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
