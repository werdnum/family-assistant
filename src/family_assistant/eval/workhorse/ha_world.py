"""The household's Home Assistant, rebuilt from recorded tool results and events.

The snapshot holds no export of the house, but it holds a great deal of it in
pieces. Every entity listing, state lookup, search, history query and template
a turn rendered is recorded, and the ``recent_events`` table holds the
``state_changed`` events of the last few days before the snapshot, each with
the entity's full new state and attributes. Pooled, they give the entities the
house had, their names, areas, devices and integrations, and how each one's
state looked at the moments something observed it.

Each entity keeps its observations over time, so a case sees the house as it
looked when the case was asked: the latest state known at that moment, or, for
an entity nothing observed until later, the earliest one known. Attributes are
carried from the nearest observation that recorded them in full, overlaid with
any partial ones (a single ``state_attr`` lookup, a search result) up to the
chosen state.

What the record lacks is filled in, and says so. An entity that was only ever
listed is given a plausible resting state for its domain and is marked
``world_built``; a weather forecast recorded on another day is re-dated to the
case's day. The counts of each are reported by :meth:`HomeWorld.summary`.
"""

from __future__ import annotations

import ast
import json
import re
from collections import Counter, defaultdict
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Literal

import yaml
from pydantic import BaseModel, ConfigDict, JsonValue

if TYPE_CHECKING:
    from collections.abc import Callable, Iterable, Iterator, Mapping

    from family_assistant.storage.types import MessageHistoryRow

__all__ = [
    "EntityState",
    "HistoryPoint",
    "HomeSnapshot",
    "HomeWorld",
    "StateObservation",
    "WorldArea",
    "WorldConfig",
    "WorldDevice",
    "WorldEntity",
    "WorldForecast",
    "build_home_world",
    "slugify",
]

Attributes = dict[str, JsonValue]
ForecastType = Literal["daily", "hourly", "twice_daily"]
_FORECAST_TYPES: dict[str, ForecastType] = {
    "daily": "daily",
    "hourly": "hourly",
    "twice_daily": "twice_daily",
}

_ENTITY_ID = r"[a-z_]+\.[a-z0-9_]+"
_ENTITY_ID_RE = re.compile(rf"^{_ENTITY_ID}$")
_LISTING_LINE = re.compile(
    rf"^- (?P<entity_id>{_ENTITY_ID})(?: - (?P<name>.*?))?"
    r"(?: \(Area: (?P<area>.*?)\))?(?: \[Device: (?P<device>.*)\])?$"
)
_CHANGED_STATES = re.compile(
    r"\d+ state change\(s\): (?P<changes>.*?)(?:; response=|$)"
)
_CHANGE = re.compile(rf"(?P<entity_id>{_ENTITY_ID})=(?P<state>[^,;]*)")
_STATE_REPR = re.compile(
    rf"<state (?P<entity_id>{_ENTITY_ID})=(?P<state>[^;@]*?)(?:; (?P<attrs>.*?))?"
    r" @ (?P<changed>\d{4}-\d\d-\d\dT[\d:.]+[+-]\d\d:\d\d)>"
)
_REPR_FRIENDLY_NAME = re.compile(r"(?:^|, )friendly_name=(?P<name>.*?)(?:, [a-z_]+=|$)")
_ENUM_REPR = re.compile(r"<[A-Za-z_][\w.]*: ('[^']*'|-?\d+(?:\.\d+)?)>")
_EXPRESSION = re.compile(r"\{\{-?\s*(.*?)\s*-?\}\}", re.DOTALL)
_QUOTED = r"""['"]([^'"]+)['"]"""
_STATES_CALL = re.compile(rf"^states\(\s*{_QUOTED}\s*\)$")
_STATES_DOT = re.compile(r"^states\.([a-z_]+)\.([a-z0-9_]+)\.state$")
_STATES_INDEX = re.compile(rf"^states\[{_QUOTED}\]\[{_QUOTED}\]\.state$")
_STATE_ATTR = re.compile(rf"^state_attr\(\s*{_QUOTED}\s*,\s*{_QUOTED}\s*\)$")
_ATTRIBUTES_JSON = re.compile(
    r"^states\.([a-z_]+)\.([a-z0-9_]+)\.attributes\s*\|\s*(?:to_json|tojson)$"
)
_INTEGRATION_ENTITIES = re.compile(rf"^integration_entities\(\s*{_QUOTED}\s*\)$")
_AREA_ENTITIES = re.compile(rf"^area_entities\(\s*{_QUOTED}\s*\)$")
_AREA_NAME = re.compile(rf"^area_name\(\s*{_QUOTED}\s*\)$")
_LOOP_VARIABLE = re.compile(r"\{%-?\s*for\s+(\w+)\s+in\s")
_CONCAT_CHAIN = re.compile(
    r"(?:\w+(?:\.\w+)+(?:\s*\|\s*default\([^)]*\))?|'[^']*'|\"[^\"]*\")"
    r"(?:\s*~\s*(?:\w+(?:\.\w+)+(?:\s*\|\s*default\([^)]*\))?|'[^']*'|\"[^\"]*\"))+"
)
_CHAIN_TOKEN = re.compile(r"\w+(?:\.\w+)+|'[^']*'|\"[^\"]*\"")
_RESPONSE = re.compile(r"; response=(?P<json>\{.*\})\s*$", re.DOTALL)
_SERVICE_REF = re.compile(r"^([a-z_]+)\.([a-z0-9_]+)$")
# Keys that make a dict in a result a description of an entity, rather than
# a reference to one inside a configuration or trace.
_EVIDENCE = {
    "attributes",
    "entity_entry",
    "friendly_name",
    "last_changed",
    "last_updated",
    "states",
    "unique_id",
}
# Fields a loop body can name on the state it iterates over.
_LOOP_FIELDS = {"entity_id", "state", "name", "attributes.friendly_name"}
_UNKNOWN = {"unknown", "unavailable", "None", ""}

# A plausible resting state for an entity nothing ever observed.
_RESTING_STATE: dict[str, str] = {
    "automation": "on",
    "binary_sensor": "off",
    "button": "unknown",
    "camera": "idle",
    "climate": "off",
    "cover": "closed",
    "device_tracker": "not_home",
    "event": "unknown",
    "fan": "off",
    "input_boolean": "off",
    "light": "off",
    "lock": "locked",
    "media_player": "off",
    "remote": "off",
    "script": "off",
    "siren": "off",
    "switch": "off",
    "update": "off",
    "vacuum": "docked",
    "valve": "closed",
}


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid")


def slugify(text: str) -> str:
    """Home Assistant's id for a name: lower case, runs of other characters to ``_``."""
    return re.sub(r"[^a-z0-9]+", "_", text.lower()).strip("_") or "unnamed"


class StateObservation(_Model):
    """An entity's state as one recorded result showed it."""

    # When the state is known to have held: its ``last_changed`` where the
    # result carried one, otherwise the moment the result was received.
    at: datetime
    state: str
    attributes: Attributes | None = None
    # False for a subset: a single ``state_attr`` lookup, a filtered search.
    attributes_complete: bool = False
    source: str


class HistoryPoint(_Model):
    at: datetime
    state: str
    attributes: Attributes


class EntityState(_Model):
    """An entity as it is served at one moment."""

    entity_id: str
    state: str
    attributes: Attributes
    name: str | None
    integration: str | None
    device: str | None
    area: str | None
    # The state was invented: nothing recorded ever showed this entity's state.
    world_built: bool = False


class WorldEntity(_Model):
    entity_id: str
    name: str | None = None
    integration: str | None = None
    # A device's key in ``HomeWorld.devices``.
    device: str | None = None
    # The entity's own area (by name), where it differs from or lacks a device.
    area: str | None = None
    observations: list[StateObservation] = []

    @property
    def domain(self) -> str:
        return self.entity_id.split(".", 1)[0]

    def _ordered(self) -> list[StateObservation]:
        return sorted(self.observations, key=lambda o: o.at)

    def as_of(self, moment: datetime) -> EntityState:
        """The entity at ``moment``: latest known state, else the earliest after."""
        ordered = self._ordered()
        if not ordered:
            state = _resting_state(self.domain)
            attributes: Attributes = {}
            world_built = True
        else:
            before = [o for o in ordered if o.at <= moment]
            chosen = before[-1] if before else ordered[0]
            state = chosen.state
            attributes = _attributes_at(ordered, chosen, moment)
            world_built = False
        if self.name and "friendly_name" not in attributes:
            attributes["friendly_name"] = self.name
        name = attributes.get("friendly_name")
        return EntityState(
            entity_id=self.entity_id,
            state=state,
            attributes=attributes,
            name=name if isinstance(name, str) else self.name,
            integration=self.integration,
            device=self.device,
            area=self.area,
            world_built=world_built,
        )

    def history_until(self, moment: datetime) -> list[HistoryPoint]:
        """Every distinct state up to ``moment``, attributes carried forward."""
        points: list[HistoryPoint] = []
        attributes: Attributes = {}
        for observation in self._ordered():
            if observation.at > moment:
                break
            if observation.attributes is not None:
                if observation.attributes_complete:
                    attributes = dict(observation.attributes)
                else:
                    attributes = {**attributes, **observation.attributes}
            current = dict(attributes)
            if self.name and "friendly_name" not in current:
                current["friendly_name"] = self.name
            if (
                points
                and points[-1].state == observation.state
                and points[-1].attributes == current
            ):
                continue
            points.append(
                HistoryPoint(
                    at=observation.at, state=observation.state, attributes=current
                )
            )
        return points


def _resting_state(domain: str) -> str:
    return _RESTING_STATE.get(domain, "unknown")


def _attributes_at(
    ordered: list[StateObservation], chosen: StateObservation, moment: datetime
) -> Attributes:
    complete = [
        o for o in ordered if o.attributes is not None and o.attributes_complete
    ]
    if chosen.attributes is not None and chosen.attributes_complete:
        return dict(chosen.attributes)
    complete_before = [o for o in complete if o.at <= max(moment, chosen.at)]
    base = (
        complete_before[-1] if complete_before else (complete[0] if complete else None)
    )
    attributes: Attributes = dict(base.attributes or {}) if base is not None else {}
    floor = base.at if base is not None else None
    for observation in ordered:
        if observation.at > chosen.at:
            break
        if (
            observation.attributes is not None
            and not observation.attributes_complete
            and (floor is None or observation.at >= floor)
        ):
            attributes.update(observation.attributes)
    return attributes


class WorldArea(_Model):
    area_id: str
    name: str


class WorldDevice(_Model):
    # The device's name as listings show it: the only identity they carry.
    key: str
    name: str
    manufacturer: str | None = None
    model: str | None = None
    sw_version: str | None = None
    area: str | None = None
    integration: str | None = None
    # The production device id, where a result showed it.
    recorded_id: str | None = None


class WorldForecast(_Model):
    entity_id: str
    forecast_type: ForecastType
    seen_at: datetime
    forecast: list[Attributes]
    # Shifted from the day it was recorded to the case's day.
    redated: bool = False


class WorldConfig(_Model):
    """An automation or script configuration as a result showed it."""

    entity_id: str
    seen_at: datetime
    config: Attributes


class HomeSnapshot(_Model):
    """The house at one moment: what the sandbox ``hass`` is seeded with."""

    moment: datetime
    location_name: str
    time_zone: str
    latitude: float | None
    longitude: float | None
    areas: list[WorldArea]
    devices: list[WorldDevice]
    entities: list[EntityState]
    forecasts: list[WorldForecast]
    automations: list[WorldConfig]
    scripts: list[WorldConfig]
    services: list[str]
    history: dict[str, list[HistoryPoint]]


class HomeWorld(_Model):
    location_name: str
    time_zone: str
    latitude: float | None = None
    longitude: float | None = None
    areas: list[WorldArea]
    devices: list[WorldDevice]
    entities: list[WorldEntity]
    forecasts: list[WorldForecast] = []
    automations: list[WorldConfig] = []
    scripts: list[WorldConfig] = []
    # ``domain.service`` names recorded calls or configurations used that no
    # served entity platform provides (legacy notify targets, ``mqtt.publish``).
    services: list[str] = []

    def entity(self, entity_id: str) -> WorldEntity | None:
        return next((e for e in self.entities if e.entity_id == entity_id), None)

    def as_of(self, moment: datetime) -> HomeSnapshot:
        return HomeSnapshot(
            moment=moment,
            location_name=self.location_name,
            time_zone=self.time_zone,
            latitude=self.latitude,
            longitude=self.longitude,
            areas=self.areas,
            devices=self.devices,
            entities=[e.as_of(moment) for e in self.entities],
            forecasts=_forecasts_as_of(self.forecasts, moment),
            automations=_configs_as_of(self.automations, moment),
            scripts=_configs_as_of(self.scripts, moment),
            services=self.services,
            history={
                e.entity_id: points
                for e in self.entities
                if (points := e.history_until(moment))
            },
        )

    def summary(self) -> dict[str, JsonValue]:
        """Counts of what is recorded and what is world-built."""
        by_domain = Counter(e.domain for e in self.entities)
        observed = [e for e in self.entities if e.observations]
        with_attributes = [
            e
            for e in self.entities
            if any(o.attributes is not None for o in e.observations)
        ]
        with_full_attributes = [
            e
            for e in self.entities
            if any(o.attributes_complete for o in e.observations)
        ]
        sources = Counter(o.source for e in self.entities for o in e.observations)
        return {
            "entities": len(self.entities),
            "by_domain": dict(by_domain.most_common()),
            "with_recorded_state": len(observed),
            "world_built_state": len(self.entities) - len(observed),
            "world_built_by_domain": dict(
                Counter(e.domain for e in self.entities if not e.observations)
            ),
            "with_recorded_attributes": len(with_attributes),
            "with_full_recorded_attributes": len(with_full_attributes),
            "observations_by_source": dict(sources.most_common()),
            "areas": len(self.areas),
            "devices": len(self.devices),
            "entities_with_device": sum(1 for e in self.entities if e.device),
            "entities_with_area": sum(1 for e in self.entities if e.area),
            "entities_with_integration": sum(1 for e in self.entities if e.integration),
            "forecasts": len(self.forecasts),
            "automation_configs": len({a.entity_id for a in self.automations}),
            "script_configs": len({s.entity_id for s in self.scripts}),
            "extra_services": len(self.services),
        }


def _nearest[T](items: list[T], at: Callable[[T], datetime], moment: datetime) -> T:
    ordered = sorted(items, key=at)
    before = [i for i in ordered if at(i) <= moment]
    return before[-1] if before else ordered[0]


def _configs_as_of(configs: list[WorldConfig], moment: datetime) -> list[WorldConfig]:
    grouped: dict[str, list[WorldConfig]] = defaultdict(list)
    for config in configs:
        grouped[config.entity_id].append(config)
    return [
        _nearest(items, lambda c: c.seen_at, moment)
        for _, items in sorted(grouped.items())
    ]


def _shift(value: JsonValue, delta: timedelta) -> JsonValue:
    if not isinstance(value, str):
        return value
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return value
    return (parsed + delta).isoformat()


def _forecasts_as_of(
    forecasts: list[WorldForecast], moment: datetime
) -> list[WorldForecast]:
    """One forecast per entity and type, re-dated to start from ``moment``."""
    grouped: dict[tuple[str, str], list[WorldForecast]] = defaultdict(list)
    for forecast in forecasts:
        grouped[forecast.entity_id, forecast.forecast_type].append(forecast)
    served: list[WorldForecast] = []
    for _, items in sorted(grouped.items()):
        chosen = _nearest(items, lambda f: f.seen_at, moment)
        if chosen.forecast_type == "hourly":
            hours = round((moment - chosen.seen_at).total_seconds() / 3600)
            delta = timedelta(hours=hours)
        else:
            delta = timedelta(days=(moment.date() - chosen.seen_at.date()).days)
        if not delta:
            served.append(chosen)
            continue
        served.append(
            chosen.model_copy(
                update={
                    "seen_at": chosen.seen_at + delta,
                    "redated": True,
                    "forecast": [
                        {
                            key: _shift(value, delta) if key == "datetime" else value
                            for key, value in item.items()
                        }
                        for item in chosen.forecast
                    ],
                }
            )
        )
    return served


# --- Building ---------------------------------------------------------------


def _arguments(raw: object) -> dict[str, object]:
    if isinstance(raw, dict):
        return {str(k): v for k, v in raw.items()}
    if isinstance(raw, str):
        try:
            decoded = json.loads(raw)
        except json.JSONDecodeError:
            return {}
        return _arguments(decoded) if isinstance(decoded, dict) else {}
    return {}


def _json(text: str) -> JsonValue | None:
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return None


def _parse_time(value: object) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def _json_safe(value: object) -> JsonValue:
    """A Python literal from a rendered template, as JSON."""
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    if isinstance(value, (list, tuple, set, frozenset)):
        return [_json_safe(v) for v in value]
    if isinstance(value, dict):
        return {str(k): _json_safe(v) for k, v in value.items()}
    return str(value)


def _literal(text: str) -> JsonValue:
    """A rendered value: a Python or JSON literal where it parses, else the text."""
    # Home Assistant renders enum members as ``<HVACMode.COOL: 'cool'>``.
    stripped = _ENUM_REPR.sub(r"\1", text.strip())
    try:
        return _json_safe(ast.literal_eval(stripped))
    except (ValueError, SyntaxError, MemoryError, RecursionError):
        pass
    decoded = _json(stripped)
    return decoded if decoded is not None else stripped


def _attribute_dict(value: object) -> Attributes | None:
    if not isinstance(value, dict):
        return None
    return {str(k): _json_safe(v) for k, v in value.items()}


def _str(value: object) -> str | None:
    return value if isinstance(value, str) and value else None


class _Builder:
    def __init__(self) -> None:
        self.entities: dict[str, WorldEntity] = {}
        self.devices: dict[str, WorldDevice] = {}
        self.device_by_id: dict[str, str] = {}
        self.area_names: dict[str, str] = {}
        self.forecasts: list[WorldForecast] = []
        self.automations: list[WorldConfig] = []
        self.scripts: list[WorldConfig] = []
        self.services: set[str] = set()
        self.location_name: str | None = None
        self.time_zone: str | None = None
        # GetLiveContext names entities only by friendly name; resolved last.
        self.live_context: list[tuple[datetime, Mapping[str, object]]] = []
        self.pending_device_ids: list[tuple[str, str]] = []

    # Registry ---------------------------------------------------------------

    def entity(self, entity_id: str) -> WorldEntity:
        existing = self.entities.get(entity_id)
        if existing is None:
            existing = WorldEntity(entity_id=entity_id)
            self.entities[entity_id] = existing
        return existing

    def area(self, name: str, area_id: str | None = None) -> str:
        key = area_id or slugify(name)
        self.area_names.setdefault(key, name)
        return self.area_names[key]

    def area_from_id(self, area_id: str) -> str:
        if area_id in self.area_names:
            return self.area_names[area_id]
        return self.area(area_id.replace("_", " ").title(), area_id)

    def device(self, name: str) -> WorldDevice:
        existing = self.devices.get(name)
        if existing is None:
            existing = WorldDevice(key=name, name=name)
            self.devices[name] = existing
        return existing

    def observe(
        self,
        entity_id: str,
        state: object,
        at: datetime,
        source: str,
        *,
        attributes: Mapping[str, JsonValue] | None = None,
        complete: bool = False,
    ) -> None:
        if not _ENTITY_ID_RE.match(entity_id) or not isinstance(state, str):
            return
        entity = self.entity(entity_id)
        entity.observations.append(
            StateObservation(
                at=at,
                state=state,
                attributes=dict(attributes) if attributes is not None else None,
                attributes_complete=complete and attributes is not None,
                source=source,
            )
        )
        name = (attributes or {}).get("friendly_name")
        if isinstance(name, str) and name and entity.name is None:
            entity.name = name

    # Sources ------------------------------------------------------------------

    def add_listing(self, content: str) -> None:
        for line in content.splitlines():
            match = _LISTING_LINE.match(line.strip())
            if match is None:
                continue
            entity = self.entity(match["entity_id"])
            if match["name"]:
                entity.name = match["name"]
            if match["device"]:
                entity.device = self.device(match["device"]).key
            if match["area"]:
                entity.area = self.area(match["area"])

    def add_event(self, data: Mapping[str, object], at: datetime) -> None:
        event_type = data.get("event_type")
        entity_id = data.get("entity_id")
        if event_type == "automation_triggered" and isinstance(entity_id, str):
            name = _str(data.get("name"))
            entity = self.entity(entity_id)
            if name and entity.name is None:
                entity.name = name
            return
        if event_type != "state_changed" or not isinstance(entity_id, str):
            return
        for key in ("old_state", "new_state"):
            state = data.get(key)
            if not isinstance(state, dict):
                continue
            attributes = state.get("attributes")
            changed = _parse_time(state.get("last_changed")) or at
            self.observe(
                entity_id,
                state.get("state"),
                changed,
                "recent_events",
                attributes=_attribute_dict(attributes),
                complete=isinstance(attributes, dict),
            )

    def add_json_result(self, value: JsonValue, seen: datetime, source: str) -> None:
        for item in _walk(value):
            self._json_item(item, seen, source)

    def _json_item(
        self, item: Mapping[str, JsonValue], seen: datetime, source: str
    ) -> None:
        entity_id = item.get("entity_id")
        if isinstance(entity_id, str) and _ENTITY_ID_RE.match(entity_id):
            self._json_entity(entity_id, item, seen, source)
            return
        device_id = _str(item.get("device_id"))
        name = _str(item.get("name"))
        if device_id and name and ("manufacturer" in item or "model" in item):
            self._json_device(device_id, name, item)

    def _json_entity(
        self,
        entity_id: str,
        item: Mapping[str, JsonValue],
        seen: datetime,
        source: str,
    ) -> None:
        # Configurations and traces name entities without describing them, and
        # may name ones that no longer exist; only a description is evidence.
        if not _EVIDENCE.intersection(item):
            return
        entity = self.entity(entity_id)
        friendly = _str(item.get("friendly_name"))
        if friendly:
            entity.name = entity.name or friendly
        platform = _str(item.get("platform"))
        if platform and platform != "unknown" and "unique_id" in item:
            entity.integration = platform
        area_id = _str(item.get("area_id"))
        if area_id:
            entity.area = self.area_from_id(area_id)
        device_id = _str(item.get("device_id"))
        if device_id:
            self.pending_device_ids.append((entity_id, device_id))
        entry = item.get("entity_entry")
        if isinstance(entry, dict):
            self._json_entity(entity_id, entry, seen, source)
        name = _str(item.get("original_name")) or _str(item.get("name"))
        if name and "platform" in item and entity.name is None:
            entity.name = name
        points = item.get("states")
        if isinstance(points, list):
            for point in points:
                if isinstance(point, dict):
                    changed = _parse_time(point.get("last_changed")) or seen
                    self.observe(entity_id, point.get("state"), changed, source)
            return
        state = item.get("state")
        if not isinstance(state, str):
            return
        attributes = item.get("attributes")
        has_marker = any(
            key in item
            for key in ("last_changed", "attributes", "friendly_name", "last_updated")
        )
        if not has_marker:
            return
        changed = _parse_time(item.get("last_changed")) or seen
        full = isinstance(attributes, dict) and "last_changed" in item
        self.observe(
            entity_id,
            state,
            changed,
            source,
            attributes=attributes if isinstance(attributes, dict) else None,
            complete=full,
        )
        if not isinstance(attributes, dict) and friendly:
            entity.observations[-1].attributes = {"friendly_name": friendly}

    def _json_device(
        self, device_id: str, name: str, item: Mapping[str, JsonValue]
    ) -> None:
        device = self.device(name)
        device.recorded_id = device_id
        self.device_by_id[device_id] = device.key
        device.manufacturer = _str(item.get("manufacturer")) or device.manufacturer
        device.model = _str(item.get("model")) or device.model
        device.sw_version = _str(item.get("sw_version")) or device.sw_version
        area_id = _str(item.get("area_id"))
        if area_id:
            device.area = self.area_from_id(area_id)
        integration = _str(item.get("integration_type"))
        if integration:
            device.integration = integration
        entities = item.get("entities")
        if isinstance(entities, list):
            for listed in entities:
                if isinstance(listed, dict):
                    entity_id = _str(listed.get("entity_id"))
                    if entity_id and _ENTITY_ID_RE.match(entity_id):
                        entity = self.entity(entity_id)
                        entity.device = device.key
                        platform = _str(listed.get("platform"))
                        if platform:
                            entity.integration = platform

    def add_action_result(
        self, arguments: Mapping[str, object], content: str, seen: datetime
    ) -> None:
        domain = _str(arguments.get("domain"))
        action = _str(arguments.get("action"))
        if domain and action and _SERVICE_REF.match(f"{domain}.{action}"):
            self.services.add(f"{domain}.{action}")
        if match := _CHANGED_STATES.search(content):
            for change in _CHANGE.finditer(match["changes"]):
                self.observe(
                    change["entity_id"], change["state"].strip(), seen, "action_result"
                )
        response = _RESPONSE.search(content)
        if domain == "weather" and action == "get_forecasts" and response:
            data = _arguments(arguments.get("service_data"))
            forecast_type = _FORECAST_TYPES.get(str(data.get("type")))
            decoded = _json(response["json"])
            if isinstance(decoded, dict) and forecast_type is not None:
                self.add_forecasts(decoded, forecast_type, seen)

    def add_forecasts(
        self,
        response: Mapping[str, JsonValue],
        forecast_type: ForecastType,
        seen: datetime,
    ) -> None:
        for entity_id, body in response.items():
            if not isinstance(body, dict):
                continue
            forecast = body.get("forecast")
            if not isinstance(forecast, list) or not _ENTITY_ID_RE.match(entity_id):
                continue
            self.entity(entity_id)
            self.forecasts.append(
                WorldForecast(
                    entity_id=entity_id,
                    forecast_type=forecast_type,
                    seen_at=seen,
                    forecast=[f for f in forecast if isinstance(f, dict)],
                )
            )

    def add_config(
        self,
        kind: Literal["automation", "script"],
        identifier: str,
        config: object,
        seen: datetime,
    ) -> None:
        if isinstance(config, str):
            config = _json(config)
        if not isinstance(config, dict):
            return
        entity_id = identifier if identifier.startswith(f"{kind}.") else None
        if entity_id is None:
            alias = _str(config.get("alias"))
            entity_id = f"{kind}.{slugify(alias or identifier)}"
        target = self.automations if kind == "automation" else self.scripts
        target.append(WorldConfig(entity_id=entity_id, seen_at=seen, config=config))
        entity = self.entity(entity_id)
        alias = _str(config.get("alias"))
        if alias and entity.name is None:
            entity.name = alias
        for service in _service_references(config):
            self.services.add(service)

    def add_template(self, template: str, output: str, seen: datetime) -> None:
        for match in _STATE_REPR.finditer(output):
            changed = _parse_time(match["changed"]) or seen
            attributes: Attributes | None = None
            if match["attrs"] and (name := _REPR_FRIENDLY_NAME.search(match["attrs"])):
                attributes = {"friendly_name": name["name"]}
            self.observe(
                match["entity_id"],
                match["state"],
                changed,
                "template",
                attributes=attributes,
            )
        for expression, value in _template_values(template, output):
            self._template_fact(expression, value, seen)

    def _template_fact(self, expression: str, value: str, seen: datetime) -> None:
        expression = expression.strip()
        if match := (_STATES_CALL.match(expression) or _STATES_INDEX.match(expression)):
            entity_id = (
                match[1] if match.re is _STATES_CALL else f"{match[1]}.{match[2]}"
            )
            self.observe(entity_id, value.strip(), seen, "template")
        elif match := _STATES_DOT.match(expression):
            self.observe(f"{match[1]}.{match[2]}", value.strip(), seen, "template")
        elif match := _STATE_ATTR.match(expression):
            self._template_attribute(match[1], match[2], value, seen)
        elif match := _ATTRIBUTES_JSON.match(expression):
            decoded = _json(value)
            entity = self.entities.get(f"{match[1]}.{match[2]}")
            if isinstance(decoded, dict) and entity is not None and entity.observations:
                # Attributes alone carry no state; pair them with the state known
                # closest to that moment.
                closest = min(
                    entity.observations,
                    key=lambda o: abs((o.at - seen).total_seconds()),
                )
                self.observe(
                    entity.entity_id,
                    closest.state,
                    seen,
                    "template",
                    attributes=decoded,
                    complete=True,
                )
        elif match := _INTEGRATION_ENTITIES.match(expression):
            for entity_id in _entity_list(value):
                self.entity(entity_id).integration = match[1]
        elif match := _AREA_ENTITIES.match(expression):
            area = self.area(match[1])
            for entity_id in _entity_list(value):
                entity = self.entity(entity_id)
                entity.area = entity.area or area
        elif match := _AREA_NAME.match(expression):
            if value.strip() not in _UNKNOWN:
                self.entity(match[1]).area = self.area(value.strip())
        elif expression.endswith(".entity_id") or expression.endswith(".name"):
            return

    def _template_attribute(
        self, entity_id: str, attribute: str, value: str, seen: datetime
    ) -> None:
        if value.strip() in {"None", ""}:
            return
        entity = self.entities.get(entity_id)
        if entity is None or not entity.observations:
            return
        closest = min(
            entity.observations, key=lambda o: abs((o.at - seen).total_seconds())
        )
        self.observe(
            entity_id,
            closest.state,
            seen,
            "template",
            attributes={attribute: _literal(value)},
        )

    def add_loop_rows(self, rows: Iterable[Mapping[str, str]], seen: datetime) -> None:
        for row in rows:
            entity_id = row.get("entity_id", "").strip()
            if not _ENTITY_ID_RE.match(entity_id):
                continue
            entity = self.entity(entity_id)
            name = row.get("name") or row.get("attributes.friendly_name")
            if name and name.strip() and entity.name is None:
                entity.name = name.strip()
            state = row.get("state")
            if state is not None:
                self.observe(entity_id, state.strip(), seen, "template")

    def add_live_context(self, content: str, seen: datetime) -> None:
        decoded = _json(content)
        text = decoded.get("result") if isinstance(decoded, dict) else content
        if not isinstance(text, str):
            return
        _, _, body = text.partition("\n")
        try:
            items = yaml.safe_load(body)
        except yaml.YAMLError:
            return
        if isinstance(items, list):
            self.live_context.extend(
                (seen, item) for item in items if isinstance(item, dict)
            )

    # Finishing ----------------------------------------------------------------

    def _resolve_live_context(self) -> None:
        by_name: dict[tuple[str, str], list[str]] = defaultdict(list)
        for entity in self.entities.values():
            names = {
                name
                for o in entity.observations
                if isinstance(name := (o.attributes or {}).get("friendly_name"), str)
            }
            if entity.name:
                names.add(entity.name)
            for name in names:
                by_name[entity.domain, name].append(entity.entity_id)
        for seen, item in self.live_context:
            names = item.get("names")
            domain = item.get("domain")
            if not isinstance(names, str) or not isinstance(domain, str):
                continue
            matches = sorted(set(by_name.get((domain, names), [])))
            if len(matches) != 1:
                continue
            entity = self.entities[matches[0]]
            areas = item.get("areas")
            if isinstance(areas, str) and areas:
                entity.area = entity.area or self.area(areas.split(",")[0].strip())
            state = item.get("state")
            attributes = item.get("attributes")
            self.observe(
                entity.entity_id,
                str(state) if state is not None else None,
                seen,
                "live_context",
                attributes={
                    str(k): _json_safe(v)
                    for k, v in attributes.items()
                    if v is not None
                }
                if isinstance(attributes, dict)
                else None,
            )

    def _resolve_devices(self) -> None:
        for entity_id, device_id in self.pending_device_ids:
            key = self.device_by_id.get(device_id)
            if key is not None:
                self.entities[entity_id].device = key
        # A device sits in the area most of its entities are listed in; an
        # entity keeps its own area only where it differs.
        areas_of: dict[str, Counter[str]] = defaultdict(Counter)
        for entity in self.entities.values():
            if entity.device and entity.area:
                areas_of[entity.device][entity.area] += 1
        for key, device in self.devices.items():
            if device.area is None and areas_of[key]:
                device.area = areas_of[key].most_common(1)[0][0]
        integrations_of: dict[str, Counter[str]] = defaultdict(Counter)
        for entity in self.entities.values():
            if entity.device and entity.integration:
                integrations_of[entity.device][entity.integration] += 1
        for key, device in self.devices.items():
            if device.integration is None and integrations_of[key]:
                device.integration = integrations_of[key].most_common(1)[0][0]
        for entity in self.entities.values():
            device = self.devices.get(entity.device) if entity.device else None
            if device is not None:
                if entity.area == device.area:
                    entity.area = None
                if entity.integration is None:
                    entity.integration = device.integration

    def _location(self) -> tuple[float | None, float | None]:
        home = self.entities.get("zone.home")
        if home is None:
            return None, None
        for observation in sorted(home.observations, key=lambda o: o.at, reverse=True):
            attributes = observation.attributes or {}
            latitude, longitude = (
                attributes.get("latitude"),
                attributes.get("longitude"),
            )
            if isinstance(latitude, (int, float)) and isinstance(
                longitude, (int, float)
            ):
                return float(latitude), float(longitude)
        return None, None

    def _drop_phantoms(self) -> None:
        """Forget ids only ever seen rendering ``unknown`` in a template.

        ``states('x')`` renders ``unknown`` for an id that does not exist, so
        an id nothing else describes is a guess a turn made, not an entity.
        """
        for entity_id in [
            e.entity_id
            for e in self.entities.values()
            if e.observations
            and e.name is None
            and e.device is None
            and e.integration is None
            and e.area is None
            and all(
                o.source == "template" and o.state == "unknown" for o in e.observations
            )
        ]:
            del self.entities[entity_id]

    def finish(self, time_zone: str) -> HomeWorld:
        self._drop_phantoms()
        self._resolve_live_context()
        self._resolve_devices()
        latitude, longitude = self._location()
        entity_domains = {e.domain for e in self.entities.values()}
        services = sorted(
            s
            for s in self.services
            if s.split(".", 1)[0] not in entity_domains or s.startswith("notify.")
        )
        home = self.entities.get("zone.home")
        location_name = self.location_name or (home.name if home else None) or "Home"
        return HomeWorld(
            location_name=location_name,
            time_zone=self.time_zone or time_zone,
            latitude=latitude,
            longitude=longitude,
            areas=sorted(
                (WorldArea(area_id=k, name=v) for k, v in self.area_names.items()),
                key=lambda a: a.area_id,
            ),
            devices=sorted(self.devices.values(), key=lambda d: d.key),
            entities=sorted(self.entities.values(), key=lambda e: e.entity_id),
            forecasts=sorted(self.forecasts, key=lambda f: (f.entity_id, f.seen_at)),
            automations=sorted(
                self.automations, key=lambda a: (a.entity_id, a.seen_at)
            ),
            scripts=sorted(self.scripts, key=lambda s: (s.entity_id, s.seen_at)),
            services=services,
        )


def _walk(value: JsonValue) -> Iterator[Mapping[str, JsonValue]]:
    if isinstance(value, dict):
        yield value
        for child in value.values():
            yield from _walk(child)
    elif isinstance(value, list):
        for child in value:
            yield from _walk(child)


def _entity_list(value: str) -> list[str]:
    parsed = _literal(value)
    if not isinstance(parsed, list):
        return []
    return [v for v in parsed if isinstance(v, str) and _ENTITY_ID_RE.match(v)]


def _service_references(config: JsonValue) -> Iterator[str]:
    for item in _walk(config):
        for key in ("service", "action"):
            value = item.get(key)
            if isinstance(value, str) and _SERVICE_REF.match(value):
                yield value


def _line_pattern(line: str) -> tuple[re.Pattern[str], list[str]] | None:
    parts = _EXPRESSION.split(line)
    literals, expressions = parts[0::2], parts[1::2]
    if not expressions:
        return None
    pattern = "".join(
        re.escape(literal) + ("(.*?)" if index < len(expressions) else "")
        for index, literal in enumerate(literals)
    )
    return re.compile(f"^{pattern}$", re.DOTALL), expressions


def _template_values(template: str, output: str) -> Iterator[tuple[str, str]]:
    """Pair each expression in a straight-line template with what it rendered to.

    A template without control flow renders line for line, so each template
    line becomes a pattern whose literal text must match its output line and
    whose expressions capture their values. A template with loops is left to
    :func:`_loop_rows`.
    """
    if "{%" in template:
        return
    template_lines = template.strip().split("\n")
    output_lines = output.split("\n")
    if len(template_lines) != len(output_lines):
        return
    for template_line, output_line in zip(template_lines, output_lines, strict=True):
        compiled = _line_pattern(template_line.strip())
        if compiled is None:
            continue
        pattern, expressions = compiled
        match = pattern.match(output_line.strip())
        if match is None:
            continue
        yield from zip(expressions, match.groups(), strict=True)


def _loop_row_patterns(template: str) -> list[tuple[re.Pattern[str], list[str]]]:
    """Line patterns for the rows a loop over states renders.

    Two shapes are common: a body line such as ``{{ s.entity_id }}: {{ s.state }}``
    and a ``~`` concatenation such as ``s.entity_id ~ ' | ' ~ s.state`` joined
    into the output. Both become patterns whose captures name loop fields.
    """
    variables = set(_LOOP_VARIABLE.findall(template))
    if not variables:
        return []
    candidates: list[str] = [
        line.strip() for line in template.split("\n") if "{{" in line
    ]
    for chain in _CONCAT_CHAIN.findall(template):
        line = ""
        for token in _CHAIN_TOKEN.findall(chain):
            if token[0] in "'\"":
                line += token[1:-1].encode().decode("unicode_escape")
            else:
                line += "{{ " + token + " }}"
        candidates.append(line)
    patterns: list[tuple[re.Pattern[str], list[str]]] = []
    for line in candidates:
        compiled = _line_pattern(line)
        if compiled is None:
            continue
        pattern, expressions = compiled
        fields: list[str] = []
        for expression in expressions:
            head, _, field = expression.strip().partition(".")
            field = field.split("|", 1)[0].strip()
            fields.append(field if head in variables and field in _LOOP_FIELDS else "")
        if "entity_id" in fields and ("state" in fields or "name" in fields):
            patterns.append((pattern, fields))
    return patterns


def _loop_rows(template: str, output: str) -> list[dict[str, str]]:
    rows: list[dict[str, str]] = []
    patterns = _loop_row_patterns(template)
    if not patterns:
        return rows
    for line in output.split("\n"):
        for pattern, fields in patterns:
            match = pattern.match(line.strip())
            if match is None:
                continue
            rows.append({
                field: value
                for field, value in zip(fields, match.groups(), strict=True)
                if field
            })
            break
    return rows


_JSON_RESULT_TOOLS = {
    "ha_search_entities",
    "ha_search",
    "ha_get_state",
    "ha_call_read_tool",
    "ha_call_write_tool",
    "ha_get_overview",
    "ha_get_integration",
}


def _record(
    builder: _Builder,
    name: str,
    arguments: Mapping[str, object],
    content: str,
    seen: datetime,
) -> None:
    if name == "list_home_assistant_entities":
        builder.add_listing(content)
    elif name == "render_home_assistant_template":
        template = _str(arguments.get("template"))
        if template:
            builder.add_template(template, content, seen)
            builder.add_loop_rows(_loop_rows(template, content), seen)
    elif name == "call_home_assistant_action":
        builder.add_action_result(arguments, content, seen)
    elif name == "GetLiveContext":
        builder.add_live_context(content, seen)
    elif name in {"ha_config_get_automation", "ha_config_set_automation"}:
        _record_config(builder, "automation", arguments, content, seen)
    elif name in _JSON_RESULT_TOOLS:
        inner = _str(arguments.get("name")) if name.startswith("ha_call_") else None
        if inner == "ha_config_get_script":
            _record_config(
                builder, "script", _arguments(arguments.get("arguments")), content, seen
            )
            return
        if inner == "ha_eval_template":
            inner_args = _arguments(arguments.get("arguments"))
            decoded = _json(content)
            result = decoded.get("result") if isinstance(decoded, dict) else None
            template = _str(inner_args.get("template"))
            if template and isinstance(result, str):
                builder.add_template(template, result, seen)
            return
        decoded = _json(content)
        if decoded is None:
            return
        if name == "ha_get_overview" and isinstance(decoded, dict):
            info = decoded.get("system_info")
            if isinstance(info, dict):
                builder.location_name = (
                    _str(info.get("location_name")) or builder.location_name
                )
                builder.time_zone = _str(info.get("time_zone")) or builder.time_zone
        builder.add_json_result(decoded, seen, name if inner is None else inner)


def _record_config(
    builder: _Builder,
    kind: Literal["automation", "script"],
    arguments: Mapping[str, object],
    content: str,
    seen: datetime,
) -> None:
    identifier = _str(arguments.get("identifier")) or _str(arguments.get("script_id"))
    if "config" in arguments and identifier:
        builder.add_config(kind, identifier, arguments["config"], seen)
        return
    decoded = _json(content)
    if not isinstance(decoded, dict) or decoded.get("success") is False:
        return
    config = decoded.get("config")
    # Some results nest the configuration inside a second envelope.
    while isinstance(config, dict) and isinstance(config.get("config"), dict):
        config = config["config"]
    identifier = (
        _str(decoded.get(f"{kind}_id"))
        or identifier
        or (
            f"{kind}.{slugify(str(config.get('alias')))}"
            if isinstance(config, dict)
            else None
        )
    )
    if identifier:
        builder.add_config(kind, identifier, config, seen)


def build_home_world(
    grouped: Mapping[tuple[str, str], Iterable[MessageHistoryRow]],
    recent_events: Iterable[Mapping[str, object]],
    *,
    time_zone: str = "UTC",
) -> HomeWorld:
    """Pool every recorded Home Assistant result and state event into one house.

    ``recent_events`` are rows of the ``recent_events`` table: mappings with
    ``event_data`` (the event as JSON) and ``timestamp``. ``time_zone`` is used
    when no recorded result names the instance's own.
    """
    builder = _Builder()
    timed = [
        (at, event.get("event_data"))
        for event in recent_events
        if isinstance(at := event.get("timestamp"), datetime)
    ]
    for at, raw in sorted(timed, key=lambda pair: pair[0]):
        data = _json(raw) if isinstance(raw, str) else raw
        if isinstance(data, dict):
            builder.add_event(data, at)
    results: list[tuple[datetime, str, dict[str, object], str]] = []
    for rows in grouped.values():
        calls: dict[str, tuple[str, dict[str, object]]] = {}
        for row in rows:
            if row["role"] == "assistant":
                for item in row["tool_calls"] or []:
                    calls[item.id] = (
                        item.function.name,
                        _arguments(item.function.arguments),
                    )
            elif row["role"] == "tool" and row["content"]:
                name, arguments = calls.get(
                    row["tool_call_id"] or "", (row["tool_name"] or "", {})
                )
                content = row["content"] or ""
                if content.startswith("Error"):
                    continue
                results.append((row["timestamp"], name, arguments, content))
    # Results apply in time order, so attribute-only lookups can pair with the
    # state known closest to them.
    for seen, name, arguments, content in sorted(results, key=lambda r: r[0]):
        _record(builder, name, arguments, content, seen)
    return builder.finish(time_zone)
