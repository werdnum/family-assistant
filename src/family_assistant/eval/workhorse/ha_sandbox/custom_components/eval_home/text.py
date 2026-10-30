"""Text entities."""

from __future__ import annotations

# Home Assistant's entity bases declare a sync and an async form of each action and
# an integration implements one; the sync forms stay abstract here by design.
# pylint: disable=abstract-method
from typing import TYPE_CHECKING

from homeassistant.components.text import TextEntity, TextMode
from homeassistant.util.enum import try_parse_enum

from .entity import Attributes, WorldEntity, build_entities

if TYPE_CHECKING:
    from homeassistant.config_entries import ConfigEntry
    from homeassistant.core import HomeAssistant
    from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback


class WorldText(WorldEntity, TextEntity):
    def setup_world(self, attributes: Attributes) -> None:
        self._attr_native_value = self.world_state
        self._attr_native_min = attributes.integer("min") or 0
        self._attr_native_max = attributes.integer("max") or 255
        self._attr_pattern = attributes.text("pattern")
        self._attr_mode = try_parse_enum(TextMode, attributes.text("mode")) or (
            TextMode.TEXT
        )

    async def async_set_value(self, value: str) -> None:
        self._attr_native_value = value
        self.async_write_ha_state()


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    async_add_entities(build_entities(hass, entry, "text", WorldText))
