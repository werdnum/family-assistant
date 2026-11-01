"""Device trackers: where each phone or laptop was, with its recorded zone name.

The recorded state (``home``, ``not_home``, a zone's name) is served as the
tracker's location name, so it reads exactly as recorded whatever zones the
sandbox defines; coordinates are served alongside it.
"""

from __future__ import annotations

# Home Assistant's entity bases declare a sync and an async form of each action and
# an integration implements one; the sync forms stay abstract here by design.
# pylint: disable=abstract-method
from typing import TYPE_CHECKING

from homeassistant.components.device_tracker.config_entry import TrackerEntity
from homeassistant.components.device_tracker.const import SourceType
from homeassistant.util.enum import try_parse_enum

from .entity import Attributes, WorldEntity, build_entities

if TYPE_CHECKING:
    from homeassistant.config_entries import ConfigEntry
    from homeassistant.core import HomeAssistant
    from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback


class WorldTracker(WorldEntity, TrackerEntity):
    def setup_world(self, attributes: Attributes) -> None:
        self._attr_latitude = attributes.number("latitude")
        self._attr_longitude = attributes.number("longitude")
        self._attr_location_accuracy = attributes.number("gps_accuracy") or 0
        self._attr_location_name = self.world_state
        self._attr_source_type = (
            try_parse_enum(SourceType, attributes.text("source_type")) or SourceType.GPS
        )
        attributes.take("in_zones")
        battery = attributes.integer("battery_level")
        if battery is not None:
            attributes.keep("battery_level", battery)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    async_add_entities(build_entities(hass, entry, "device_tracker", WorldTracker))
