"""The household's Tuit task tracker, rebuilt from recorded tool results.

The snapshot holds no Tuit data, but the deployment reaches Tuit as an MCP
server whose tools (``tuit_now``, ``tuit_find_tasks``, ``tuit_get_task``,
``tuit_checkpoint`` ...) answer with Tuit's own JSON: whole tasks, their
activity history, queues and change-feed events. Pooled across the history
they give most of the tracker as it stood -- every task a turn saw, how it
looked each time, the notes agents and people left on it, and who pinned it.

Each task keeps its observations over time, so a case sees it as it stood
when the case was asked: the latest observation at or before that moment,
else the earliest after it, with a task created later absent. A note a
recorded turn wrote through ``checkpoint`` but never read back is kept from
the call's arguments.

What is filled in rather than recorded: a task seen only in the change feed
is a stub (title, state, visibility, creator); a task first observed after
the moment, in a state it reached after the moment, is shown as it would have
been before -- open, not yet done or waiting. Nothing else is invented.

The household's earlier tracker (Vikunja, behind ``find_tasks`` and
``list_projects``) is left out: the deployment no longer has those tools,
and what they returned was mostly mirrored Alertmanager alerts, not the
household's tasks.
"""

from __future__ import annotations

import json
from datetime import datetime
from typing import TYPE_CHECKING, Literal

from pydantic import BaseModel, ConfigDict, JsonValue

if TYPE_CHECKING:
    from collections.abc import Iterable, Mapping

    from family_assistant.storage.types import MessageHistoryRow

__all__ = [
    "ActivityEntry",
    "PinObservation",
    "QueueObservation",
    "QueueSnapshot",
    "TaskObservation",
    "TaskSnapshot",
    "TuitWorld",
    "WorldQueue",
    "WorldTask",
    "build_tuit_world",
]

TOOL_PREFIX = "tuit_"
FA_AGENT = "family-assistant"

TaskState = Literal["open", "waiting", "done", "expired", "shelved"]
_CLOSED: frozenset[str] = frozenset({"done", "expired", "shelved"})


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid")


class TaskSnapshot(BaseModel):
    """A task in Tuit's wire format; omitted fields are empty, as Tuit prunes them."""

    # Tuit may grow fields; an unknown one is dropped rather than failing the build.
    model_config = ConfigDict(extra="ignore")

    id: str
    title: str
    seq: int | None = None
    brief: str = ""
    next_action: str = ""
    done_means: str = ""
    owner: str | None = None
    visibility: Literal["household", "private"] = "household"
    next_actor: dict[str, str] | None = None
    actor_since: datetime | None = None
    state: TaskState = "open"
    close_reason: str = ""
    closed_at: datetime | None = None
    waiting: dict[str, JsonValue] | None = None
    available_from: dict[str, str] | None = None
    available_rule: dict[str, JsonValue] | None = None
    target: dict[str, str] | None = None
    target_rule: dict[str, JsonValue] | None = None
    deadline: dict[str, str] | None = None
    expires: dict[str, str] | None = None
    area: str | None = None
    requires: list[str] = []
    prefers: list[str] = []
    recurrence: dict[str, JsonValue] | None = None
    attachments: list[dict[str, JsonValue]] = []
    last_done_at: datetime | None = None
    last_skip_at: datetime | None = None
    revision: int | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None
    created_by: dict[str, str | None] | None = None


class TaskObservation(_Model):
    seen_at: datetime
    task: TaskSnapshot
    # Seen only as a change-feed event: title, state and visibility are real,
    # the rest is empty.
    stub: bool = False


class ActivityEntry(_Model):
    """One entry of a task's history, as ``get_task`` returns it."""

    # Tuit's entry id; None for a note known only from a recorded call.
    id: int | None = None
    kind: str
    body: str = ""
    data: dict[str, JsonValue] = {}
    happened_at: datetime
    recorded_at: datetime
    author: dict[str, str | None] = {}


class PinObservation(_Model):
    seen_at: datetime
    user: str
    pinned: bool


class WorldTask(_Model):
    id: str
    observations: list[TaskObservation]
    activity: list[ActivityEntry] = []
    pins: list[PinObservation] = []

    @property
    def created_at(self) -> datetime | None:
        return min(
            (o.task.created_at for o in self.observations if o.task.created_at),
            default=None,
        )

    def as_of(self, moment: datetime) -> TaskSnapshot | None:
        """The task as it stood at ``moment``, or None if it did not exist yet."""
        created = self.created_at
        if created is not None and created > moment:
            return None
        ordered = sorted(self.observations, key=lambda o: (o.stub, o.seen_at))
        before = [o for o in ordered if o.seen_at <= moment]
        if before:
            full = [o for o in before if not o.stub]
            return (full or before)[-1].task
        return _before_later_changes(ordered[0].task, moment)

    def activity_as_of(self, moment: datetime) -> list[ActivityEntry]:
        return sorted(
            (a for a in self.activity if a.recorded_at <= moment),
            key=lambda a: (a.recorded_at, a.id or 0),
        )

    def pinned_as_of(self, moment: datetime) -> set[str]:
        """The users who had the task pinned at ``moment``, as last observed."""
        latest: dict[str, PinObservation] = {}
        for pin in sorted(self.pins, key=lambda p: p.seen_at):
            if pin.seen_at <= moment or pin.user not in latest:
                latest[pin.user] = pin
        return {user for user, pin in latest.items() if pin.pinned}


def _before_later_changes(task: TaskSnapshot, moment: datetime) -> TaskSnapshot:
    """A task first seen after ``moment``, without a state it reached after it."""
    update: dict[str, object] = {}
    if task.state in _CLOSED and task.closed_at is not None and task.closed_at > moment:
        update |= {"state": "open", "closed_at": None, "close_reason": ""}
    elif task.state == "waiting" and task.waiting is not None:
        since = task.waiting.get("since")
        if isinstance(since, str) and datetime.fromisoformat(since) > moment:
            update |= {"state": "open", "waiting": None}
    attachments = [
        a
        for a in task.attachments
        if not isinstance(a.get("added_at"), str)
        or datetime.fromisoformat(str(a["added_at"])) <= moment
    ]
    if len(attachments) != len(task.attachments):
        update["attachments"] = attachments
    return task.model_copy(update=update) if update else task


class QueueSnapshot(BaseModel):
    model_config = ConfigDict(extra="ignore")

    id: str
    name: str
    owner: str | None = None
    visibility: Literal["household", "private"] = "household"
    enabled: bool = True
    config: dict[str, JsonValue] = {}
    revision: int | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None
    created_by_agent: str | None = None


class QueueObservation(_Model):
    seen_at: datetime
    queue: QueueSnapshot


class WorldQueue(_Model):
    id: str
    observations: list[QueueObservation]

    def as_of(self, moment: datetime) -> QueueSnapshot | None:
        ordered = sorted(self.observations, key=lambda o: o.seen_at)
        first = ordered[0].queue
        if first.created_at is not None and first.created_at > moment:
            return None
        before = [o for o in ordered if o.seen_at <= moment]
        return (before or ordered)[-1 if before else 0].queue


class TuitWorld(_Model):
    users: list[str]
    tasks: list[WorldTask]
    queues: list[WorldQueue] = []

    def task(self, task_id: str) -> WorldTask | None:
        return next((t for t in self.tasks if t.id == task_id), None)

    def areas_as_of(self, moment: datetime) -> list[str]:
        return sorted({
            snapshot.area
            for task in self.tasks
            if (snapshot := task.as_of(moment)) is not None and snapshot.area
        })


def _arguments(raw: object) -> dict[str, object]:
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, str):
        try:
            decoded = json.loads(raw)
        except json.JSONDecodeError:
            return {}
        return decoded if isinstance(decoded, dict) else {}
    return {}


def _payload(content: str) -> dict[str, object] | None:
    """A successful Tuit answer; None for an error or anything that is not one."""
    try:
        decoded = json.loads(content)
    except json.JSONDecodeError:
        return None
    if not isinstance(decoded, dict) or "error" in decoded:
        return None
    return decoded


def _parse_time(value: object) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


class _Builder:
    def __init__(self, user_ids: Mapping[str, str]) -> None:
        self.user_ids = user_ids
        self.tasks: dict[str, WorldTask] = {}
        self.queues: dict[str, WorldQueue] = {}
        self.users: set[str] = set()

    def _world_task(self, task_id: str) -> WorldTask:
        if task_id not in self.tasks:
            self.tasks[task_id] = WorldTask(id=task_id, observations=[])
        return self.tasks[task_id]

    def add_task(
        self, raw: object, seen: datetime, *, stub: bool = False
    ) -> TaskSnapshot | None:
        if not isinstance(raw, dict) or "id" not in raw or "title" not in raw:
            return None
        snapshot = TaskSnapshot.model_validate(raw)
        self._world_task(snapshot.id).observations.append(
            TaskObservation(seen_at=seen, task=snapshot, stub=stub)
        )
        self.users.update(u for u in _users_of(snapshot) if u)
        return snapshot

    def add_pin(
        self, task_id: str, user: str | None, pinned: object, seen: datetime
    ) -> None:
        if user is None or not isinstance(pinned, bool):
            return
        self._world_task(task_id).pins.append(
            PinObservation(seen_at=seen, user=user, pinned=pinned)
        )

    def add_activity(self, task_id: str, raw: object) -> None:
        if not isinstance(raw, dict):
            return
        entry = ActivityEntry.model_validate(raw)
        task = self._world_task(task_id)
        if entry.id is not None and any(a.id == entry.id for a in task.activity):
            return
        task.activity.append(entry)
        if user := entry.author.get("user"):
            self.users.add(user)

    def add_written_note(
        self,
        task: TaskSnapshot,
        kind: str,
        body: str,
        user: str | None,
        *,
        happened_at: datetime | None = None,
        data: dict[str, JsonValue] | None = None,
    ) -> None:
        """A note a recorded call wrote; Tuit stamped it with the task's updated_at."""
        if task.updated_at is None:
            return
        self._world_task(task.id).activity.append(
            ActivityEntry(
                kind=kind,
                body=body,
                data=data or {},
                happened_at=happened_at or task.updated_at,
                recorded_at=task.updated_at,
                author={"user": user or task.owner, "agent": FA_AGENT},
            )
        )

    def add_queue(self, raw: object, seen: datetime) -> None:
        if not isinstance(raw, dict) or "id" not in raw or "name" not in raw:
            return
        queue = QueueSnapshot.model_validate(raw)
        if queue.id not in self.queues:
            self.queues[queue.id] = WorldQueue(id=queue.id, observations=[])
        self.queues[queue.id].observations.append(
            QueueObservation(seen_at=seen, queue=queue)
        )

    def add_result(
        self,
        name: str,
        arguments: Mapping[str, object],
        payload: Mapping[str, object],
        seen: datetime,
        acting_user: str | None,
    ) -> None:
        tool = name.removeprefix(TOOL_PREFIX)
        if tool in {"now", "preview_queue"}:
            for key in ("plan", "new_items", "also", "urgent", "items"):
                for raw in _list(payload.get(key)):
                    self._add_item(raw, seen, acting_user)
        elif tool == "find_tasks":
            for raw in _list(payload.get("tasks")):
                self.add_task(raw, seen)
        elif tool == "run_queue":
            self.add_queue(payload.get("queue"), seen)
            result = payload.get("result")
            if isinstance(result, dict):
                for key in ("items", "urgent"):
                    for raw in _list(result.get(key)):
                        self._add_item(raw, seen, acting_user)
        elif tool == "list_queues":
            for raw in _list(payload.get("queues")):
                self.add_queue(raw, seen)
        elif tool == "save_queue":
            self.add_queue(payload, seen)
        elif tool == "get_changes":
            self._add_changes(payload, seen)
        elif "task" in payload:
            self._add_view(tool, arguments, payload, seen, acting_user)

    def _add_item(self, raw: object, seen: datetime, acting_user: str | None) -> None:
        if not isinstance(raw, dict):
            return
        item = raw.get("item", raw)
        if not isinstance(item, dict):
            return
        snapshot = self.add_task(item.get("task"), seen)
        if snapshot is not None:
            self.add_pin(snapshot.id, acting_user, item.get("pinned"), seen)

    def _add_view(
        self,
        tool: str,
        arguments: Mapping[str, object],
        payload: Mapping[str, object],
        seen: datetime,
        acting_user: str | None,
    ) -> None:
        snapshot = self.add_task(payload.get("task"), seen)
        if snapshot is None:
            return
        status = payload.get("status")
        if isinstance(status, dict):
            self.add_pin(snapshot.id, acting_user, status.get("pinned"), seen)
        for raw in _list(payload.get("activity")):
            self.add_activity(snapshot.id, raw)
        if tool in {"checkpoint", "hand_off"}:
            note = arguments.get("note")
            kind = arguments.get("kind")
            self.add_written_note(
                snapshot,
                kind if isinstance(kind, str) else "note",
                note if isinstance(note, str) else "Handed off",
                acting_user,
            )
        elif tool == "complete_task":
            note = arguments.get("note")
            done = snapshot.last_done_at if snapshot.recurrence else snapshot.closed_at
            self.add_written_note(
                snapshot,
                "completion",
                note if isinstance(note, str) else "",
                acting_user,
                happened_at=done,
            )
        elif tool == "skip_routine":
            note = arguments.get("note")
            self.add_written_note(
                snapshot, "skip", note if isinstance(note, str) else "", acting_user
            )

    def _add_changes(self, payload: Mapping[str, object], seen: datetime) -> None:
        for event in _list(payload.get("events")):
            if not isinstance(event, dict):
                continue
            task = event.get("task")
            at = _parse_time(event.get("at"))
            if not isinstance(task, dict) or at is None:
                continue
            raw: dict[str, object] = {**task}
            if event.get("type") == "created":
                actor = event.get("actor")
                raw["created_at"] = event["at"]
                if isinstance(actor, dict):
                    raw["created_by"] = actor
                    raw["owner"] = actor.get("user")
            self.add_task(raw, at, stub=True)

    def finish(self) -> TuitWorld:
        for task in self.tasks.values():
            task.activity = _without_echoes(task.activity)
            _complete_stubs(task)
        tasks = [t for t in self.tasks.values() if t.observations]
        return TuitWorld(
            users=sorted(self.users | set(self.user_ids.values())),
            tasks=sorted(
                tasks, key=lambda t: (t.created_at is None, str(t.created_at), t.id)
            ),
            queues=sorted(self.queues.values(), key=lambda q: q.id),
        )


def _complete_stubs(task: WorldTask) -> None:
    """Give a feed-only task its creator and creation time from its created event."""
    created = next(
        (o.task for o in task.observations if o.stub and o.task.created_at), None
    )
    if created is None:
        return
    for observation in task.observations:
        if observation.stub and observation.task.created_at is None:
            observation.task = observation.task.model_copy(
                update={
                    "created_at": created.created_at,
                    "created_by": created.created_by,
                    "owner": created.owner,
                }
            )


def _without_echoes(entries: list[ActivityEntry]) -> list[ActivityEntry]:
    """Drop a note kept from a call's arguments once ``get_task`` showed it."""
    recorded = {(a.recorded_at, a.body) for a in entries if a.id is not None}
    return [
        a
        for a in entries
        if a.id is not None or (a.recorded_at, a.body) not in recorded
    ]


def _users_of(task: TaskSnapshot) -> list[str | None]:
    actor = (task.next_actor or {}).get("user")
    creator = (task.created_by or {}).get("user")
    return [task.owner, actor, creator]


def _list(value: object) -> list[object]:
    return value if isinstance(value, list) else []


def build_tuit_world(
    grouped: Mapping[tuple[str, str], Iterable[MessageHistoryRow]],
    *,
    user_ids: Mapping[str, str] | None = None,
) -> TuitWorld:
    """Pool every recorded Tuit tool result into one world.

    ``user_ids`` maps the assistant's user ids to Tuit's (the deployment's
    ``user_auth`` mapping); a turn's reads tell whose pins they show, and its
    writes whose notes they are. Without a mapping, a write is credited to
    the task's owner.
    """
    mapping = dict(user_ids or {})
    builder = _Builder(mapping)
    results: list[
        tuple[datetime, str, dict[str, object], dict[str, object], str | None]
    ] = []
    for rows in grouped.values():
        calls: dict[str, tuple[str, dict[str, object]]] = {}
        for row in rows:
            if row["role"] == "assistant":
                for item in row["tool_calls"] or []:
                    calls[item.id] = (
                        item.function.name,
                        _arguments(item.function.arguments),
                    )
                continue
            if row["role"] != "tool" or not row["tool_call_id"] or not row["content"]:
                continue
            name, arguments = calls.get(
                row["tool_call_id"], (row["tool_name"] or "", {})
            )
            if not name.startswith(TOOL_PREFIX):
                continue
            payload = _payload(row["content"])
            if payload is None:
                continue
            user = mapping.get(row["user_id"] or "")
            results.append((row["timestamp"], name, arguments, payload, user))
    for seen, name, arguments, payload, user in sorted(results, key=lambda r: r[0]):
        builder.add_result(name, arguments, payload, seen, user)
    return builder.finish()
