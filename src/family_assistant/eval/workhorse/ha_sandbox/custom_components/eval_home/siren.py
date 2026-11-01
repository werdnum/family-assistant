"""Sirens: on and off."""

from __future__ import annotations

# Home Assistant's entity bases declare a sync and an async form of each action and
# an integration implements one; the sync forms stay abstract here by design.
# pylint: disable=abstract-method
from typing import TYPE_CHECKING

from homeassistant.components.siren import SirenEntity
from homeassistant.components.siren.const import SirenEntityFeature

from .entity import Attributes, WorldEntity, build_entities

if TYPE_CHECKING:
    from homeassistant.config_entries import ConfigEntry
    from homeassistant.core import HomeAssistant
    from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback


class WorldSiren(WorldEntity, SirenEntity):
    def setup_world(self, attributes: Attributes) -> None:
        self._attr_is_on = (
            None if self.world_state is None else self.world_state == "on"
        )
        tones = attributes.texts("available_tones")
        self._attr_available_tones = list[int | str](tones) if tones else None
        self._attr_supported_features = SirenEntityFeature(
            attributes.integer("supported_features")
            or (SirenEntityFeature.TURN_ON | SirenEntityFeature.TURN_OFF)
        )

    async def async_turn_on(self, **kwargs: object) -> None:
        self._attr_is_on = True
        self.async_write_ha_state()

    async def async_turn_off(self, **kwargs: object) -> None:
        self._attr_is_on = False
        self.async_write_ha_state()


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    async_add_entities(build_entities(hass, entry, "siren", WorldSiren))
