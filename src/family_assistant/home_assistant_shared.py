"""
Shared Home Assistant client management.
"""

import logging
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from family_assistant.home_assistant_wrapper import HomeAssistantClientWrapper

logger = logging.getLogger(__name__)


def create_home_assistant_client(
    api_url: str,
    token: str,
    verify_ssl: bool = True,
) -> "HomeAssistantClientWrapper | None":
    """
    Create a shared Home Assistant client instance.

    Args:
        api_url: Base URL of Home Assistant (e.g., "http://localhost:8123")
        token: Long-lived access token
        verify_ssl: Whether to verify SSL certificates

    Returns:
        Home Assistant client wrapper instance or None if library not available
    """
    try:
        import homeassistant_api  # noqa: PLC0415 - optional dependency import
    except ImportError:
        logger.warning(
            "homeassistant_api library is not installed. Home Assistant features will be disabled."
        )
        return None

    # The homeassistant_api.Client expects the URL to include /api
    ha_api_url_with_path = api_url.rstrip("/") + "/api"

    client = homeassistant_api.Client(
        api_url=ha_api_url_with_path,
        token=token,
        use_async=True,  # Required for async context provider
        verify_ssl=verify_ssl,
    )

    # Import the wrapper at runtime to avoid circular imports
    from family_assistant.home_assistant_wrapper import (  # noqa: PLC0415
        HomeAssistantClientWrapper,
    )

    # Create wrapper with the original api_url (without /api) and the raw client
    wrapper = HomeAssistantClientWrapper(
        api_url=api_url,  # Use original URL without /api suffix
        token=token,
        client=client,
        verify_ssl=verify_ssl,
    )

    logger.info(f"Created Home Assistant client wrapper for URL: {api_url}")
    return wrapper
