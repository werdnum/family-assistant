"""The WillyWeather forecast service, the get_weather_forecast tool and the provider."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING
from unittest.mock import MagicMock
from zoneinfo import ZoneInfo

import httpx
import pytest
from pydantic import SecretStr

from family_assistant.config_loader import load_config
from family_assistant.services.effective_tool_registry import (
    build_effective_local_tool_registrations,
)
from family_assistant.services.oauth_integration_state import OAuthIntegrationState
from family_assistant.storage.database import Database
from family_assistant.tools import LocalToolsProvider
from family_assistant.tools.types import ToolExecutionContext, ToolResult
from family_assistant.utils.clock import MockClock
from family_assistant.weather import WeatherService

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Mapping
    from pathlib import Path

    from family_assistant.config_models import AppConfig
    from family_assistant.tools import ToolRegistration

pytestmark = pytest.mark.no_db

SYDNEY = ZoneInfo("Australia/Sydney")
# 10:00 on Monday 15 June 2026 in Sydney (AEST, no daylight saving).
NOW = datetime(2026, 6, 15, 0, 0, tzinfo=UTC)
API_KEY = "test-willyweather-key"
LOCATION_ID = 1234


def _unix(hour: int, day_offset: int = 0) -> int:
    local = datetime(2026, 6, 15, hour, 0, tzinfo=SYDNEY) + timedelta(days=day_offset)
    return int(local.timestamp())


def _day(offset: int, entry: Mapping[str, object]) -> dict[str, object]:
    day = (datetime(2026, 6, 15) + timedelta(days=offset)).strftime("%Y-%m-%d")
    return {"dateTime": f"{day} 00:00:00", "entries": [entry]}


def _willyweather_response() -> dict[str, object]:
    """A WillyWeather ``weather.json`` response trimmed to the fields read."""
    precis = ["Sunny", "Showers", "Cloudy", "Rain", "Windy", "Fine", "Hot"]
    sun = {"riseDateTime": "2026-06-15 07:00:00", "setDateTime": "2026-06-15 16:54:00"}
    return {
        "location": {"name": "Springfield", "timeZone": "Australia/Sydney"},
        "observational": {
            "observations": {
                "temperature": {"temperature": 14.2, "apparentTemperature": 12.1},
                "wind": {"speed": 11, "directionText": "SW"},
            }
        },
        "forecasts": {
            "weather": {
                "days": [
                    _day(i, {"precis": precis[i], "min": 5 + i, "max": 15 + i})
                    for i in range(7)
                ]
            },
            "rainfall": {
                "days": [
                    _day(0, {"probability": 60, "startRange": 1, "endRange": 5}),
                    *[_day(i, {"probability": 0}) for i in range(1, 7)],
                ]
            },
            "sunrisesunset": {"days": [_day(i, sun) for i in range(7)]},
            "uv": {
                "days": [
                    {
                        "alert": {
                            "maxIndex": 4,
                            "scale": "moderate",
                            "startDateTime": "2026-06-15 10:00:00",
                            "endDateTime": "2026-06-15 14:00:00",
                        },
                        "entries": [],
                    },
                    *[{"entries": [{"index": 2, "scale": "low"}]} for _ in range(1, 7)],
                ]
            },
        },
        "forecastGraphs": {
            "rainfallprobability": {
                "dataConfig": {
                    "series": {
                        "groups": [
                            {
                                "points": [
                                    {"x": _unix(9), "y": 10},
                                    {"x": _unix(15), "y": 70},
                                ]
                            }
                        ]
                    }
                }
            },
            "temperature": {
                "dataConfig": {
                    "series": {
                        "groups": [
                            {
                                "points": [
                                    {"x": _unix(9), "y": 11},
                                    {"x": _unix(14), "y": 17},
                                    {"x": _unix(19), "y": 13},
                                ]
                            }
                        ]
                    }
                }
            },
            "precis": {
                "dataConfig": {
                    "series": {
                        "groups": [
                            {
                                "points": [
                                    {"x": _unix(9), "precisCode": "fine"},
                                    {"x": _unix(15), "precisCode": "showers-rain"},
                                ]
                            }
                        ]
                    }
                }
            },
        },
    }


class FakeWillyWeather:
    """Serves one canned forecast and records each request it receives."""

    def __init__(self, status_code: int = 200) -> None:
        self.status_code = status_code
        self.requests: list[httpx.Request] = []

    def handle(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if self.status_code != 200:
            return httpx.Response(self.status_code, text="upstream unavailable")
        return httpx.Response(200, json=_willyweather_response())


EXPECTED_FORECAST = "\n".join([
    "Weather for Springfield:",
    "Now: 14.2°C (feels like 12.1°C), Sunny. Wind: 11 km/h SW.",
    "Today (Jun 15, Monday): Sunny. High: 15°C, Low: 5°C. Rain: 1-5mm, 60% chance."
    " Sun: 07:00-16:54. UV: Max 4 (moderate) from 10:00 to 14:00.",
    "Rain likely: 15:00 (70%).",
    "Temps: Morning 11°C, Afternoon 17°C, Evening 13°C.",
    "Conditions: 09:00: fine -> 15:00: showers rain.",
    "\nOutlook for the week:",
    "Tuesday (Jun 16): Showers. High: 16°C, Low: 6°C."
    " Rain: Little to no rain (0% chance). Sun: 07:00-16:54. UV: Max 2 (low).",
    "Wednesday (Jun 17): Cloudy. High: 17°C, Low: 7°C."
    " Rain: Little to no rain (0% chance). Sun: 07:00-16:54. UV: Max 2 (low).",
    "Thursday (Jun 18): Rain. High: 18°C, Low: 8°C."
    " Rain: Little to no rain (0% chance). Sun: 07:00-16:54. UV: Max 2 (low).",
    "Friday (Jun 19): Windy. High: 19°C, Low: 9°C."
    " Rain: Little to no rain (0% chance). Sun: 07:00-16:54. UV: Max 2 (low).",
    "Saturday (Jun 20): Fine. High: 20°C, Low: 10°C."
    " Rain: Little to no rain (0% chance). Sun: 07:00-16:54. UV: Max 2 (low).",
    "Sunday (Jun 21): Hot. High: 21°C, Low: 11°C."
    " Rain: Little to no rain (0% chance). Sun: 07:00-16:54. UV: Max 2 (low).",
])


@pytest.fixture
def willyweather() -> FakeWillyWeather:
    return FakeWillyWeather()


@pytest.fixture
def clock() -> MockClock:
    return MockClock(NOW)


@pytest.fixture
async def service(
    willyweather: FakeWillyWeather, clock: MockClock
) -> AsyncIterator[WeatherService]:
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(willyweather.handle)
    ) as client:
        yield WeatherService(
            location_id=LOCATION_ID, api_key=API_KEY, httpx_client=client, clock=clock
        )


def _exec_context(timezone: ZoneInfo = SYDNEY) -> ToolExecutionContext:
    return ToolExecutionContext(
        interface_type="test",
        conversation_id="weather-test",
        user_name="Tester",
        turn_id=None,
        db_context=MagicMock(spec=Database),
        processing_service=None,
        clock=None,
        plugins=None,
        event_sources=None,
        attachment_registry=None,
        credential_resolvers=None,
        api_backend=None,
        timezone=timezone,
    )


def _load_defaults(tmp_path: Path) -> AppConfig:
    config = load_config(
        defaults_file_path="defaults.yaml",
        config_file_path=str(tmp_path / "missing-config.yaml"),
    )
    config.willyweather_api_key = None
    config.willyweather_location_id = None
    return config


def _google_disabled() -> OAuthIntegrationState:
    return OAuthIntegrationState(
        provider="google",
        enabled=False,
        reason="Google integration is disabled.",
        taint_enforcement_waived=False,
        enabled_tool_names=frozenset(),
        governed_tool_names=frozenset(),
    )


async def _call_tool(
    registrations: list[ToolRegistration], exec_context: ToolExecutionContext
) -> ToolResult | str:
    provider = LocalToolsProvider(registrations=registrations)
    return await provider.execute_tool("get_weather_forecast", {}, exec_context)


@pytest.mark.asyncio
async def test_forecast_fragments_render_today_and_the_week(
    service: WeatherService, willyweather: FakeWillyWeather
) -> None:
    fragments = await service.get_forecast_fragments(SYDNEY)

    assert "\n".join(fragments) == EXPECTED_FORECAST
    (request,) = willyweather.requests
    assert request.url.path == f"/v2/{API_KEY}/locations/{LOCATION_ID}/weather.json"
    assert request.url.params["days"] == "7"


@pytest.mark.asyncio
async def test_forecast_is_cached_for_an_hour(
    service: WeatherService, willyweather: FakeWillyWeather, clock: MockClock
) -> None:
    await service.get_forecast_fragments(SYDNEY)
    clock.advance(timedelta(minutes=59))
    await service.get_forecast_fragments(SYDNEY)
    assert len(willyweather.requests) == 1

    clock.advance(timedelta(minutes=2))
    await service.get_forecast_fragments(SYDNEY)
    assert len(willyweather.requests) == 2


@pytest.mark.asyncio
async def test_upstream_failure_says_the_forecast_is_unavailable(
    clock: MockClock,
) -> None:
    willyweather = FakeWillyWeather(status_code=503)
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(willyweather.handle)
    ) as client:
        service = WeatherService(
            location_id=LOCATION_ID, api_key=API_KEY, httpx_client=client, clock=clock
        )

        fragments = await service.get_forecast_fragments(SYDNEY)

    assert fragments == ["Weather data unavailable."]


@pytest.mark.asyncio
def test_weather_tool_is_withheld_without_configuration(tmp_path: Path) -> None:
    config = _load_defaults(tmp_path)

    registrations = build_effective_local_tool_registrations(config, _google_disabled())

    assert "get_weather_forecast" not in {r.name for r in registrations}


@pytest.mark.asyncio
async def test_configured_weather_tool_returns_the_forecast(
    tmp_path: Path, service: WeatherService
) -> None:
    config = _load_defaults(tmp_path)
    config.willyweather_api_key = SecretStr(API_KEY)
    config.willyweather_location_id = LOCATION_ID

    registrations = build_effective_local_tool_registrations(
        config, _google_disabled(), service
    )
    result = await _call_tool(registrations, _exec_context())

    assert isinstance(result, ToolResult)
    assert result.get_text() == EXPECTED_FORECAST


@pytest.mark.asyncio
async def test_weather_tool_shows_times_in_the_profile_timezone(
    tmp_path: Path, service: WeatherService
) -> None:
    config = _load_defaults(tmp_path)
    config.willyweather_api_key = SecretStr(API_KEY)
    config.willyweather_location_id = LOCATION_ID

    registrations = build_effective_local_tool_registrations(
        config, _google_disabled(), service
    )
    result = await _call_tool(registrations, _exec_context(ZoneInfo("UTC")))

    assert isinstance(result, ToolResult)
    # 07:00 and 16:54 in Sydney (UTC+10) are 21:00 and 06:54 UTC.
    assert "Sun: 21:00-06:54." in result.get_text()


@pytest.mark.asyncio
async def test_registry_built_without_the_service_reports_it_unavailable(
    tmp_path: Path,
) -> None:
    config = _load_defaults(tmp_path)
    config.willyweather_api_key = SecretStr(API_KEY)
    config.willyweather_location_id = LOCATION_ID

    registrations = build_effective_local_tool_registrations(config, _google_disabled())
    result = await _call_tool(registrations, _exec_context())

    assert isinstance(result, ToolResult)
    assert result.get_text().startswith("Error:")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("api_key", "location_id", "configured"),
    [
        (None, None, False),
        (API_KEY, None, False),
        (None, LOCATION_ID, False),
        (API_KEY, LOCATION_ID, True),
    ],
)
async def test_service_from_config_needs_key_and_location(
    tmp_path: Path, api_key: str | None, location_id: int | None, configured: bool
) -> None:
    config = _load_defaults(tmp_path)
    config.willyweather_api_key = None if api_key is None else SecretStr(api_key)
    config.willyweather_location_id = location_id

    async with httpx.AsyncClient() as client:
        service = WeatherService.from_config(config, client)

    assert (service is not None) == configured
