"""Run a real ``hass`` that looks like the household's home at a case's moment.

The house is configured from the :class:`~family_assistant.eval.workhorse.ha_world.HomeWorld`
as it stood at the moment:

- device and sensor entities (lights, climate, the vacuum, sensors, ...) are
  served by the ``eval_home`` custom integration from a seed file, one config
  entry per production integration, with production entity ids, names,
  attributes, devices and areas;
- helpers (``input_*``), people, zones, automations and scripts are Home
  Assistant's own integrations, configured from the recorded states and
  configurations, so their services behave exactly as at home;
- the recorder database is backfilled with every recorded state up to the
  moment, so history queries answer from the record;
- the process clock starts at the moment and runs on (``time-machine``), so
  ``now()`` in a template and ``sun.sun`` agree with the case.

Every service call is logged to ``service_calls.jsonl`` in the work directory.
Entities of domains no integration here can serve (``conversation``, ``tts``)
and any that failed to come up are listed in ``unserved.json``: what a trial
touching them got is a harness gap, not a model error.
"""

from __future__ import annotations

import contextlib
import json
import logging
import shutil
import socket
import subprocess
import sys
import time as wall
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, NamedTuple

import httpx
import yaml
from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from websockets.sync.client import connect

from family_assistant.eval.workhorse.ha_world import EntityState

if TYPE_CHECKING:
    from collections.abc import Iterator, Mapping

    from pydantic import JsonValue

    from family_assistant.eval.workhorse.ha_world import (
        HistoryPoint,
        HomeSnapshot,
        HomeWorld,
        WorldConfig,
    )

__all__ = [
    "SERVICE_LOG",
    "UNSERVED_REPORT",
    "HomeAssistantServer",
    "home_assistant_server",
    "write_home_assistant_config",
]

logger = logging.getLogger(__name__)

SERVICE_LOG = "service_calls.jsonl"
UNSERVED_REPORT = "unserved.json"
_COMPONENTS = Path(__file__).parent / "ha_sandbox" / "custom_components"
_SEED_FILE = "eval_home_world.json"
_USERNAME = "eval"
_PASSWORD = "eval-home-assistant"
# Served by eval_home as entity platforms (mirrors its const.PLATFORMS).
_PLATFORM_DOMAINS = frozenset({
    "binary_sensor",
    "button",
    "calendar",
    "camera",
    "climate",
    "cover",
    "date",
    "datetime",
    "device_tracker",
    "event",
    "fan",
    "geo_location",
    "image",
    "light",
    "lock",
    "media_player",
    "notify",
    "number",
    "remote",
    "select",
    "sensor",
    "siren",
    "switch",
    "text",
    "time",
    "update",
    "vacuum",
    "valve",
    "weather",
})
# Served by Home Assistant's own integrations, configured here.
_NATIVE_DOMAINS = frozenset({
    "automation",
    "counter",
    "input_boolean",
    "input_button",
    "input_datetime",
    "input_number",
    "input_select",
    "input_text",
    "person",
    "schedule",
    "script",
    "sun",
    "timer",
    "zone",
})
# Starts the process clock at the moment and lets it run, then runs hass.
# The camera integration imports TurboJPEG, which it uses only to scale
# frames; where the library is not installed a stand-in lets cameras load.
_RUNNER = """
import importlib.util
import sys
import types
from datetime import datetime

import time_machine

if importlib.util.find_spec("turbojpeg") is None:
    stub = types.ModuleType("turbojpeg")

    class TurboJPEG:
        def __init__(self, *args, **kwargs):
            raise OSError("TurboJPEG is not installed in the sandbox")

    stub.TurboJPEG = TurboJPEG
    sys.modules["turbojpeg"] = stub

time_machine.travel(datetime.fromisoformat(sys.argv[1]), tick=True).start()
from homeassistant.__main__ import main

sys.argv = ["hass", *sys.argv[2:]]
sys.exit(main())
"""


class HomeAssistantServer(NamedTuple):
    base_url: str
    token: str


# --- Configuration ----------------------------------------------------------


def _object_id(entity: EntityState) -> str:
    return entity.entity_id.split(".", 1)[1]


def _number(value: object) -> float | None:
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


def _known(state: str) -> str | None:
    return None if state in {"unknown", "unavailable"} else state


def _helper(entity: EntityState) -> dict[str, object]:
    """One helper's YAML configuration, its recorded state as the initial one."""
    attributes = entity.attributes
    config: dict[str, object] = {"name": entity.name or _object_id(entity)}
    if isinstance(icon := attributes.get("icon"), str):
        config["icon"] = icon
    domain = entity.entity_id.split(".", 1)[0]
    state = _known(entity.state)
    if domain == "input_boolean":
        config["initial"] = state == "on"
    elif domain == "input_number":
        value = _number(state)
        minimum = _number(attributes.get("min"))
        maximum = _number(attributes.get("max"))
        minimum = minimum if minimum is not None else min(0.0, value or 0.0)
        maximum = maximum if maximum is not None else max(100.0, value or 0.0)
        config.update(min=minimum, max=maximum)
        if (step := _number(attributes.get("step"))) is not None:
            config["step"] = step
        if attributes.get("mode") in {"box", "slider"}:
            config["mode"] = attributes["mode"]
        if isinstance(unit := attributes.get("unit_of_measurement"), str):
            config["unit_of_measurement"] = unit
        if value is not None and minimum <= value <= maximum:
            config["initial"] = value
    elif domain == "input_select":
        raw = attributes.get("options")
        options = [str(o) for o in raw] if isinstance(raw, list) else []
        if state is not None and state not in options:
            options.append(state)
        config["options"] = options or ["unknown"]
        if state is not None:
            config["initial"] = state
    elif domain == "input_text":
        for key in ("min", "max"):
            if (value := _number(attributes.get(key))) is not None:
                config[key] = int(value)
        if attributes.get("mode") in {"text", "password"}:
            config["mode"] = attributes["mode"]
        if state is not None:
            config["initial"] = state
    elif domain == "input_datetime":
        has_date = attributes.get("has_date")
        has_time = attributes.get("has_time")
        if not isinstance(has_date, bool) or not isinstance(has_time, bool):
            has_date = state is not None and "-" in state
            has_time = state is None or ":" in state
        config.update(has_date=has_date, has_time=has_time)
        if state is not None:
            config["initial"] = state
    elif domain == "counter":
        if (value := _number(state)) is not None:
            config["initial"] = int(value)
        for key in ("step", "minimum", "maximum"):
            if (value := _number(attributes.get(key))) is not None:
                config[key] = int(value)
    elif domain == "timer" and isinstance(duration := attributes.get("duration"), str):
        config["duration"] = duration
    return config


def _zone(entity: EntityState) -> dict[str, object] | None:
    attributes = entity.attributes
    latitude = _number(attributes.get("latitude"))
    longitude = _number(attributes.get("longitude"))
    if latitude is None or longitude is None:
        return None
    zone: dict[str, object] = {
        "name": entity.name or _object_id(entity),
        "latitude": latitude,
        "longitude": longitude,
        "radius": _number(attributes.get("radius")) or 100.0,
        "passive": bool(attributes.get("passive", False)),
    }
    if isinstance(icon := attributes.get("icon"), str):
        zone["icon"] = icon
    return zone


def _person_tracker(
    person: EntityState, served: Mapping[str, EntityState]
) -> tuple[str, EntityState | None]:
    """The tracker a person follows, inventing one if none recorded is served.

    A person's state comes from its trackers. Only the one production reported
    as the person's source is attached, so the person reads as recorded.
    """
    attributes = person.attributes
    candidates: list[str] = []
    if isinstance(source := attributes.get("source"), str):
        candidates.append(source)
    if isinstance(trackers := attributes.get("device_trackers"), list):
        candidates.extend(t for t in trackers if isinstance(t, str))
    for candidate in candidates:
        if candidate in served:
            return candidate, None
    invented = EntityState(
        entity_id=f"device_tracker.{_object_id(person)}_location",
        state=person.state,
        attributes={
            key: attributes[key]
            for key in ("latitude", "longitude", "gps_accuracy")
            if key in attributes
        },
        name=f"{person.name or _object_id(person)} location",
        integration=None,
        device=None,
        area=None,
        world_built=True,
    )
    return invented.entity_id, invented


def _automation_entries(
    snapshot: HomeSnapshot, automations: list[EntityState]
) -> tuple[list[dict[str, JsonValue]], list[dict[str, str]]]:
    """automations.yaml, and the renames that give each its production id."""
    configs: dict[str, WorldConfig] = {c.entity_id: c for c in snapshot.automations}
    entries: list[dict[str, JsonValue]] = []
    renames: list[dict[str, str]] = []
    for entity in automations:
        recorded = configs.get(entity.entity_id)
        if recorded is not None:
            # The editor's category is registry metadata ha-mcp returns with
            # the configuration; the YAML schema rejects it.
            config = {k: v for k, v in recorded.config.items() if k != "category"}
        else:
            config = {
                "alias": entity.name or _object_id(entity),
                "description": "Configuration not recorded.",
                "triggers": [],
                "actions": [],
            }
        identifier = config.get("id")
        if not isinstance(identifier, str) or not identifier:
            identifier = f"eval_{_object_id(entity)}"
            config["id"] = identifier
        if entity.state == "off":
            config["initial_state"] = False
        entries.append(config)
        renames.append({
            "domain": "automation",
            "unique_id": identifier,
            "entity_id": entity.entity_id,
        })
    return entries, renames


def _script_entries(
    snapshot: HomeSnapshot, scripts: list[EntityState]
) -> dict[str, dict[str, JsonValue]]:
    configs: dict[str, WorldConfig] = {c.entity_id: c for c in snapshot.scripts}
    entries: dict[str, dict[str, JsonValue]] = {}
    for entity in scripts:
        recorded = configs.get(entity.entity_id)
        if recorded is not None:
            config = {k: v for k, v in recorded.config.items() if k != "id"}
        else:
            config = {"alias": entity.name or _object_id(entity), "sequence": []}
        entries[_object_id(entity)] = config
    return entries


def write_home_assistant_config(
    snapshot: HomeSnapshot, config_dir: Path, *, port: int, service_log: Path
) -> tuple[list[str], list[str]]:
    """Write a configuration directory for ``snapshot``; return (served, unserved) ids."""
    config_dir.mkdir(parents=True, exist_ok=True)
    for component in ("eval_home", "frontend"):
        shutil.copytree(
            _COMPONENTS / component,
            config_dir / "custom_components" / component,
            dirs_exist_ok=True,
            ignore=shutil.ignore_patterns("__pycache__"),
        )
    by_domain: dict[str, list[EntityState]] = {}
    for entity in snapshot.entities:
        by_domain.setdefault(entity.entity_id.split(".", 1)[0], []).append(entity)
    platform_entities = [e for d in _PLATFORM_DOMAINS for e in by_domain.get(d, [])]
    served_platform = {e.entity_id: e for e in platform_entities}

    people: list[dict[str, object]] = []
    renames: list[dict[str, str]] = []
    for person in by_domain.get("person", []):
        tracker, invented = _person_tracker(person, served_platform)
        if invented is not None:
            platform_entities.append(invented)
            served_platform[invented.entity_id] = invented
        people.append({
            "id": _object_id(person),
            "name": person.name or _object_id(person),
            "device_trackers": [tracker],
        })
        renames.append({
            "domain": "person",
            "unique_id": _object_id(person),
            "entity_id": person.entity_id,
        })

    home = next(
        (e for e in by_domain.get("zone", []) if e.entity_id == "zone.home"), None
    )
    home_zone = _zone(home) if home is not None else None
    core: dict[str, object] = {
        "name": snapshot.location_name,
        "time_zone": snapshot.time_zone,
        "unit_system": "metric",
        "latitude": snapshot.latitude
        if snapshot.latitude is not None
        else (home_zone or {}).get("latitude", 0.0),
        "longitude": snapshot.longitude
        if snapshot.longitude is not None
        else (home_zone or {}).get("longitude", 0.0),
        "elevation": 0,
        "auth_providers": [{"type": "homeassistant"}],
    }
    if home_zone is not None:
        core["radius"] = home_zone["radius"]
    config: dict[str, object] = {
        "homeassistant": core,
        "http": {"server_host": "127.0.0.1", "server_port": port},
        "api": {},
        "websocket_api": {},
        "onboarding": {},
        "config": {},
        "search": {},
        "history": {},
        "sun": {},
        "recorder": {
            "db_url": f"sqlite:///{config_dir / 'home-assistant_v2.db'}",
            "auto_purge": False,
            "commit_interval": 1,
        },
        "eval_home": {},
        "person": people,
        "zone": [
            z
            for e in by_domain.get("zone", [])
            if e.entity_id != "zone.home" and (z := _zone(e)) is not None
        ],
    }
    for domain in (
        "input_boolean",
        "input_number",
        "input_select",
        "input_text",
        "input_datetime",
        "input_button",
        "counter",
        "timer",
        "schedule",
    ):
        if entities := by_domain.get(domain):
            config[domain] = {_object_id(e): _helper(e) for e in entities}

    automations, automation_renames = _automation_entries(
        snapshot, by_domain.get("automation", [])
    )
    renames.extend(automation_renames)
    (config_dir / "automations.yaml").write_text(
        yaml.safe_dump(automations, allow_unicode=True, sort_keys=False),
        encoding="utf-8",
    )
    (config_dir / "scripts.yaml").write_text(
        yaml.safe_dump(
            _script_entries(snapshot, by_domain.get("script", [])),
            allow_unicode=True,
            sort_keys=False,
        ),
        encoding="utf-8",
    )
    (config_dir / "configuration.yaml").write_text(
        yaml.safe_dump(config, allow_unicode=True, sort_keys=False)
        + "automation: !include automations.yaml\n"
        + "script: !include scripts.yaml\n",
        encoding="utf-8",
    )

    seed = snapshot.model_dump(mode="json", exclude={"history"})
    seed["entities"] = [e.model_dump(mode="json") for e in platform_entities]
    seed["service_log"] = str(service_log)
    seed["renames"] = renames
    (config_dir / _SEED_FILE).write_text(
        json.dumps(seed, ensure_ascii=False), encoding="utf-8"
    )

    served = sorted(
        {e.entity_id for e in platform_entities}
        | {
            e.entity_id
            for d in _NATIVE_DOMAINS - {"sun"}
            for e in by_domain.get(d, [])
            if d != "zone" or e.entity_id == "zone.home" or _zone(e) is not None
        }
    )
    unserved = sorted(
        {e.entity_id for e in snapshot.entities} - set(served) - {"sun.sun"}
    )
    return served, unserved


# --- Recorder history ---------------------------------------------------------


def write_recorder_history(
    history: Mapping[str, list[HistoryPoint]], database: Path, moment: datetime
) -> int:
    """Create the recorder database with every recorded state up to ``moment``.

    The schema is Home Assistant's own, created and stamped as the recorder
    stamps a new database, so the recorder adopts it on startup.
    """
    # Imported here: Home Assistant's recorder is a heavy import the world
    # builder and the rest of this module do not need.
    from homeassistant.components.recorder.db_schema import (  # noqa: PLC0415
        SCHEMA_VERSION,
        Base,
        SchemaChanges,
        StateAttributes,
        States,
        StatesMeta,
        StatisticsRuns,
    )

    engine = create_engine(f"sqlite:///{database}")
    rows = 0
    try:
        Base.metadata.create_all(engine)
        with Session(engine) as session:
            session.add(StatisticsRuns(start=moment.astimezone(UTC)))
            session.add(SchemaChanges(schema_version=SCHEMA_VERSION))
            attribute_ids: dict[bytes, int] = {}
            for entity_id, points in sorted(history.items()):
                meta = StatesMeta(entity_id=entity_id)
                session.add(meta)
                session.flush()
                previous: int | None = None
                for point in points:
                    shared = json.dumps(
                        point.attributes, separators=(",", ":"), ensure_ascii=False
                    ).encode()
                    attributes_id = attribute_ids.get(shared)
                    if attributes_id is None:
                        attributes = StateAttributes(
                            shared_attrs=shared.decode(),
                            hash=StateAttributes.hash_shared_attrs_bytes(shared),
                        )
                        session.add(attributes)
                        session.flush()
                        attributes_id = attribute_ids[shared] = attributes.attributes_id
                    at = point.at.timestamp()
                    state = States(
                        state=point.state[:255],
                        last_changed_ts=at,
                        last_updated_ts=at,
                        old_state_id=previous,
                        attributes_id=attributes_id,
                        origin_idx=0,
                        metadata_id=meta.metadata_id,
                    )
                    session.add(state)
                    session.flush()
                    previous = state.state_id
                    rows += 1
            session.commit()
    finally:
        engine.dispose()
    return rows


# --- Process ------------------------------------------------------------------


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def _wait_for_http(
    client: httpx.Client, process: subprocess.Popen[bytes], deadline: float
) -> None:
    while wall.monotonic() < deadline:
        if process.poll() is not None:
            raise RuntimeError("Home Assistant exited during startup")
        with contextlib.suppress(httpx.HTTPError):
            if client.get("/api/").status_code < 500:
                return
        wall.sleep(0.5)
    raise RuntimeError("Home Assistant did not start serving HTTP in time")


def _onboard(client: httpx.Client, base_url: str) -> str:
    """Create the owner account and return a long-lived access token."""
    response = client.post(
        "/api/onboarding/users",
        json={
            "name": "Eval",
            "username": _USERNAME,
            "password": _PASSWORD,
            "language": "en",
            "client_id": f"{base_url}/",
        },
    )
    response.raise_for_status()
    code = response.json()["auth_code"]
    response = client.post(
        "/auth/token",
        data={
            "grant_type": "authorization_code",
            "code": code,
            "client_id": f"{base_url}/",
        },
    )
    response.raise_for_status()
    access_token = response.json()["access_token"]
    websocket_url = base_url.replace("http://", "ws://") + "/api/websocket"
    with connect(websocket_url) as websocket:
        json.loads(websocket.recv())
        websocket.send(json.dumps({"type": "auth", "access_token": access_token}))
        if json.loads(websocket.recv()).get("type") != "auth_ok":
            raise RuntimeError("Home Assistant refused the onboarding token")
        websocket.send(
            json.dumps({
                "id": 1,
                "type": "auth/long_lived_access_token",
                "client_name": "workhorse eval",
                "lifespan": 3650,
            })
        )
        reply = json.loads(websocket.recv())
    if not reply.get("success"):
        raise RuntimeError(f"Could not create a long-lived token: {reply}")
    return str(reply["result"])


def _wait_for_entities(
    client: httpx.Client,
    expected: set[str],
    process: subprocess.Popen[bytes],
    deadline: float,
    settle_seconds: float = 10.0,
) -> set[str]:
    """Wait until every expected entity is up, or none has appeared for a while.

    Returns the expected ids that never appeared. An entity can legitimately
    fail to come up (a recorded configuration Home Assistant rejects); that is
    reported, not fatal.
    """
    seen: set[str] = set()
    last_growth = wall.monotonic()
    running = False
    while wall.monotonic() < deadline:
        if process.poll() is not None:
            raise RuntimeError("Home Assistant exited during startup")
        with contextlib.suppress(httpx.HTTPError):
            states = client.get("/api/states").json()
            present = {s["entity_id"] for s in states} & expected
            if len(present) > len(seen):
                last_growth = wall.monotonic()
            seen = present
            running = client.get("/api/config").json().get("state") == "RUNNING"
        if seen == expected:
            return set()
        if running and wall.monotonic() - last_growth > settle_seconds:
            break
        wall.sleep(0.5)
    return expected - seen


@contextlib.contextmanager
def home_assistant_server(
    world: HomeWorld,
    moment: datetime,
    work_dir: Path,
    *,
    with_history: bool = True,
    freeze_clock: bool = True,
    startup_timeout: float = 180.0,
) -> Iterator[HomeAssistantServer]:
    """Run ``hass`` seeded with ``world`` as of ``moment`` for the block.

    ``work_dir`` should be on a local filesystem: the recorder's SQLite
    database does not tolerate shared or network mounts.
    """
    snapshot = world.as_of(moment)
    config_dir = work_dir / "config"
    port = _free_port()
    work_dir.mkdir(parents=True, exist_ok=True)
    service_log = work_dir / SERVICE_LOG
    service_log.touch()
    served, unserved = write_home_assistant_config(
        snapshot, config_dir, port=port, service_log=service_log
    )
    if with_history:
        rows = write_recorder_history(
            snapshot.history, config_dir / "home-assistant_v2.db", moment
        )
        logger.info("Backfilled %d recorded states into the recorder", rows)
    log_file = work_dir / "home-assistant.log"
    arguments = ["-c", str(config_dir), "--skip-pip", "--log-file", str(log_file)]
    command = (
        [sys.executable, "-c", _RUNNER, moment.isoformat(), *arguments]
        if freeze_clock
        else [sys.executable, "-m", "homeassistant", *arguments]
    )
    with (work_dir / "hass-output.log").open("wb") as output:
        process = subprocess.Popen(command, stdout=output, stderr=subprocess.STDOUT)
    base_url = f"http://127.0.0.1:{port}"
    try:
        deadline = wall.monotonic() + startup_timeout
        with httpx.Client(base_url=base_url, timeout=10) as client:
            _wait_for_http(client, process, deadline)
            token = _onboard(client, base_url)
            client.headers["Authorization"] = f"Bearer {token}"
            missing = _wait_for_entities(client, set(served), process, deadline)
        if missing:
            logger.warning(
                "%d entities did not come up in the sandbox Home Assistant; see %s",
                len(missing),
                log_file,
            )
        (work_dir / UNSERVED_REPORT).write_text(
            json.dumps(
                {
                    "unsupported_domain": unserved,
                    "failed_to_start": sorted(missing),
                    "world_built": sorted(
                        e.entity_id for e in snapshot.entities if e.world_built
                    ),
                },
                indent=1,
            ),
            encoding="utf-8",
        )
        yield HomeAssistantServer(base_url=base_url, token=token)
    finally:
        process.terminate()
        try:
            process.wait(timeout=20)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait()
