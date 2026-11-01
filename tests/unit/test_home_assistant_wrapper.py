"""Unit tests for the Home Assistant client wrapper."""

from collections.abc import AsyncIterator
from datetime import UTC, datetime
from unittest.mock import patch

import homeassistant_api
import pytest
import pytest_asyncio
from homeassistant_api.models.domains import Domain

from family_assistant.plugins.home_assistant.client import HomeAssistantClientWrapper
from tests.mocks.home_assistant_client import (
    make_home_assistant_client,
    serve_entities,
)


@pytest_asyncio.fixture
async def client() -> AsyncIterator[homeassistant_api.Client]:
    """A real async SDK client; it is never contacted, and its session is closed."""
    real_client = homeassistant_api.Client(
        api_url="http://home-assistant.test:8123/api",
        token="test-token",
        use_async=True,
    )
    yield real_client
    await real_client.async_cache_session.close()


@pytest.mark.asyncio
async def test_action_catalog_supports_media_selector_multiple(
    client: homeassistant_api.Client,
) -> None:
    """The upstream service model accepts HA's media selector multiple field."""
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
        client=client,
    )
    wrapper = HomeAssistantClientWrapper(
        api_url="http://home-assistant.test:8123", token="test-token", client=client
    )

    with patch.object(
        client,
        "async_get_domains",
        autospec=True,
        return_value={"media_player": domain},
    ):
        catalog = await wrapper.async_get_action_catalog()

    assert catalog[0]["fields"]["media_content_id"]["selector"]["media"] == {
        "multiple": False
    }


@pytest.mark.asyncio
async def test_entity_histories_filters_by_entity_id(
    client: homeassistant_api.Client,
) -> None:
    """History is requested for exactly the given entity ids over the window."""
    wrapper = HomeAssistantClientWrapper(
        api_url="http://home-assistant.test:8123", token="test-token", client=client
    )
    start = datetime(2026, 9, 23, 12, 0, tzinfo=UTC)
    end = datetime(2026, 9, 30, 12, 0, tzinfo=UTC)

    with patch.object(
        client,
        "async_request",
        autospec=True,
        return_value=[
            [
                {"entity_id": "person.test", "state": "home"},
                {"entity_id": "person.test", "state": "away"},
            ]
        ],
    ) as request:
        histories = await wrapper.async_get_entity_histories(
            ["person.test"], start_timestamp=start, end_timestamp=end
        )

    assert [h.entity_id for h in histories] == ["person.test"]
    assert [s.state for s in histories[0].states] == ["home", "away"]
    (url,), kwargs = request.call_args
    assert url == "history/period/2026-09-23T12:00:00+00:00"
    assert "filter_entity_id=person.test" in kwargs["params"]
    assert "end_time=2026-09-30T12%3A00%3A01%2B00%3A00" in kwargs["params"]


@pytest.mark.asyncio
async def test_entity_list_fits_home_assistant_output_limit_on_a_large_install() -> (
    None
):
    """Every entity is listed even when one render of them all would exceed HA's limit.

    Home Assistant rejects a template whose output is longer than
    MAX_TEMPLATE_OUTPUT_CHARS; the fake renderer enforces the same limit.
    """
    entities = [
        {
            "entity_id": f"sensor.upstairs_bedroom_climate_sensor_{n}_temperature",
            "name": f"Upstairs Bedroom Climate Sensor {n} Temperature",
            "area_name": "Upstairs Bedroom" if n % 2 else None,
            "device_id": f"{n:032x}",
            "device_name": f"Upstairs Bedroom Climate Sensor {n}",
            "device_class": "temperature" if n % 3 else None,
        }
        for n in range(2500)
    ]
    home_assistant = make_home_assistant_client()
    serve_entities(home_assistant.api, entities)

    listed = await home_assistant.wrapper.async_get_entity_list_with_metadata()

    assert sorted(listed, key=lambda e: e["entity_id"]) == sorted(
        entities, key=lambda e: str(e["entity_id"])
    )
