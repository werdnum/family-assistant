"""Covers (blinds, garage doors): open, close and position."""

from __future__ import annotations

# Home Assistant's entity bases declare a sync and an async form of each action and
# an integration implements one; the sync forms stay abstract here by design.
# pylint: disable=abstract-method
from typing import TYPE_CHECKING

from homeassistant.components.cover import (
    ATTR_POSITION,
    ATTR_TILT_POSITION,
    CoverDeviceClass,
    CoverEntity,
    CoverEntityFeature,
)
from homeassistant.util.enum import try_parse_enum

from .entity import Attributes, WorldEntity, build_entities

if TYPE_CHECKING:
    from homeassistant.config_entries import ConfigEntry
    from homeassistant.core import HomeAssistant
    from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback


class WorldCover(WorldEntity, CoverEntity):
    def setup_world(self, attributes: Attributes) -> None:
        state = self.world_state
        self._attr_device_class = try_parse_enum(
            CoverDeviceClass, attributes.text("device_class")
        )
        self._attr_current_cover_position = attributes.integer("current_position")
        self._attr_current_cover_tilt_position = attributes.integer(
            "current_tilt_position"
        )
        self._attr_is_closed = None if state is None else state == "closed"
        self._attr_is_opening = state == "opening"
        self._attr_is_closing = state == "closing"
        features = attributes.integer("supported_features")
        if features is None:
            features = CoverEntityFeature.OPEN | CoverEntityFeature.CLOSE
            if self._attr_current_cover_position is not None:
                features |= CoverEntityFeature.SET_POSITION | CoverEntityFeature.STOP
        self._attr_supported_features = CoverEntityFeature(features)

    def _settle(self, position: int) -> None:
        self._attr_is_opening = self._attr_is_closing = False
        self._attr_is_closed = position == 0
        if self._attr_current_cover_position is not None:
            self._attr_current_cover_position = position
        self.async_write_ha_state()

    async def async_open_cover(self, **kwargs: object) -> None:
        self._settle(100)

    async def async_close_cover(self, **kwargs: object) -> None:
        self._settle(0)

    async def async_stop_cover(self, **kwargs: object) -> None:
        self._attr_is_opening = self._attr_is_closing = False
        self.async_write_ha_state()

    async def async_set_cover_position(self, **kwargs: object) -> None:
        position = kwargs.get(ATTR_POSITION)
        if isinstance(position, int):
            self._attr_current_cover_position = position
            self._settle(position)

    async def async_set_cover_tilt_position(self, **kwargs: object) -> None:
        tilt = kwargs.get(ATTR_TILT_POSITION)
        if isinstance(tilt, int):
            self._attr_current_cover_tilt_position = tilt
            self.async_write_ha_state()


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    async_add_entities(build_entities(hass, entry, "cover", WorldCover))
