"""Fans: on, off, speed and presets."""

from __future__ import annotations

# Home Assistant's entity bases declare a sync and an async form of each action and
# an integration implements one; the sync forms stay abstract here by design.
# pylint: disable=abstract-method
from typing import TYPE_CHECKING

from homeassistant.components.fan import FanEntity, FanEntityFeature

from .entity import Attributes, WorldEntity, build_entities

if TYPE_CHECKING:
    from homeassistant.config_entries import ConfigEntry
    from homeassistant.core import HomeAssistant
    from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback


class WorldFan(WorldEntity, FanEntity):
    def setup_world(self, attributes: Attributes) -> None:
        self._attr_is_on = (
            None if self.world_state is None else self.world_state == "on"
        )
        self._attr_percentage = attributes.integer("percentage")
        step = attributes.number("percentage_step")
        self._attr_speed_count = round(100 / step) if step else 100
        self._attr_preset_modes = attributes.texts("preset_modes")
        self._attr_preset_mode = attributes.text("preset_mode")
        self._attr_oscillating = attributes.flag("oscillating")
        self._attr_current_direction = attributes.text("direction")
        features = FanEntityFeature(attributes.integer("supported_features") or 0)
        features |= FanEntityFeature.TURN_ON | FanEntityFeature.TURN_OFF
        if self._attr_percentage is not None:
            features |= FanEntityFeature.SET_SPEED
        if self._attr_preset_modes:
            features |= FanEntityFeature.PRESET_MODE
        self._attr_supported_features = features

    async def async_turn_on(
        self,
        percentage: int | None = None,
        preset_mode: str | None = None,
        **kwargs: object,
    ) -> None:
        self._attr_is_on = True
        if percentage is not None:
            self._attr_percentage = percentage
        if preset_mode is not None:
            self._attr_preset_mode = preset_mode
        self.async_write_ha_state()

    async def async_turn_off(self, **kwargs: object) -> None:
        self._attr_is_on = False
        self.async_write_ha_state()

    async def async_set_percentage(self, percentage: int) -> None:
        self._attr_percentage = percentage
        self._attr_is_on = percentage > 0
        self.async_write_ha_state()

    async def async_set_preset_mode(self, preset_mode: str) -> None:
        self._attr_preset_mode = preset_mode
        self.async_write_ha_state()

    async def async_oscillate(self, oscillating: bool) -> None:
        self._attr_oscillating = oscillating
        self.async_write_ha_state()

    async def async_set_direction(self, direction: str) -> None:
        self._attr_current_direction = direction
        self.async_write_ha_state()


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    async_add_entities(build_entities(hass, entry, "fan", WorldFan))
