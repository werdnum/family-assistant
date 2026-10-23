"""Sensors, with the recorded value kept exactly as it was shown.

Home Assistant refuses a sensor whose value contradicts its metadata (a
non-number with a unit, an enum value outside its options). A recorded state
can do that legitimately -- the moment it was seen, the source was offline --
so the metadata is reconciled with the value rather than the entity lost.
"""

from __future__ import annotations

# Home Assistant's entity bases declare a sync and an async form of each action and
# an integration implements one; the sync forms stay abstract here by design.
# pylint: disable=abstract-method
from typing import TYPE_CHECKING

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorStateClass,
)
from homeassistant.components.sensor.const import (
    NON_NUMERIC_DEVICE_CLASSES,
    STATE_CLASS_UNITS,
)
from homeassistant.util.enum import try_parse_enum

from .entity import Attributes, WorldEntity, build_entities, parse_date, parse_datetime

if TYPE_CHECKING:
    from datetime import date, datetime

    from homeassistant.config_entries import ConfigEntry
    from homeassistant.core import HomeAssistant
    from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback


class WorldSensor(WorldEntity, SensorEntity):
    def setup_world(self, attributes: Attributes) -> None:
        raw_class = attributes.text("device_class") or None
        device_class = try_parse_enum(SensorDeviceClass, raw_class)
        if raw_class is not None and device_class is None:
            # A custom class Home Assistant does not know is shown, not validated.
            attributes.keep("device_class", raw_class)
        state_class = try_parse_enum(SensorStateClass, attributes.text("state_class"))
        unit = attributes.text("unit_of_measurement") or None
        options = attributes.texts("options")
        attributes.take("last_reset")
        value: str | date | datetime | None = self.world_state
        if options is not None or device_class is SensorDeviceClass.ENUM:
            device_class = SensorDeviceClass.ENUM
            unit = None
            state_class = None
            options = list(options or [])
            if isinstance(value, str) and value not in options:
                options.append(value)
        elif device_class is SensorDeviceClass.TIMESTAMP:
            value = parse_datetime(value)
            unit = None
        elif device_class is SensorDeviceClass.DATE:
            value = parse_date(value if isinstance(value, str) else None)
            unit = None
        elif device_class in NON_NUMERIC_DEVICE_CLASSES:
            unit = None
        else:
            numeric = (
                device_class is not None or state_class is not None or unit is not None
            )
            if numeric and isinstance(value, str):
                try:
                    float(value)
                except ValueError:
                    value = None
            if (
                state_class is not None
                and (allowed := STATE_CLASS_UNITS.get(state_class)) is not None
                and unit not in allowed
            ):
                state_class = None
        self._attr_device_class = device_class
        self._attr_state_class = state_class
        self._attr_native_unit_of_measurement = unit
        self._attr_options = options
        self._attr_native_value = value


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    async_add_entities(build_entities(hass, entry, "sensor", WorldSensor))
