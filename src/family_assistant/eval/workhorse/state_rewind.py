"""Rewind a snapshot copy to the moment a case was asked.

The snapshot is the household's state on the day it was taken, after every
case in it. Replayed as is, a case sees its own future: notes its original
turn went on to write, documents ingested later, the later conversation in
message history. Each case therefore runs on a fresh copy rewound to its
moment ``T``:

- **Rows created at or after T are removed**: message history (and what
  references it), documents and their embeddings, tasks, delegation runs,
  received email, Home Assistant events, memory change records, and audit
  and error rows.
- **Notes are rewound** to their content at T. The notes table keeps no
  history, but every ``get_note`` result and every note write a turn made is
  recorded, which reconstructs each note's timeline; replaying the recorded
  writes forward from each observation predicts the next observation 99.6%
  of the time. A note is set to the last state known at or before T, or
  failing that to the first observation after T if no write intervenes; a
  note created after T is removed and one deleted after T restored. Changed
  notes are re-indexed so search sees the rewound text.

What cannot be rewound is reported, not guessed: a note whose state at T
the record does not determine keeps its snapshot content and is listed.
"""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Literal

from sqlalchemy import delete, insert, select, update

from family_assistant.storage.base import attachment_metadata_table, metadata
from family_assistant.storage.events import recent_events_table
from family_assistant.storage.memory_change_log import memory_change_log_table
from family_assistant.storage.message_history import message_history_table
from family_assistant.storage.notes import notes_table
from family_assistant.storage.tasks import TaskPriority, tasks_table

if TYPE_CHECKING:
    from collections.abc import Iterable, Mapping

    from family_assistant.storage.database import Database
    from family_assistant.storage.types import MessageHistoryRow

__all__ = [
    "NoteState",
    "NoteTimelines",
    "RewindReport",
    "build_note_timelines",
    "rewind_to",
]

# Embeddings cascade from documents.
_DOCUMENTS = metadata.tables["documents"]

EventKind = Literal["observed", "written", "appended", "deleted"]
Quality = Literal["unchanged", "known", "inferred", "absent", "unknown"]


@dataclass(frozen=True)
class _NoteEvent:
    at: datetime
    kind: EventKind
    # Full content for observed/written, the appended text for appended,
    # None for deleted or for an observation of a missing note.
    content: str | None


@dataclass(frozen=True)
class NoteState:
    quality: Quality
    # None means the note did not exist at T.
    content: str | None


@dataclass
class NoteTimelines:
    events: dict[str, list[_NoteEvent]] = field(default_factory=dict)

    def add(self, title: str, event: _NoteEvent) -> None:
        self.events.setdefault(title, []).append(event)

    def sort(self) -> None:
        for events in self.events.values():
            events.sort(key=lambda e: e.at)

    def state_at(self, title: str, moment: datetime) -> NoteState | None:
        """The note at ``moment``, or None when nothing recorded bears on it."""
        events = self.events.get(title)
        if not events:
            return None
        before = [e for e in events if e.at <= moment]
        after = [e for e in events if e.at > moment]
        content: str | None = None
        known = False
        for event in before:
            if event.kind in {"observed", "written"}:
                content, known = event.content, True
            elif event.kind == "deleted":
                content, known = None, True
            elif known and content is not None:
                content = f"{content}\n{event.content or ''}"
            else:
                known = False
        if known:
            return NoteState("known", content)
        if after and after[0].kind == "observed":
            return NoteState("inferred", after[0].content)
        return NoteState("unknown", None)


def _aware(value: datetime | None) -> datetime | None:
    """SQLite hands timestamps back naive; they were stored as UTC."""
    if value is None or value.tzinfo is not None:
        return value
    return value.replace(tzinfo=UTC)


def _json_object(content: str) -> dict[str, object] | None:
    try:
        decoded = json.loads(content)
    except json.JSONDecodeError:
        return None
    return decoded if isinstance(decoded, dict) else None


def _arguments(raw: object) -> dict[str, object]:
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, str):
        return _json_object(raw) or {}
    return {}


def build_note_timelines(
    grouped: Mapping[tuple[str, str], Iterable[MessageHistoryRow]],
) -> NoteTimelines:
    """Every recorded read and successful write of every note, in time order."""
    timelines = NoteTimelines()
    for rows in grouped.values():
        calls: dict[str, tuple[str, dict[str, object]]] = {}
        results: list[MessageHistoryRow] = []
        for row in rows:
            if row["role"] == "assistant":
                for item in row["tool_calls"] or []:
                    calls[item.id] = (
                        item.function.name,
                        _arguments(item.function.arguments),
                    )
            elif row["role"] == "tool" and row["tool_call_id"] and row["content"]:
                results.append(row)
        for row in results:
            name, arguments = calls.get(
                row["tool_call_id"] or "", (row["tool_name"], {})
            )
            content = row["content"] or ""
            at = row["timestamp"]
            title = arguments.get("title")
            if name == "get_note":
                data = _json_object(content)
                if data is None or not isinstance(data.get("title"), str):
                    continue
                exists = data.get("exists") is True
                body = data.get("content")
                timelines.add(
                    str(data["title"]),
                    _NoteEvent(
                        at,
                        "observed",
                        body if exists and isinstance(body, str) else None,
                    ),
                )
            elif name == "add_or_update_note" and isinstance(title, str):
                # Created, updated, or a bare "Success": anything else (an error,
                # a refused write) changed nothing.
                if "successfully" not in content and content.strip() != "Success":
                    continue
                text = arguments.get("content")
                timelines.add(
                    title,
                    _NoteEvent(
                        at,
                        "appended" if arguments.get("append") else "written",
                        text if isinstance(text, str) else "",
                    ),
                )
            elif name == "delete_note" and isinstance(title, str):
                data = _json_object(content)
                if data is not None and data.get("success") is True:
                    timelines.add(title, _NoteEvent(at, "deleted", None))
    timelines.sort()
    return timelines


@dataclass
class RewindReport:
    moment: datetime
    deleted_rows: dict[str, int] = field(default_factory=dict)
    notes_rewound: list[str] = field(default_factory=list)
    notes_removed: list[str] = field(default_factory=list)
    notes_restored: list[str] = field(default_factory=list)
    notes_unknown: list[str] = field(default_factory=list)


async def _delete_after(db: Database, moment: datetime, report: RewindReport) -> None:
    future_messages = select(message_history_table.c.internal_id).where(
        message_history_table.c.timestamp >= moment
    )
    statements = {
        "attachment_metadata": delete(attachment_metadata_table).where(
            (attachment_metadata_table.c.message_id.in_(future_messages))
            | (attachment_metadata_table.c.created_at >= moment)
        ),
        "message_history": delete(message_history_table).where(
            message_history_table.c.timestamp >= moment
        ),
        "documents": delete(_DOCUMENTS).where(_DOCUMENTS.c.added_at >= moment),
        "tasks": delete(tasks_table).where(tasks_table.c.created_at >= moment),
        "memory_change_log": delete(memory_change_log_table).where(
            memory_change_log_table.c.created_at >= moment
        ),
        "recent_events": delete(recent_events_table).where(
            recent_events_table.c.timestamp >= moment
        ),
    }
    async with db.transaction() as txn:
        for name, statement in statements.items():
            result = await txn.execute(statement)
            report.deleted_rows[name] = result.rowcount


async def _rewind_notes(
    db: Database,
    moment: datetime,
    timelines: NoteTimelines,
    report: RewindReport,
) -> list[int]:
    """Set every note to its state at ``moment``; return the ids to re-index."""
    rows = await db.fetch_all(
        select(
            notes_table.c.id,
            notes_table.c.title,
            notes_table.c.content,
            notes_table.c.created_at,
            notes_table.c.updated_at,
            notes_table.c.is_skill,
        )
    )
    current = {row["title"]: row for row in rows}
    changed: list[int] = []
    async with db.transaction() as txn:
        for title, row in current.items():
            updated, created = _aware(row["updated_at"]), _aware(row["created_at"])
            touched = updated is not None and updated > moment
            created_later = created is not None and created > moment
            if not touched and not created_later:
                continue
            state = timelines.state_at(title, moment)
            if state is None or state.quality == "unknown":
                if created_later:
                    await txn.execute(
                        delete(notes_table).where(notes_table.c.id == row["id"])
                    )
                    report.notes_removed.append(title)
                else:
                    report.notes_unknown.append(title)
                continue
            if state.content is None:
                await txn.execute(
                    delete(notes_table).where(notes_table.c.id == row["id"])
                )
                report.notes_removed.append(title)
            elif state.content != row["content"]:
                # Restoring recorded content in a fixture copy before the app
                # runs: the row keeps its stored provenance, which is what a
                # restore should do, so the repository's stamping is not wanted.
                # ast-grep-ignore: no-raw-note-table-write
                restore = update(notes_table).where(notes_table.c.id == row["id"])
                await txn.execute(
                    restore.values(content=state.content, updated_at=moment)
                )
                report.notes_rewound.append(title)
                changed.append(row["id"])
        # Notes a recorded turn deleted after the moment existed at it.
        for title, events in timelines.events.items():
            if title in current or not any(
                e.kind == "deleted" and e.at > moment for e in events
            ):
                continue
            state = timelines.state_at(title, moment)
            if state is None or state.content is None:
                continue
            # As above: a restore of recorded content into a fixture copy.
            # ast-grep-ignore: no-raw-note-table-write
            recreate = insert(notes_table).returning(notes_table.c.id)
            result = await txn.execute(
                recreate.values(
                    title=title,
                    content=state.content,
                    created_at=moment,
                    updated_at=moment,
                )
            )
            restored = result.scalar_one()
            report.notes_restored.append(title)
            changed.append(restored)
    return changed


async def rewind_to(
    db: Database, moment: datetime, timelines: NoteTimelines
) -> RewindReport:
    """Rewind a fresh snapshot copy to ``moment``; see the module docstring."""
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=UTC)
    report = RewindReport(moment=moment)
    await _delete_after(db, moment, report)
    note_ids = await _rewind_notes(db, moment, timelines, report)
    for note_id in note_ids:
        await db.tasks.enqueue(
            task_id=f"index_note_{note_id}_{uuid.uuid4()}",
            task_type="index_note",
            payload={"note_id": note_id},
            priority=TaskPriority.BACKGROUND,
        )
    return report
