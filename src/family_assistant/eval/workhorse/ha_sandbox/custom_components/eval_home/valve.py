"""Valves (irrigation, gas): open and close."""

from __future__ import annotations

# Home Assistant's entity bases declare a sync and an async form of each action and
# an integration implements one; the sync forms stay abstract here by design.
# pylint: disable=abstract-method
from typing import TYPE_CHECKING

from homeassistant.components.valve import (
    ValveDeviceClass,
    ValveEntity,
    ValveEntityFeature,
)
from homeassistant.util.enum import try_parse_enum

from .entity import Attributes, WorldEntity, build_entities

if TYPE_CHECKING:
    from homeassistant.config_entries import ConfigEntry
    from homeassistant.core import HomeAssistant
    from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback


class WorldValve(WorldEntity, ValveEntity):
    def setup_world(self, attributes: Attributes) -> None:
        state = self.world_state
        position = attributes.integer("current_position")
        self._attr_reports_position = position is not None
        self._attr_current_valve_position = position
        self._attr_is_closed = None if state is None else state == "closed"
        self._attr_is_opening = state == "opening"
        self._attr_is_closing = state == "closing"
        self._attr_device_class = try_parse_enum(
            ValveDeviceClass, attributes.text("device_class")
        )
        self._attr_supported_features = ValveEntityFeature(
            attributes.integer("supported_features")
            or (ValveEntityFeature.OPEN | ValveEntityFeature.CLOSE)
        )

    def _settle(self, *, closed: bool) -> None:
        self._attr_is_closed = closed
        self._attr_is_opening = self._attr_is_closing = False
        if self._attr_reports_position:
            self._attr_current_valve_position = 0 if closed else 100
        self.async_write_ha_state()

    async def async_open_valve(self) -> None:
        self._settle(closed=False)

    async def async_close_valve(self) -> None:
        self._settle(closed=True)

    async def async_set_valve_position(self, position: int) -> None:
        self._attr_current_valve_position = position
        self._settle(closed=position == 0)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    async_add_entities(build_entities(hass, entry, "valve", WorldValve))
