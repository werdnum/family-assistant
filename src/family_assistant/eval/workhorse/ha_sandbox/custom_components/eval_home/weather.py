"""Weather: current conditions as recorded, and forecasts for ``get_forecasts``.

A forecast comes from a recorded ``weather.get_forecasts`` response for the
same entity, re-dated by the world builder to the case's day. An entity no
turn ever asked for a forecast gets a flat one built from its current
conditions, which is world building, not weather.
"""

from __future__ import annotations

# Home Assistant's entity bases declare a sync and an async form of each action and
# an integration implements one; the sync forms stay abstract here by design.
# pylint: disable=abstract-method
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, cast

from homeassistant.components.weather import Forecast, WeatherEntity
from homeassistant.components.weather.const import WeatherEntityFeature
from homeassistant.util import dt as dt_util

from .const import DATA_SEED
from .entity import Attributes, WorldEntity, build_entities

if TYPE_CHECKING:
    from homeassistant.config_entries import ConfigEntry
    from homeassistant.core import HomeAssistant
    from homeassistant.helpers.device_registry import DeviceInfo
    from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

    from .const import EntityRecord, ForecastRecord

_FEATURE_OF_TYPE = {
    "daily": WeatherEntityFeature.FORECAST_DAILY,
    "hourly": WeatherEntityFeature.FORECAST_HOURLY,
    "twice_daily": WeatherEntityFeature.FORECAST_TWICE_DAILY,
}


class WorldWeather(WorldEntity, WeatherEntity):
    def __init__(
        self,
        record: EntityRecord,
        device_info: DeviceInfo | None,
        forecasts: list[ForecastRecord],
    ) -> None:
        self._forecasts = {
            f["forecast_type"]: f["forecast"]
            for f in forecasts
            if f["entity_id"] == record["entity_id"]
        }
        WorldEntity.__init__(self, record, device_info)

    def setup_world(self, attributes: Attributes) -> None:
        self._attr_condition = self.world_state
        self._attr_native_temperature = attributes.number("temperature")
        self._attr_native_temperature_unit = attributes.text("temperature_unit") or "°C"
        self._attr_native_apparent_temperature = attributes.number(
            "apparent_temperature"
        )
        self._attr_native_dew_point = attributes.number("dew_point")
        self._attr_humidity = attributes.number("humidity")
        self._attr_native_pressure = attributes.number("pressure")
        self._attr_native_pressure_unit = attributes.text("pressure_unit") or "hPa"
        self._attr_native_wind_speed = attributes.number("wind_speed")
        self._attr_native_wind_gust_speed = attributes.number("wind_gust_speed")
        self._attr_native_wind_speed_unit = attributes.text("wind_speed_unit") or "km/h"
        bearing = attributes.take("wind_bearing")
        self._attr_wind_bearing = (
            bearing if isinstance(bearing, (int, float, str)) else None
        )
        self._attr_native_visibility = attributes.number("visibility")
        self._attr_native_visibility_unit = attributes.text("visibility_unit") or "km"
        self._attr_native_precipitation_unit = (
            attributes.text("precipitation_unit") or "mm"
        )
        cloud = attributes.number("cloud_coverage")
        self._attr_cloud_coverage = int(cloud) if cloud is not None else None
        self._attr_uv_index = attributes.number("uv_index")
        self._attr_ozone = attributes.number("ozone")
        features = WeatherEntityFeature(attributes.integer("supported_features") or 0)
        for kind in self._forecasts:
            features |= _FEATURE_OF_TYPE.get(kind, WeatherEntityFeature(0))
        if not features:
            features = WeatherEntityFeature.FORECAST_DAILY
        self._attr_supported_features = features

    def _flat(self, step: timedelta, count: int) -> list[Forecast]:
        start = dt_util.now().replace(minute=0, second=0, microsecond=0)
        return [
            Forecast(
                datetime=(start + step * index).isoformat(),
                condition=self._attr_condition,
                native_temperature=self._attr_native_temperature,
                native_templow=self._attr_native_temperature,
                native_precipitation=0.0,
                precipitation_probability=0,
            )
            for index in range(count)
        ]

    def _recorded(self, kind: str) -> list[Forecast] | None:
        recorded = self._forecasts.get(kind)
        if recorded is None:
            return None
        forecasts: list[Forecast] = []
        for item in recorded:
            entry = dict(item)
            # Recorded responses are in display units, which are the native
            # units here; Forecast's native keys carry them unconverted.
            for display, native in (
                ("temperature", "native_temperature"),
                ("templow", "native_templow"),
                ("apparent_temperature", "native_apparent_temperature"),
                ("dew_point", "native_dew_point"),
                ("precipitation", "native_precipitation"),
                ("pressure", "native_pressure"),
                ("wind_speed", "native_wind_speed"),
                ("wind_gust_speed", "native_wind_gust_speed"),
            ):
                if display in entry:
                    entry[native] = entry.pop(display)
            when = entry.get("datetime")
            if isinstance(when, str) and datetime.fromisoformat(when).tzinfo is None:
                entry["datetime"] = (
                    datetime
                    .fromisoformat(when)
                    .replace(tzinfo=dt_util.get_default_time_zone())
                    .isoformat()
                )
            forecasts.append(cast("Forecast", entry))
        return forecasts

    async def async_forecast_daily(self) -> list[Forecast] | None:
        return self._recorded("daily") or self._flat(timedelta(days=1), 7)

    async def async_forecast_hourly(self) -> list[Forecast] | None:
        return self._recorded("hourly") or self._flat(timedelta(hours=1), 24)

    async def async_forecast_twice_daily(self) -> list[Forecast] | None:
        return self._recorded("twice_daily") or self._flat(timedelta(hours=12), 14)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    forecasts = hass.data[DATA_SEED]["forecasts"]
    async_add_entities(
        build_entities(
            hass,
            entry,
            "weather",
            lambda record, device: WorldWeather(record, device, forecasts),
        )
    )
