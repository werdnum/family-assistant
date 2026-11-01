"""Serve the household's Tuit from a real, locally running Tuit.

Tuit (Node and PostgreSQL) runs from a checkout of its source with its own
database cluster under the trial's work directory, and is seeded with the
Tuit world as of the trial's moment through Tuit's own REST API -- the same
domain operations its MCP tools call -- by the people and agents who did each
thing, with Tuit's test clock set to when they did it. A task is created when
it was created, its notes are checkpointed when they were written, its links
attached when they were added, and it is then brought to its recorded state
as of the moment. Its history therefore reads as it would have, and
everything time-driven (expiry, availability, routines) is Tuit's own.

Two things go around the API, because Tuit offers no way to do them:

- **Task and queue ids.** Tuit picks ids itself; recorded turns, briefs and
  skills name the recorded ids. Once seeded, ids are renamed to the recorded
  ones in the database.
- **Credentials.** Tokens are minted with Tuit's operator command
  (``npm run admin -- mint-token``), as the deployment's were.

The assistant gets one ``agent:family-assistant`` token per household member,
which passes the deployment's ``/api/me`` identity check. Tuit's clock is
then held at the moment and, by default, ticks forward with wall time.
"""

from __future__ import annotations

import contextlib
import json
import logging
import os
import shutil
import socket
import subprocess
import threading
import time as wall
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, cast

import httpx

from family_assistant.config_models import MCPServerConfig
from family_assistant.eval.workhorse.tuit_world import FA_AGENT

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator, Mapping

    from family_assistant.eval.workhorse.tuit_world import (
        ActivityEntry,
        TaskSnapshot,
        TuitWorld,
        WorldTask,
    )

__all__ = ["TUIT_DIR_ENV", "TuitSeedError", "TuitServer", "tuit_server"]

logger = logging.getLogger(__name__)

TUIT_DIR_ENV = "FA_EVAL_TUIT_DIR"
_PG_BIN_ENV = "FA_EVAL_PG_BIN"
_DEFAULT_PG_BIN = Path("/opt/homebrew/opt/postgresql@18/bin")
_NOTE_KINDS = frozenset({"note", "research", "decision", "attempt"})
_CLOSED = frozenset({"done", "expired", "shelved"})
_FIELDS = (
    "title",
    "brief",
    "next_action",
    "done_means",
    "visibility",
    "area",
    "requires",
    "prefers",
    "recurrence",
    "deadline",
    "expires",
    "available_rule",
    "target_rule",
)

Principal = tuple[str, str | None]


class TuitSeedError(RuntimeError):
    """Tuit refused an operation the seeding depends on."""


@dataclass
class TuitServer:
    base_url: str
    mcp_url: str
    # Tuit user id -> the assistant's agent:family-assistant token for them.
    tokens: dict[str, str]
    # The ``mcp_config.mcpServers`` entry the deployment uses, pointed here.
    server_config: MCPServerConfig
    # Environment variables the entry's ``user_auth`` reads tokens from.
    environment: dict[str, str]
    _client: httpx.Client = field(repr=False)

    def set_clock(self, moment: datetime) -> None:
        _set_clock(self._client, moment)


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def _iso(moment: datetime) -> str:
    return moment.isoformat().replace("+00:00", "Z")


def _set_clock(client: httpx.Client, moment: datetime) -> None:
    client.post("/__test/clock", json={"now": _iso(moment)}).raise_for_status()


def _pg_bin() -> Path:
    configured = os.environ.get(_PG_BIN_ENV)
    if configured:
        return Path(configured)
    found = shutil.which("initdb")
    return Path(found).parent if found else _DEFAULT_PG_BIN


def _tuit_dir(tuit_dir: Path | None) -> Path:
    resolved = tuit_dir or (
        Path(os.environ[TUIT_DIR_ENV]) if os.environ.get(TUIT_DIR_ENV) else None
    )
    if resolved is None or not (resolved / "node_modules").is_dir():
        raise RuntimeError(
            f"Tuit needs a checkout with its dependencies installed: clone "
            f"https://github.com/werdnum/tuit, run `npm ci --omit=dev` there, and "
            f"pass tuit_dir or set {TUIT_DIR_ENV}"
        )
    return resolved


@contextlib.contextmanager
def _postgres(data: Path, log: Path) -> Iterator[int]:
    """A fresh PostgreSQL cluster on a loopback port, stopped on exit."""
    pg_bin = _pg_bin()
    env = {**os.environ, "LC_ALL": "C"}
    subprocess.run(
        [
            str(pg_bin / "initdb"),
            "-D",
            str(data),
            "-U",
            "postgres",
            "-A",
            "trust",
            "-E",
            "UTF8",
            "--locale=C",
        ],
        check=True,
        env=env,
        capture_output=True,
    )
    port = _free_port()
    # No Unix socket: the work directory's path can exceed the socket path limit.
    options = f"-p {port} -c listen_addresses=127.0.0.1 -c unix_socket_directories=''"
    subprocess.run(
        [
            str(pg_bin / "pg_ctl"),
            "-D",
            str(data),
            "-l",
            str(log),
            "-o",
            options,
            "-w",
            "start",
        ],
        check=True,
        env=env,
        capture_output=True,
    )
    try:
        subprocess.run(
            [
                str(pg_bin / "createdb"),
                "-h",
                "127.0.0.1",
                "-p",
                str(port),
                "-U",
                "postgres",
                "tuit",
            ],
            check=True,
            env=env,
            capture_output=True,
        )
        yield port
    finally:
        subprocess.run(
            [str(pg_bin / "pg_ctl"), "-D", str(data), "-m", "fast", "-w", "stop"],
            check=False,
            env=env,
            capture_output=True,
        )


def _psql(port: int, sql: str) -> None:
    subprocess.run(
        [
            str(_pg_bin() / "psql"),
            "-h",
            "127.0.0.1",
            "-p",
            str(port),
            "-U",
            "postgres",
            "-d",
            "tuit",
            "-v",
            "ON_ERROR_STOP=1",
            "-q",
            "-c",
            sql,
        ],
        check=True,
        env={**os.environ, "LC_ALL": "C"},
        capture_output=True,
    )


def _literal(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def _rename_ids(port: int, tasks: Mapping[str, str], queues: Mapping[str, str]) -> None:
    """Give seeded tasks and queues their recorded ids.

    Tuit has no way to choose an id. Foreign keys are suspended for the one
    transaction (``session_replication_role``) so a task and every row naming
    it move together; ids are random eight-letter words, so the JSON
    references are rewritten as text.
    """
    renames = [(new, old) for old, new in tasks.items() if old != new]
    queue_renames = [(new, old) for old, new in queues.items() if old != new]
    if not renames and not queue_renames:
        return

    def values(pairs: list[tuple[str, str]]) -> str:
        return ", ".join(f"({_literal(new)}, {_literal(old)})" for new, old in pairs)

    statements = ["BEGIN", "SET LOCAL session_replication_role = replica"]
    if renames:
        statements += [
            "CREATE TEMP TABLE task_ids (new text PRIMARY KEY, old text) ON COMMIT DROP",
            f"INSERT INTO task_ids VALUES {values(renames)}",
            """DO $$
            DECLARE r record;
            BEGIN
              FOR r IN
                SELECT kcu.table_name, kcu.column_name
                FROM information_schema.referential_constraints rc
                JOIN information_schema.key_column_usage kcu
                  ON kcu.constraint_name = rc.constraint_name
                JOIN information_schema.constraint_column_usage ccu
                  ON ccu.constraint_name = rc.unique_constraint_name
                WHERE ccu.table_name = 'tasks' AND ccu.column_name = 'id'
              LOOP
                EXECUTE format(
                  'UPDATE %I SET %I = m.old FROM task_ids m WHERE %I.%I = m.new',
                  r.table_name, r.column_name, r.table_name, r.column_name);
              END LOOP;
            END $$""",
            "UPDATE tasks SET id = m.old FROM task_ids m WHERE tasks.id = m.new",
        ]
    if queue_renames:
        statements += [
            "CREATE TEMP TABLE queue_ids (new text PRIMARY KEY, old text) ON COMMIT DROP",
            f"INSERT INTO queue_ids VALUES {values(queue_renames)}",
            "UPDATE events SET queue_id = m.old FROM queue_ids m WHERE events.queue_id = m.new",
            "UPDATE queues SET id = m.old FROM queue_ids m WHERE queues.id = m.new",
        ]
    for new, old in renames:
        for table in ("activity", "events"):
            statements.append(
                f"UPDATE {table} SET data = replace(data::text, {_literal(json.dumps(new))}, "
                f"{_literal(json.dumps(old))})::jsonb "
                f"WHERE data::text LIKE {_literal('%' + json.dumps(new) + '%')}"
            )
    statements.append("COMMIT")
    _psql(port, ";\n".join(statements))


class _Seeder:
    """Replays the world through Tuit's REST API, in time order."""

    def __init__(
        self,
        client: httpx.Client,
        mint: Callable[[str, str | None], str],
        moment: datetime,
    ) -> None:
        self.client = client
        self.mint = mint
        self.moment = moment
        self.tokens: dict[Principal, str] = {}
        self.clock: datetime | None = None
        # Recorded id -> seeded id.
        self.task_ids: dict[str, str] = {}
        self.queue_ids: dict[str, str] = {}

    def _headers(self, principal: Principal) -> dict[str, str]:
        if principal not in self.tokens:
            self.tokens[principal] = self.mint(*principal)
        return {"Authorization": f"Bearer {self.tokens[principal]}"}

    def at(self, moment: datetime) -> None:
        """Move Tuit's clock forward to ``moment``; it never moves back."""
        moment = min(moment, self.moment)
        if self.clock is not None and moment <= self.clock:
            return
        _set_clock(self.client, moment)
        self.clock = moment

    def call(
        self, method: str, path: str, principal: Principal, body: object = None
    ) -> dict[str, object]:
        response = self.client.request(
            method, f"/api{path}", json=body, headers=self._headers(principal)
        )
        if response.is_error:
            raise TuitSeedError(
                f"{method} {path} as {principal}: HTTP {response.status_code} {response.text}"
            )
        return cast("dict[str, object]", response.json())

    def current(self, task_id: str, principal: Principal) -> dict[str, object]:
        view = self.call("GET", f"/tasks/{self.task_ids[task_id]}", principal)
        return cast("dict[str, object]", view["task"])


def _principal(author: Mapping[str, str | None] | None, fallback: str) -> Principal:
    user = (author or {}).get("user") or fallback
    return (user, (author or {}).get("agent"))


def _actor(raw: Mapping[str, str] | None) -> dict[str, str]:
    return dict(raw) if raw else {"kind": "anyone"}


def _create_body(
    task: TaskSnapshot, *, last_done: datetime | None
) -> dict[str, object]:
    body: dict[str, object] = {
        "title": task.title,
        "brief": task.brief,
        "next_action": task.next_action,
        "done_means": task.done_means,
        "visibility": task.visibility,
        "next_actor": _actor(task.next_actor),
        "area": task.area or "",
        "requires": task.requires,
        "prefers": task.prefers,
    }
    for name in ("deadline", "expires", "recurrence", "available_rule", "target_rule"):
        if (value := getattr(task, name)) is not None:
            body[name] = value
    if task.available_rule is None and task.available_from is not None:
        body["available_from"] = task.available_from
    if task.target_rule is None and task.target is not None:
        body["target"] = task.target
    if last_done is not None and task.recurrence is not None:
        body["last_done"] = {"at": _iso(last_done)}
    return body


def _snapshot_at(task: WorldTask, recorded_at: datetime) -> TaskSnapshot | None:
    """The observation a write produced: Tuit stamps updated_at with its time."""
    return next(
        (
            o.task
            for o in task.observations
            if not o.stub and o.task.updated_at == recorded_at
        ),
        None,
    )


def _waiting_body(
    waiting: Mapping[str, object], task_ids: Mapping[str, str]
) -> dict[str, object]:
    body: dict[str, object] = {"kind": waiting.get("kind", "reply")}
    if waiting.get("for"):
        body["for"] = waiting["for"]
    if waiting.get("follow_up"):
        body["follow_up"] = waiting["follow_up"]
    if body["kind"] == "task":
        dependency = task_ids.get(str(waiting.get("task_id")))
        if dependency is None:
            # The task it waits on is not in this world: wait on a reply instead.
            body["kind"] = "reply"
            body.setdefault("for", "another task")
        else:
            body["task_id"] = dependency
    return body


@dataclass(order=True)
class _Op:
    at: datetime
    order: int
    run: Callable[[], None] = field(compare=False)


def _plan_task(
    seeder: _Seeder, task: WorldTask, default_user: str, ops: list[_Op]
) -> None:
    moment = seeder.moment
    target = task.as_of(moment)
    if target is None:
        return
    owner = target.owner or (target.created_by or {}).get("user") or default_user
    created_at = target.created_at or min(o.seen_at for o in task.observations)
    created_at = min(created_at, moment)
    creator = _principal(target.created_by, owner)
    first = next(
        (
            o.task
            for o in sorted(task.observations, key=lambda o: o.seen_at)
            if not o.stub and o.seen_at <= moment
        ),
        target,
    )
    activity = [
        a
        for a in task.activity_as_of(moment)
        if a.kind in _NOTE_KINDS or a.kind in {"completion", "skip"}
    ]
    completions = any(a.kind == "completion" for a in activity)
    last_done = None if completions else target.last_done_at
    actor_now: dict[str, dict[str, str]] = {}

    def create() -> None:
        seeder.at(created_at)
        view = seeder.call(
            "POST", "/tasks", creator, _create_body(first, last_done=last_done)
        )
        seeded = cast("dict[str, object]", view["task"])
        seeder.task_ids[task.id] = str(seeded["id"])
        actor_now[task.id] = cast("dict[str, str]", seeded["next_actor"])

    ops.append(_Op(created_at, 0, create))
    for entry in activity:
        ops.append(
            _Op(
                entry.recorded_at,
                1,
                lambda entry=entry: _replay(seeder, task, entry, owner, actor_now),
            )
        )
    for attachment in target.attachments:
        added_at = attachment.get("added_at")
        when = (
            datetime.fromisoformat(added_at)
            if isinstance(added_at, str)
            else created_at
        )
        if when > moment:
            continue
        added_by = attachment.get("added_by")
        principal = _principal(
            cast("Mapping[str, str | None]", added_by)
            if isinstance(added_by, dict)
            else None,
            owner,
        )
        body: dict[str, object] = {
            key: attachment[key]
            for key in ("url", "title", "mime_type")
            if attachment.get(key)
        }

        def attach(
            principal: Principal = principal, body: dict[str, object] = body
        ) -> None:
            seeder.call(
                "POST",
                f"/tasks/{seeder.task_ids[task.id]}/attachments",
                principal,
                body,
            )

        ops.append(_Op(max(when, created_at), 2, attach))
    settle_at = max(
        [target.updated_at or created_at, target.closed_at or created_at, created_at]
        + [a.recorded_at for a in activity]
    )
    ops.append(
        _Op(
            min(settle_at, moment),
            3,
            lambda: _settle(seeder, task, target, owner, creator, moment),
        )
    )


def _replay(
    seeder: _Seeder,
    task: WorldTask,
    entry: ActivityEntry,
    owner: str,
    actor_now: dict[str, dict[str, str]],
) -> None:
    seeder.at(entry.recorded_at)
    seeded = seeder.task_ids[task.id]
    principal = _principal(entry.author, owner)
    if entry.kind == "completion":
        current = seeder.current(task.id, principal)
        if current.get("state") == "done" or (
            current.get("recurrence") and current.get("state") in _CLOSED
        ):
            return
        body: dict[str, object] = {
            "at": {"at": _iso(min(entry.happened_at, entry.recorded_at))}
        }
        if entry.body:
            body["note"] = entry.body
        seeder.call("POST", f"/tasks/{seeded}/complete", principal, body)
        return
    if entry.kind == "skip":
        seeder.call(
            "POST",
            f"/tasks/{seeded}/skip",
            principal,
            {"note": entry.body} if entry.body else {},
        )
        return
    after = _snapshot_at(task, entry.recorded_at)
    plain: dict[str, object] = {
        "note": entry.body or "(no note)",
        "kind": entry.kind,
        "next_actor": actor_now.get(task.id, {"kind": "anyone"}),
    }
    full = dict(plain)
    handed = entry.data.get("handed_to")
    if after is not None:
        full |= {
            "brief": after.brief,
            "next_action": after.next_action,
            "next_actor": _actor(after.next_actor),
        }
        if after.state == "waiting" and after.waiting:
            full |= {
                "state": "waiting",
                "waiting": _waiting_body(after.waiting, seeder.task_ids),
            }
        elif after.state == "open":
            full["state"] = "open"
    else:
        if isinstance(handed, dict):
            full["next_actor"] = handed
        if isinstance(entry.data.get("next_action"), str):
            full["next_action"] = entry.data["next_action"]
        state = entry.data.get("state")
        waiting = entry.data.get("waiting")
        if (
            isinstance(state, dict)
            and state.get("to") == "waiting"
            and isinstance(waiting, dict)
        ):
            full |= {
                "state": "waiting",
                "waiting": _waiting_body(waiting, seeder.task_ids),
            }
        elif isinstance(state, dict) and state.get("to") == "open":
            full["state"] = "open"
    try:
        view = seeder.call("POST", f"/tasks/{seeded}/checkpoint", principal, full)
    except TuitSeedError as error:
        # A step Tuit's rules refuse (e.g. a since-tightened waiting rule):
        # keep the note; the task's recorded state is reached when it settles.
        logger.warning(
            "Replaying a note on %s without its state change: %s", task.id, error
        )
        view = seeder.call("POST", f"/tasks/{seeded}/checkpoint", principal, plain)
    actor_now[task.id] = cast(
        "dict[str, str]", cast("dict[str, object]", view["task"])["next_actor"]
    )


def _settle(
    seeder: _Seeder,
    task: WorldTask,
    target: TaskSnapshot,
    owner: str,
    creator: Principal,
    moment: datetime,
) -> None:
    """Bring a seeded task to its recorded state as of the moment."""
    seeded = seeder.task_ids[task.id]
    person: Principal = (owner, None)
    current = seeder.current(task.id, person)
    state = current.get("state")
    if target.state == "open" and state != "open":
        seeder.call("POST", f"/tasks/{seeded}/reopen", person, {})
    elif target.state == "waiting":
        waiting = _waiting_body(target.waiting or {"kind": "reply"}, seeder.task_ids)
        have = cast("Mapping[str, object]", current.get("waiting") or {})
        if (
            state != "waiting"
            or _waiting_body(have, {str(have.get("task_id")): str(have.get("task_id"))})
            != waiting
        ):
            seeder.call(
                "POST",
                f"/tasks/{seeded}/checkpoint",
                person,
                {
                    "note": f"Waiting for {waiting.get('for') or 'a reply'}",
                    "next_actor": _actor(target.next_actor),
                    "state": "waiting",
                    "waiting": waiting,
                },
            )
    elif target.state == "done" and state != "done" and not target.recurrence:
        done_at = min(
            target.closed_at or seeder.clock or moment, seeder.clock or moment
        )
        seeder.call(
            "POST", f"/tasks/{seeded}/complete", person, {"at": {"at": _iso(done_at)}}
        )
    elif target.state in {"expired", "shelved"} and state != target.state:
        seeder.call(
            "POST",
            f"/tasks/{seeded}/close",
            creator,
            {"state": target.state, "reason": target.close_reason}
            if target.close_reason
            else {"state": target.state},
        )
    current = seeder.current(task.id, person)
    wanted = _create_body(target, last_done=None)
    wanted.pop("last_done", None)
    changes: dict[str, object] = {}
    for name in (*_FIELDS, "available_from", "target"):
        value = wanted.get(name)
        have = current.get(name)
        if name == "area":
            value, have = value or None, have or None
        if name in {"requires", "prefers"}:
            value, have = value or [], have or []
        if value != have and not (value is None and have is None):
            derived = {
                "available_from": target.available_rule,
                "target": target.target_rule,
            }
            if name in derived and derived[name] is not None:
                continue
            changes[name] = value
    if _actor(target.next_actor) != current.get("next_actor"):
        changes["next_actor"] = _actor(target.next_actor)
    if changes:
        seeder.call(
            "PATCH",
            f"/tasks/{seeded}",
            creator if creator[0] == owner else person,
            changes,
        )
    for user in sorted(task.pinned_as_of(moment)):
        seeder.call("POST", f"/tasks/{seeded}/pin", (user, None), {"pinned": True})


def _plan_queues(seeder: _Seeder, world: TuitWorld, ops: list[_Op]) -> None:
    for queue in world.queues:
        snapshot = queue.as_of(seeder.moment)
        if snapshot is None:
            continue
        when = min(snapshot.created_at or seeder.moment, seeder.moment)
        principal: Principal = (
            snapshot.owner or world.users[0],
            snapshot.created_by_agent,
        )
        body = {
            "name": snapshot.name,
            "visibility": snapshot.visibility,
            "enabled": snapshot.enabled,
            "config": snapshot.config,
        }

        def save(
            queue_id: str = queue.id,
            principal: Principal = principal,
            body: dict[str, object] = body,
        ) -> None:
            created = seeder.call("POST", "/queues", principal, body)
            seeder.queue_ids[queue_id] = str(created["id"])

        # After the tasks of the same instant, so a preview has them.
        ops.append(_Op(when, 4, save))


def _seed(seeder: _Seeder, world: TuitWorld) -> None:
    ops: list[_Op] = []
    for task in world.tasks:
        _plan_task(seeder, task, world.users[0], ops)
    _plan_queues(seeder, world, ops)
    for op in sorted(ops):
        seeder.at(op.at)
        op.run()


def _wait_for_health(
    client: httpx.Client, process: subprocess.Popen[bytes], deadline: float, log: Path
) -> None:
    while wall.monotonic() < deadline:
        if process.poll() is not None:
            raise RuntimeError(f"Tuit exited during startup; see {log}")
        with contextlib.suppress(httpx.HTTPError):
            if client.get("/healthz").status_code == 200:
                return
        wall.sleep(0.2)
    raise RuntimeError(f"Tuit did not become healthy in time; see {log}")


def _tick(client: httpx.Client, moment: datetime, stop: threading.Event) -> None:
    """Keep Tuit's held clock moving with wall time from ``moment``."""
    started = wall.monotonic()
    while not stop.wait(1.0):
        with contextlib.suppress(httpx.HTTPError):
            _set_clock(client, moment + timedelta(seconds=wall.monotonic() - started))


@contextlib.contextmanager
def tuit_server(
    world: TuitWorld,
    moment: datetime,
    work_dir: Path,
    *,
    tuit_dir: Path | None = None,
    fa_users: Mapping[str, str] | None = None,
    household: tuple[str, ...] = (),
    tick: bool = True,
) -> Iterator[TuitServer]:
    """Run Tuit seeded with ``world`` as of ``moment`` for the block.

    ``work_dir`` must be on a local filesystem: PostgreSQL refuses shared
    mounts. ``tuit_dir`` (or ``$FA_EVAL_TUIT_DIR``) is a Tuit checkout with
    its npm dependencies installed. ``fa_users`` maps the assistant's user
    ids to Tuit user ids, as the deployment's ``user_auth`` does; by default
    each Tuit user is its own assistant user. ``household`` names members the
    world never saw, so they exist as people tasks can be handed to. With ``tick``, Tuit's clock runs
    on from ``moment`` as the assistant's does; otherwise it stays put.
    """
    source = _tuit_dir(tuit_dir)
    node = shutil.which("node")
    if node is None:
        raise RuntimeError("Tuit needs Node.js 24 or later on PATH")
    users = sorted(set(household) | set(world.users) | set((fa_users or {}).values()))
    mapping = dict(fa_users) if fa_users is not None else {u: u for u in users}
    work_dir.mkdir(parents=True, exist_ok=True)
    with _postgres(work_dir / "tuit-pg", work_dir / "tuit-pg.log") as pg_port:
        port = _free_port()
        base_url = f"http://127.0.0.1:{port}"
        env = {
            **os.environ,
            "DATABASE_URL": f"postgres://postgres@127.0.0.1:{pg_port}/tuit",
            "PORT": str(port),
            "PUBLIC_URL": base_url,
            "TUIT_USERS": ",".join(
                f"{u}:{u}@household.invalid:{u.capitalize()}" for u in users
            ),
            # No identity provider: pick-a-user login, loopback only.
            "TUIT_DEV_LOGIN": "1",
            "TUIT_TEST_CLOCK": "1",
        }

        def mint(user: str, agent: str | None) -> str:
            role = ["--agent", agent] if agent else ["--personal"]
            result = subprocess.run(
                [
                    node,
                    "src/admin.ts",
                    "mint-token",
                    "--user",
                    user,
                    *role,
                    "--label",
                    "eval",
                ],
                cwd=source,
                env=env,
                check=True,
                capture_output=True,
                text=True,
            )
            return result.stdout.strip().splitlines()[-1]

        log = work_dir / "tuit.log"
        with log.open("wb") as log_file:
            process = subprocess.Popen(
                [node, "src/server.ts"],
                cwd=source,
                env=env,
                stdout=log_file,
                stderr=subprocess.STDOUT,
            )
        client = httpx.Client(base_url=base_url, timeout=30)
        stop = threading.Event()
        ticker: threading.Thread | None = None
        try:
            _wait_for_health(client, process, wall.monotonic() + 60, log)
            seeder = _Seeder(client, mint, moment)
            _seed(seeder, world)
            _rename_ids(pg_port, seeder.task_ids, seeder.queue_ids)
            _set_clock(client, moment)
            tokens = {
                user: mint(user, FA_AGENT) for user in sorted(set(mapping.values()))
            }
            environment = {
                f"TUIT_{user.upper()}_TOKEN": token for user, token in tokens.items()
            }
            server_config = MCPServerConfig.model_validate({
                "transport": "streamable_http",
                "url": f"{base_url}/mcp",
                "tool_name_prefix": "tuit_",
                "user_auth": {
                    "users": {
                        fa_user: {
                            "token_env": f"TUIT_{tuit_user.upper()}_TOKEN",
                            "expected_user_id": tuit_user,
                        }
                        for fa_user, tuit_user in mapping.items()
                    },
                    "identity_check": {
                        "path": "/api/me",
                        "expected_agent": FA_AGENT,
                        "require_write": True,
                    },
                },
            })
            if tick:
                ticker = threading.Thread(
                    target=_tick, args=(client, moment, stop), daemon=True
                )
                ticker.start()
            yield TuitServer(
                base_url=base_url,
                mcp_url=f"{base_url}/mcp",
                tokens=tokens,
                server_config=server_config,
                environment=environment,
                _client=client,
            )
        finally:
            stop.set()
            if ticker is not None:
                ticker.join(timeout=5)
            client.close()
            process.terminate()
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()
