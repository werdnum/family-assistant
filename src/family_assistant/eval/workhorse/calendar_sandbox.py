"""Serve the household's CalDAV calendar and iCal feeds from a local Radicale.

The deployment's writable calendar is CalDAV and its read-only feeds are
iCal URLs. Both are served by one Radicale process seeded with the calendar
world as of the trial's moment: the CalDAV calendar is a collection the
shipped CalDAV client reads and writes, and each feed is a collection whose
URL, fetched with a plain GET, returns the whole calendar as iCalendar, as a
feed URL does. Calendar ids and names are the deployment's, so recorded ids
in skills and history still resolve.

Google calendars are not here; they are served by the fake Google (see
:mod:`family_assistant.eval.workhorse.fake_google_calendar`).
"""

from __future__ import annotations

import contextlib
import json
import re
import socket
import subprocess
import sys
import time as wall
from dataclasses import dataclass
from datetime import UTC, date, datetime
from typing import TYPE_CHECKING

from icalendar import Calendar, Event
from pydantic import SecretStr

from family_assistant.config_models import (
    CalDAVCalendarConfig,
    CalDAVConfig,
    CalendarConfig,
    ICalConfig,
    ICalFeedConfig,
)

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path

    from family_assistant.eval.workhorse.calendar_world import CalendarWorld

__all__ = ["CalendarServer", "calendar_server", "write_calendar_storage"]

_USER = "eval"
_PASSWORD = "eval-calendar"


def _value(text: str) -> date | datetime:
    if len(text) == 10:
        return date.fromisoformat(text)
    # Written in UTC: a fixed-offset zone becomes a TZID with no VTIMEZONE,
    # which Radicale cannot resolve when filtering by time range.
    return datetime.fromisoformat(text).astimezone(UTC)


def _item_name(uid: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]", "_", uid)[:200] + ".ics"


def write_calendar_storage(
    world: CalendarWorld, moment: datetime, storage: Path
) -> dict[str, str]:
    """Seed Radicale's filesystem storage; return {source id: collection path}."""
    root = storage / "collection-root" / _USER
    collections: dict[str, str] = {}
    for calendar in world.calendars:
        if calendar.kind == "google":
            continue
        name = re.sub(r"[^A-Za-z0-9._-]", "_", calendar.source_id)
        folder = root / name
        folder.mkdir(parents=True, exist_ok=True)
        (folder / ".Radicale.props").write_text(
            json.dumps({"tag": "VCALENDAR", "D:displayname": calendar.name}),
            encoding="utf-8",
        )
        collections[calendar.source_id] = f"/{_USER}/{name}/"
    for event in world.events:
        path = collections.get(event.source_id)
        state = event.as_of(moment)
        if path is None or state is None:
            continue
        component = Event()
        component.add("uid", event.uid)
        component.add("summary", state.summary)
        component.add("dtstart", _value(state.start))
        component.add("dtend", _value(state.end))
        component.add("dtstamp", moment)
        if event.occurrence_starts:
            component.add("rdate", [_value(start) for start in event.occurrence_starts])
        if state.description:
            component.add("description", state.description)
        if state.location:
            component.add("location", state.location)
        calendar_object = Calendar()
        calendar_object.add("prodid", "-//family-assistant workhorse eval//EN")
        calendar_object.add("version", "2.0")
        calendar_object.add_component(component)
        folder = root / path.strip("/").split("/")[-1]
        (folder / _item_name(event.uid)).write_bytes(calendar_object.to_ical())
    return collections


@dataclass(frozen=True)
class CalendarServer:
    base_url: str
    config: CalendarConfig


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def _wait_for_port(
    port: int, process: subprocess.Popen[bytes], deadline: float
) -> None:
    while wall.monotonic() < deadline:
        if process.poll() is not None:
            raise RuntimeError("Radicale exited during startup")
        with (
            contextlib.suppress(OSError),
            socket.create_connection(("127.0.0.1", port), timeout=0.5),
        ):
            return
        wall.sleep(0.2)
    raise RuntimeError("Radicale did not start listening in time")


@contextlib.contextmanager
def calendar_server(
    world: CalendarWorld, moment: datetime, work_dir: Path
) -> Iterator[CalendarServer]:
    """Run Radicale seeded with ``world`` as of ``moment`` for the block.

    ``work_dir`` must be on a local filesystem: Radicale fsyncs directories,
    which shared and network mounts refuse, failing every request.
    """
    storage = work_dir / "radicale"
    collections = write_calendar_storage(world, moment, storage)
    users = work_dir / "radicale-users"
    users.write_text(f"{_USER}:{_PASSWORD}\n", encoding="utf-8")
    # The household's login owns everything; feeds are public URLs in the
    # deployment, so anyone may read those collections, as a feed reader does.
    feeds = "|".join(
        re.escape(collections[c.source_id].strip("/").split("/")[-1])
        for c in world.calendars
        if c.kind == "ical"
    )
    rights = work_dir / "radicale-rights"
    rights.write_text(
        f"[owner]\nuser: {_USER}\ncollection: .*\npermissions: RrWw\n\n"
        + (
            f"[feeds]\nuser: .*\ncollection: {_USER}/({feeds})\npermissions: r\n"
            if feeds
            else ""
        ),
        encoding="utf-8",
    )
    port = _free_port()
    config_file = work_dir / "radicale.conf"
    config_file.write_text(
        "[server]\n"
        f"hosts = 127.0.0.1:{port}\n"
        "[auth]\n"
        "type = htpasswd\n"
        f"htpasswd_filename = {users}\n"
        "htpasswd_encryption = plain\n"
        "[rights]\n"
        "type = from_file\n"
        f"file = {rights}\n"
        "[storage]\n"
        f"filesystem_folder = {storage}\n"
        "[logging]\n"
        "level = warning\n",
        encoding="utf-8",
    )
    process = subprocess.Popen([
        sys.executable,
        "-m",
        "radicale",
        "--config",
        str(config_file),
    ])
    try:
        _wait_for_port(port, process, wall.monotonic() + 30)
        base_url = f"http://127.0.0.1:{port}"
        caldav = [
            CalDAVCalendarConfig(
                url=f"{base_url}{collections[c.source_id]}",
                id=c.source_id,
                name=c.name,
                default=c.is_default,
            )
            for c in world.calendars
            if c.kind == "caldav"
        ]
        feeds = [
            ICalFeedConfig(
                url=f"{base_url}{collections[c.source_id]}", id=c.source_id, name=c.name
            )
            for c in world.calendars
            if c.kind == "ical"
        ]
        yield CalendarServer(
            base_url=base_url,
            config=CalendarConfig(
                caldav=CalDAVConfig(
                    username=_USER,
                    password=SecretStr(_PASSWORD),
                    calendar_urls=list(caldav),
                    base_url=base_url,
                )
                if caldav
                else None,
                ical=ICalConfig(urls=list(feeds)) if feeds else None,
            ),
        )
    finally:
        process.terminate()
        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            process.kill()
