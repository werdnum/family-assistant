"""Unit tests for the Home Assistant client wrapper."""

from datetime import UTC, datetime
from typing import cast
from unittest.mock import AsyncMock, MagicMock

import homeassistant_api
import pytest
from homeassistant_api.models.domains import Domain

from family_assistant.plugins.home_assistant.client import HomeAssistantClientWrapper


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


@pytest.mark.asyncio
async def test_entity_histories_filters_by_entity_id() -> None:
    """History is requested for exactly the given entity ids over the window."""
    client = homeassistant_api.Client(
        api_url="http://home-assistant.test:8123/api",
        token="test-token",
        use_async=True,
    )
    request = AsyncMock(
        return_value=[
            [
                {"entity_id": "person.test", "state": "home"},
                {"entity_id": "person.test", "state": "away"},
            ]
        ]
    )
    client.async_request = request  # type: ignore[method-assign]
    wrapper = HomeAssistantClientWrapper(
        api_url="http://home-assistant.test:8123", token="test-token", client=client
    )
    start = datetime(2026, 9, 23, 12, 0, tzinfo=UTC)
    end = datetime(2026, 9, 30, 12, 0, tzinfo=UTC)

    histories = await wrapper.async_get_entity_histories(
        ["person.test"], start_timestamp=start, end_timestamp=end
    )

    assert [h.entity_id for h in histories] == ["person.test"]
    assert [s.state for s in histories[0].states] == ["home", "away"]
    (url,), kwargs = request.call_args
    assert url == "history/period/2026-09-23T12:00:00+00:00"
    assert "filter_entity_id=person.test" in kwargs["params"]
    assert "end_time=2026-09-30T12%3A00%3A01%2B00%3A00" in kwargs["params"]
