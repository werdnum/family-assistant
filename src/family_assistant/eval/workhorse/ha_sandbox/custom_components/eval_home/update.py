"""Firmware and software updates: installing one brings the installed version up."""

from __future__ import annotations

# Home Assistant's entity bases declare a sync and an async form of each action and
# an integration implements one; the sync forms stay abstract here by design.
# pylint: disable=abstract-method
from typing import TYPE_CHECKING

from homeassistant.components.update import (
    UpdateDeviceClass,
    UpdateEntity,
    UpdateEntityFeature,
)
from homeassistant.util.enum import try_parse_enum

from .entity import Attributes, WorldEntity, build_entities

if TYPE_CHECKING:
    from homeassistant.config_entries import ConfigEntry
    from homeassistant.core import HomeAssistant
    from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback


class WorldUpdate(WorldEntity, UpdateEntity):
    def setup_world(self, attributes: Attributes) -> None:
        # Update entities draw their picture from the brands service.
        self._attr_entity_picture = None
        installed = attributes.text("installed_version")
        latest = attributes.text("latest_version")
        if installed is None and latest is None and self.world_state is not None:
            # Never recorded: versions consistent with the on/off state shown.
            installed = "1.0.0"
            latest = "1.0.1" if self.world_state == "on" else installed
        self._attr_installed_version = installed
        self._attr_latest_version = latest
        self._attr_title = attributes.text("title")
        self._attr_release_summary = attributes.text("release_summary")
        self._attr_release_url = attributes.text("release_url")
        self._attr_auto_update = bool(attributes.flag("auto_update"))
        self._attr_in_progress = bool(attributes.flag("in_progress"))
        self._attr_device_class = try_parse_enum(
            UpdateDeviceClass, attributes.text("device_class")
        )
        for derived in (
            "skipped_version",
            "display_precision",
            "update_percentage",
        ):
            attributes.take(derived)
        features = attributes.integer("supported_features")
        self._attr_supported_features = (
            UpdateEntityFeature(features)
            if features is not None
            else UpdateEntityFeature.INSTALL
        )

    async def async_install(
        self, version: str | None, backup: bool, **kwargs: object
    ) -> None:
        self._attr_installed_version = version or self._attr_latest_version
        self.async_write_ha_state()


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    async_add_entities(build_entities(hass, entry, "update", WorldUpdate))
