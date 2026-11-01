"""Image entities: present, with a labelled placeholder (no image was recorded)."""

from __future__ import annotations

# Home Assistant's entity bases declare a sync and an async form of each action and
# an integration implements one; the sync forms stay abstract here by design.
# pylint: disable=abstract-method
from typing import TYPE_CHECKING

from homeassistant.components.image import ImageEntity
from homeassistant.util import dt as dt_util

from .entity import Attributes, WorldEntity, build_entities, parse_datetime
from .placeholder import placeholder_jpeg

if TYPE_CHECKING:
    from homeassistant.config_entries import ConfigEntry
    from homeassistant.core import HomeAssistant
    from homeassistant.helpers.device_registry import DeviceInfo
    from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

    from .const import EntityRecord


class WorldImage(WorldEntity, ImageEntity):
    _attr_content_type = "image/jpeg"

    def __init__(
        self,
        hass: HomeAssistant,
        record: EntityRecord,
        device_info: DeviceInfo | None,
    ) -> None:
        ImageEntity.__init__(self, hass)
        WorldEntity.__init__(self, record, device_info)

    def setup_world(self, attributes: Attributes) -> None:
        self._attr_entity_picture = None
        attributes.take("access_token")
        self._attr_image_last_updated = parse_datetime(self.world_state)

    async def async_image(self) -> bytes | None:
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
    async_add_entities(
        build_entities(
            hass,
            entry,
            "image",
            lambda record, device: WorldImage(hass, record, device),
        )
    )
