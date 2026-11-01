"""The base every served entity shares: identity, availability and attributes.

Each platform takes the attributes its Home Assistant base class computes
itself (brightness, hvac modes, options) into real entity properties, so
service calls change them and Home Assistant validates them as it would at
home. Whatever no property computes is served unchanged as extra attributes.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, time
from typing import TYPE_CHECKING

from homeassistant.const import STATE_UNAVAILABLE, STATE_UNKNOWN
from homeassistant.helpers import area_registry as ar
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity import Entity

from .const import DATA_RECORDS, DATA_SEED, DOMAIN, domain_of

if TYPE_CHECKING:
    from collections.abc import Callable

    from homeassistant.config_entries import ConfigEntry
    from homeassistant.core import HomeAssistant

    from .const import EntityRecord, Seed


class Attributes:
    """Recorded attributes, taken one at a time by the property that serves each."""

    def __init__(self, attributes: dict[str, object]) -> None:
        self._values = dict(attributes)

    def take(self, key: str) -> object:
        return self._values.pop(key, None)

    def keep(self, key: str, value: object) -> None:
        """Serve ``value`` as an extra attribute after all."""
        self._values[key] = value

    def has(self, key: str) -> bool:
        return key in self._values

    def text(self, key: str) -> str | None:
        value = self.take(key)
        return value if isinstance(value, str) else None

    def number(self, key: str) -> float | None:
        value = self.take(key)
        if isinstance(value, bool):
            return None
        if isinstance(value, (int, float)):
            return float(value)
        if isinstance(value, str):
            try:
                return float(value)
            except ValueError:
                return None
        return None

    def integer(self, key: str) -> int | None:
        value = self.number(key)
        return int(value) if value is not None else None

    def flag(self, key: str) -> bool | None:
        value = self.take(key)
        return value if isinstance(value, bool) else None

    def texts(self, key: str) -> list[str] | None:
        value = self.take(key)
        if isinstance(value, (list, tuple)):
            return [str(v) for v in value]
        return None

    def numbers(self, key: str) -> tuple[float, ...] | None:
        value = self.take(key)
        if isinstance(value, (list, tuple)) and all(
            isinstance(v, (int, float)) for v in value
        ):
            return tuple(float(v) for v in value)
        return None

    def rest(self) -> dict[str, object]:
        return dict(self._values)


def parse_number(state: str | None) -> float | None:
    if state is None:
        return None
    try:
        return float(state)
    except ValueError:
        return None


def parse_datetime(value: object) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def parse_date(value: str | None) -> date | None:
    if not value:
        return None
    try:
        return date.fromisoformat(value[:10])
    except ValueError:
        return None


def parse_time(value: str | None) -> time | None:
    if not value:
        return None
    try:
        return time.fromisoformat(value)
    except ValueError:
        return None


class WorldEntity(Entity):
    """An entity with the production id, name and state of a household entity."""

    _attr_should_poll = False
    _attr_has_entity_name = False

    def __init__(self, record: EntityRecord, device_info: DeviceInfo | None) -> None:
        # Deliberately no super().__init__(): the platform bases that have one
        # (Camera, ImageEntity) take different arguments, and their subclasses
        # here call it themselves before this.
        self.entity_id = record["entity_id"]
        self._attr_unique_id = record["entity_id"]
        self._attr_name = record["name"]
        self._attr_device_info = device_info
        state = record["state"]
        self._attr_available = state != STATE_UNAVAILABLE
        # None where Home Assistant would show "unknown" or "unavailable".
        self.world_state: str | None = (
            None if state in {STATE_UNKNOWN, STATE_UNAVAILABLE} else state
        )
        self.world_area = record["area"]
        attributes = Attributes(record["attributes"])
        attributes.take("friendly_name")
        self._attr_icon = attributes.text("icon")
        self._attr_assumed_state = bool(attributes.flag("assumed_state"))
        self._attr_attribution = attributes.text("attribution")
        self._attr_entity_picture = attributes.text("entity_picture")
        self.setup_world(attributes)
        self._attr_extra_state_attributes = attributes.rest()

    def setup_world(self, attributes: Attributes) -> None:
        """Take the attributes this platform computes into properties."""

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        if self.world_area:
            area = ar.async_get(self.hass).async_get_or_create(self.world_area)
            er.async_get(self.hass).async_update_entity(self.entity_id, area_id=area.id)


def device_info(seed: Seed, key: str | None) -> DeviceInfo | None:
    device = next((d for d in seed["devices"] if d["key"] == key), None)
    if device is None:
        return None
    info = DeviceInfo(identifiers={(DOMAIN, device["key"])}, name=device["name"])
    if device["manufacturer"]:
        info["manufacturer"] = device["manufacturer"]
    if device["model"]:
        info["model"] = device["model"]
    if device["sw_version"]:
        info["sw_version"] = device["sw_version"]
    if device["area"]:
        info["suggested_area"] = device["area"]
    return info


def build_entities[E: WorldEntity](
    hass: HomeAssistant,
    entry: ConfigEntry,
    domain: str,
    factory: Callable[[EntityRecord, DeviceInfo | None], E],
) -> list[E]:
    seed: Seed = hass.data[DATA_SEED]
    records: list[EntityRecord] = hass.data[DATA_RECORDS][entry.entry_id]
    return [
        factory(record, device_info(seed, record["device"]))
        for record in records
        if domain_of(record) == domain
    ]
