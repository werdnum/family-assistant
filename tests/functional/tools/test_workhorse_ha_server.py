"""The workhorse eval's sandbox Home Assistant: a real ``hass`` seeded with a small house.

The house is synthetic. It exercises what the eval depends on: entities served
under the ids, names and states the world gives them, service calls changing
state the way Home Assistant validates them, the registry template functions
answering from the world's areas and devices, the clock set to the case's
moment, recorded history in the recorder, and every call in the service log.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

import httpx
import pytest

from family_assistant.eval.workhorse.ha_server import (
    SERVICE_LOG,
    HomeAssistantServer,
    home_assistant_server,
)
from family_assistant.eval.workhorse.ha_world import (
    HomeWorld,
    StateObservation,
    WorldArea,
    WorldDevice,
    WorldEntity,
)

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path

    from pydantic import JsonValue

pytestmark = pytest.mark.no_db

_MOMENT = datetime(2026, 7, 1, 8, 30, tzinfo=UTC)


def _seen(
    state: str, attributes: dict[str, JsonValue], *, hours_before: float = 1
) -> StateObservation:
    return StateObservation(
        at=_MOMENT - timedelta(hours=hours_before),
        state=state,
        attributes=attributes,
        attributes_complete=True,
        source="test",
    )


_WORLD = HomeWorld(
    location_name="Test House",
    time_zone="Australia/Sydney",
    latitude=-33.8,
    longitude=151.2,
    areas=[WorldArea(area_id="kitchen", name="Kitchen")],
    devices=[
        WorldDevice(
            key="Ceiling Light",
            name="Ceiling Light",
            manufacturer="Acme",
            area="Kitchen",
            integration="hue",
        )
    ],
    entities=[
        WorldEntity(
            entity_id="light.kitchen_ceiling",
            name="Kitchen Ceiling",
            integration="hue",
            device="Ceiling Light",
            observations=[
                _seen("on", {"brightness": 200}, hours_before=3),
                _seen(
                    "off",
                    {
                        "supported_color_modes": ["brightness"],
                        "friendly_name": "Kitchen Ceiling",
                    },
                ),
            ],
        ),
        WorldEntity(
            entity_id="sensor.kitchen_temperature",
            name="Kitchen Temperature",
            area="Kitchen",
            observations=[
                _seen(
                    "21.5",
                    {
                        "unit_of_measurement": "°C",
                        "device_class": "temperature",
                        "state_class": "measurement",
                    },
                )
            ],
        ),
        WorldEntity(
            entity_id="input_boolean.guest_mode",
            name="Guest mode",
            observations=[_seen("on", {})],
        ),
    ],
)


@pytest.fixture(scope="module")
def house(
    tmp_path_factory: pytest.TempPathFactory,
) -> Iterator[tuple[HomeAssistantServer, Path]]:
    work_dir = tmp_path_factory.mktemp("ha")
    with home_assistant_server(_WORLD, _MOMENT, work_dir) as server:
        yield server, work_dir


def _client(server: HomeAssistantServer) -> httpx.Client:
    return httpx.Client(
        base_url=server.base_url,
        headers={"Authorization": f"Bearer {server.token}"},
        timeout=30,
    )


def test_entities_are_served_with_their_state_at_the_moment(
    house: tuple[HomeAssistantServer, Path],
) -> None:
    server, _ = house
    with _client(server) as client:
        sensor = client.get("/api/states/sensor.kitchen_temperature").json()
        helper = client.get("/api/states/input_boolean.guest_mode").json()

    assert sensor["state"] == "21.5"
    assert sensor["attributes"]["unit_of_measurement"] == "°C"
    assert sensor["attributes"]["friendly_name"] == "Kitchen Temperature"
    assert helper["state"] == "on"


def test_a_service_call_changes_state_and_is_logged(
    house: tuple[HomeAssistantServer, Path],
) -> None:
    server, work_dir = house
    with _client(server) as client:
        response = client.post(
            "/api/services/light/turn_on",
            json={"entity_id": "light.kitchen_ceiling", "brightness_pct": 50},
        )
        response.raise_for_status()
        light = client.get("/api/states/light.kitchen_ceiling").json()

    assert light["state"] == "on"
    assert light["attributes"]["brightness"] == 128
    calls = [
        json.loads(line)
        for line in (work_dir / SERVICE_LOG).read_text(encoding="utf-8").splitlines()
    ]
    assert {"domain": "light", "service": "turn_on"}.items() <= calls[-1].items()


def test_templates_answer_from_areas_devices_and_the_moment(
    house: tuple[HomeAssistantServer, Path],
) -> None:
    server, _ = house
    template = (
        "{{ area_entities('Kitchen') | sort | join(',') }}|"
        "{{ device_attr(device_id('light.kitchen_ceiling'), 'manufacturer') }}|"
        "{{ integration_entities('hue') | join(',') }}|"
        "{{ now().date() }}"
    )
    with _client(server) as client:
        rendered = client.post("/api/template", json={"template": template}).text

    assert rendered == (
        "light.kitchen_ceiling,sensor.kitchen_temperature|Acme|"
        "light.kitchen_ceiling|2026-07-01"
    )


def test_recorded_history_is_in_the_recorder(
    house: tuple[HomeAssistantServer, Path],
) -> None:
    server, _ = house
    start = (_MOMENT - timedelta(hours=4)).isoformat()
    with _client(server) as client:
        history = client.get(
            f"/api/history/period/{start}",
            params={
                "filter_entity_id": "light.kitchen_ceiling",
                "end_time": (_MOMENT - timedelta(minutes=1)).isoformat(),
                "minimal_response": "",
            },
        ).json()

    assert [point["state"] for point in history[0]] == ["on", "off"]
