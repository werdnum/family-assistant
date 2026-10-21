"""Locks: lock, unlock and open."""

from __future__ import annotations

# Home Assistant's entity bases declare a sync and an async form of each action and
# an integration implements one; the sync forms stay abstract here by design.
# pylint: disable=abstract-method
from typing import TYPE_CHECKING

from homeassistant.components.lock import LockEntity, LockEntityFeature

from .entity import Attributes, WorldEntity, build_entities

if TYPE_CHECKING:
    from homeassistant.config_entries import ConfigEntry
    from homeassistant.core import HomeAssistant
    from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback


class WorldLock(WorldEntity, LockEntity):
    def setup_world(self, attributes: Attributes) -> None:
        state = self.world_state
        self._attr_is_locked = None if state is None else state == "locked"
        self._attr_is_open = state == "open"
        self._attr_is_jammed = state == "jammed"
        self._attr_changed_by = attributes.text("changed_by")
        self._attr_code_format = attributes.text("code_format")
        self._attr_supported_features = LockEntityFeature(
            attributes.integer("supported_features") or 0
        )

    def _set(self, *, locked: bool, opened: bool = False) -> None:
        self._attr_is_locked = locked
        self._attr_is_open = opened
        self._attr_is_jammed = False
        self.async_write_ha_state()

    async def async_lock(self, **kwargs: object) -> None:
        self._set(locked=True)

    async def async_unlock(self, **kwargs: object) -> None:
        self._set(locked=False)

    async def async_open(self, **kwargs: object) -> None:
        self._set(locked=False, opened=True)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    async_add_entities(build_entities(hass, entry, "lock", WorldLock))
