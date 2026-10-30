"""Constants and the seed file's shape."""

from __future__ import annotations

from typing import TypedDict

from homeassistant.const import Platform

DOMAIN = "eval_home"
SEED_FILE = "eval_home_world.json"
DATA_SEED = f"{DOMAIN}_seed"
DATA_RECORDS = f"{DOMAIN}_records"
# The config entry that holds entities whose integration nothing recorded.
DEFAULT_INTEGRATION = "home"

# Entity domains this integration serves as platforms. Helpers, people, zones,
# automations and scripts are served by Home Assistant's own integrations,
# configured by the launcher.
PLATFORMS: dict[str, Platform] = {
    "binary_sensor": Platform.BINARY_SENSOR,
    "button": Platform.BUTTON,
    "calendar": Platform.CALENDAR,
    "camera": Platform.CAMERA,
    "climate": Platform.CLIMATE,
    "cover": Platform.COVER,
    "date": Platform.DATE,
    "datetime": Platform.DATETIME,
    "device_tracker": Platform.DEVICE_TRACKER,
    "event": Platform.EVENT,
    "fan": Platform.FAN,
    "geo_location": Platform.GEO_LOCATION,
    "image": Platform.IMAGE,
    "light": Platform.LIGHT,
    "lock": Platform.LOCK,
    "media_player": Platform.MEDIA_PLAYER,
    "notify": Platform.NOTIFY,
    "number": Platform.NUMBER,
    "remote": Platform.REMOTE,
    "select": Platform.SELECT,
    "sensor": Platform.SENSOR,
    "siren": Platform.SIREN,
    "switch": Platform.SWITCH,
    "text": Platform.TEXT,
    "time": Platform.TIME,
    "update": Platform.UPDATE,
    "vacuum": Platform.VACUUM,
    "valve": Platform.VALVE,
    "weather": Platform.WEATHER,
}


class EntityRecord(TypedDict):
    """One entity as the house stood at the seed's moment."""

    entity_id: str
    state: str
    attributes: dict[str, object]
    name: str | None
    integration: str | None
    device: str | None
    area: str | None
    world_built: bool


class DeviceRecord(TypedDict):
    key: str
    name: str
    manufacturer: str | None
    model: str | None
    sw_version: str | None
    area: str | None
    integration: str | None
    recorded_id: str | None


class AreaRecord(TypedDict):
    area_id: str
    name: str


class ForecastRecord(TypedDict):
    entity_id: str
    forecast_type: str
    seen_at: str
    forecast: list[dict[str, object]]
    redated: bool


class Rename(TypedDict):
    """Give an entity Home Assistant's own integration created its production id."""

    domain: str
    unique_id: str
    entity_id: str


class Seed(TypedDict):
    """What the launcher writes: a ``HomeSnapshot`` plus where to log calls."""

    moment: str
    location_name: str
    areas: list[AreaRecord]
    devices: list[DeviceRecord]
    entities: list[EntityRecord]
    forecasts: list[ForecastRecord]
    services: list[str]
    service_log: str
    renames: list[Rename]


def integration_of(record: EntityRecord) -> str:
    return record["integration"] or DEFAULT_INTEGRATION


def domain_of(record: EntityRecord) -> str:
    return record["entity_id"].split(".", 1)[0]
