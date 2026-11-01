"""Selects: choosing an option makes it current."""

from __future__ import annotations

# Home Assistant's entity bases declare a sync and an async form of each action and
# an integration implements one; the sync forms stay abstract here by design.
# pylint: disable=abstract-method
from typing import TYPE_CHECKING

from homeassistant.components.select import SelectEntity

from .entity import Attributes, WorldEntity, build_entities

if TYPE_CHECKING:
    from homeassistant.config_entries import ConfigEntry
    from homeassistant.core import HomeAssistant
    from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback


class WorldSelect(WorldEntity, SelectEntity):
    def setup_world(self, attributes: Attributes) -> None:
        options = attributes.texts("options") or []
        if self.world_state is not None and self.world_state not in options:
            options.append(self.world_state)
        self._attr_options = options
        self._attr_current_option = self.world_state

    async def async_select_option(self, option: str) -> None:
        self._attr_current_option = option
        self.async_write_ha_state()


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    async_add_entities(build_entities(hass, entry, "select", WorldSelect))
