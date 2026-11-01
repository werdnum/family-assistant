"""Time-of-day entities."""

from __future__ import annotations

# Home Assistant's entity bases declare a sync and an async form of each action and
# an integration implements one; the sync forms stay abstract here by design.
# pylint: disable=abstract-method
from typing import TYPE_CHECKING

from homeassistant.components.time import TimeEntity

from .entity import Attributes, WorldEntity, build_entities, parse_time

if TYPE_CHECKING:
    from datetime import time

    from homeassistant.config_entries import ConfigEntry
    from homeassistant.core import HomeAssistant
    from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback


class WorldTime(WorldEntity, TimeEntity):
    def setup_world(self, attributes: Attributes) -> None:
        self._attr_native_value = parse_time(self.world_state)

    async def async_set_value(self, value: time) -> None:
        self._attr_native_value = value
        self.async_write_ha_state()


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    async_add_entities(build_entities(hass, entry, "time", WorldTime))
