"""Unit tests for the Home Assistant client wrapper."""

from typing import cast
from unittest.mock import AsyncMock, MagicMock

import homeassistant_api
import pytest
from homeassistant_api.models.domains import Domain

from family_assistant.home_assistant_wrapper import HomeAssistantClientWrapper


def _make_wrapper(
    client_mock: MagicMock,
) -> HomeAssistantClientWrapper:
    return HomeAssistantClientWrapper(
        api_url="http://home-assistant.test:8123",
        token="test-token",
        client=cast("homeassistant_api.Client", client_mock),
    )


@pytest.mark.asyncio
async def test_action_catalog_supports_media_selector_multiple() -> None:
    """The upstream service model accepts HA's media selector multiple field."""
    client_mock = MagicMock(spec=homeassistant_api.Client)
    domain = Domain.from_json(
        {
            "domain": "media_player",
            "services": {
                "play_media": {
                    "fields": {
                        "media_content_id": {"selector": {"media": {"multiple": False}}}
                    }
                }
            },
        },
        client=cast("homeassistant_api.Client", client_mock),
    )
    client_mock.async_get_domains = AsyncMock(return_value={"media_player": domain})

    catalog = await _make_wrapper(client_mock).async_get_action_catalog()

    assert catalog[0]["fields"]["media_content_id"]["selector"]["media"] == {
        "multiple": False
    }
