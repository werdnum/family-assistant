"""Geolocation events (incidents, quakes near home): distance and position."""

from __future__ import annotations

# Home Assistant's entity bases declare a sync and an async form of each action and
# an integration implements one; the sync forms stay abstract here by design.
# pylint: disable=abstract-method
from typing import TYPE_CHECKING

from homeassistant.components.geo_location import GeolocationEvent

from .entity import Attributes, WorldEntity, build_entities, parse_number

if TYPE_CHECKING:
    from homeassistant.config_entries import ConfigEntry
    from homeassistant.core import HomeAssistant
    from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback


class WorldGeolocation(WorldEntity, GeolocationEvent):
    def setup_world(self, attributes: Attributes) -> None:
        self._attr_source = attributes.text("source") or "eval_home"
        self._attr_distance = parse_number(self.world_state)
        self._attr_latitude = attributes.number("latitude")
        self._attr_longitude = attributes.number("longitude")
        self._attr_unit_of_measurement = attributes.text("unit_of_measurement") or "km"


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    async_add_entities(build_entities(hass, entry, "geo_location", WorldGeolocation))
