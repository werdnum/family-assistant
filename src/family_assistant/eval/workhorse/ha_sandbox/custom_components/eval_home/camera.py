"""Cameras: present, with a labelled placeholder frame (no footage was recorded)."""

from __future__ import annotations

# Home Assistant's entity bases declare a sync and an async form of each action and
# an integration implements one; the sync forms stay abstract here by design.
# pylint: disable=abstract-method
from typing import TYPE_CHECKING

from homeassistant.components.camera import Camera
from homeassistant.util import dt as dt_util

from .entity import Attributes, WorldEntity, build_entities
from .placeholder import placeholder_jpeg

if TYPE_CHECKING:
    from homeassistant.config_entries import ConfigEntry
    from homeassistant.core import HomeAssistant
    from homeassistant.helpers.device_registry import DeviceInfo
    from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

    from .const import EntityRecord

_DERIVED = ("access_token", "frontend_stream_type", "motion_detection")


class WorldCamera(WorldEntity, Camera):
    def __init__(self, record: EntityRecord, device_info: DeviceInfo | None) -> None:
        Camera.__init__(self)
        WorldEntity.__init__(self, record, device_info)

    def setup_world(self, attributes: Attributes) -> None:
        self._attr_entity_picture = None
        self._attr_brand = attributes.text("brand")
        self._attr_model = attributes.text("model_name")
        self._attr_is_recording = self.world_state == "recording"
        self._attr_is_streaming = self.world_state == "streaming"
        for derived in _DERIVED:
            attributes.take(derived)

    async def async_camera_image(
        self, width: int | None = None, height: int | None = None
    ) -> bytes | None:
        return await self.hass.async_add_executor_job(
            placeholder_jpeg,
            str(self.name or self.entity_id),
            dt_util.now().isoformat(timespec="seconds"),
        )


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    async_add_entities(build_entities(hass, entry, "camera", WorldCamera))
