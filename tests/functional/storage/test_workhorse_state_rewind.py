"""Rewinding a database copy to the moment a workhorse eval case was asked."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

from sqlalchemy import select, update

from family_assistant.eval.workhorse.state_rewind import build_note_timelines, rewind_to
from family_assistant.llm.messages import UserMessage
from family_assistant.llm.tool_call import ToolCallFunction, ToolCallItem
from family_assistant.security.note_provenance import NoteProvenanceStamp
from family_assistant.security.taint import TurnTaintState
from family_assistant.storage.database import Database
from family_assistant.storage.message_history import message_history_table
from family_assistant.storage.notes import notes_table
from family_assistant.storage.repositories.notes import NoteWritePolicy
from family_assistant.storage.types import MessageHistoryRow

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncEngine

_T = datetime(2026, 9, 9, 3, 30, tzinfo=UTC)


def _row(
    index: int,
    role: str,
    at: datetime,
    *,
    content: str | None = None,
    tool_calls: list[ToolCallItem] | None = None,
    tool_call_id: str | None = None,
    tool_name: str | None = None,
) -> MessageHistoryRow:
    return MessageHistoryRow(
        internal_id=index,
        interface_type="web",
        conversation_id="c1",
        interface_message_id=None,
        turn_id=f"t{index}",
        thread_root_id=None,
        timestamp=at,
        role=role,
        content=content,
        tool_calls=tool_calls,
        reasoning_info=None,
        tool_call_id=tool_call_id,
        error_traceback=None,
        processing_profile_id="default_assistant",
        subconversation_id=None,
        user_id="andrew",
        attachments=None,
        tool_name=tool_name,
        provider_metadata=None,
        taint_metadata_json=None,
        taint_metadata_version=None,
        taint_metadata=None,
        is_internal=False,
        echoed_from_conversation_id=None,
        moved_to_conversation_id=None,
    )


def _history(
    calls: list[tuple[datetime, str, dict[str, object], str]],
) -> dict[tuple[str, str], list[MessageHistoryRow]]:
    rows: list[MessageHistoryRow] = []
    for index, (at, name, arguments, result) in enumerate(calls):
        call = ToolCallItem(
            id=f"call-{index}",
            type="function",
            function=ToolCallFunction(name=name, arguments=arguments),
        )
        rows.append(_row(index * 2, "assistant", at, tool_calls=[call]))
        rows.append(
            _row(
                index * 2 + 1,
                "tool",
                at,
                content=result,
                tool_call_id=call.id,
                tool_name=name,
            )
        )
    return {("web", "c1"): rows}


def _read(title: str, content: str | None) -> str:
    if content is None:
        return json.dumps({"exists": False, "title": title})
    return json.dumps({"exists": True, "title": title, "content": content})


def test_a_note_is_its_last_known_state_with_later_appends_undone() -> None:
    timelines = build_note_timelines(
        _history([
            (
                _T - timedelta(days=2),
                "get_note",
                {"title": "Trip"},
                _read("Trip", "v1"),
            ),
            (
                _T - timedelta(days=1),
                "add_or_update_note",
                {"title": "Trip", "content": "flights", "append": True},
                "Note 'Trip' updated successfully.",
            ),
            (
                _T + timedelta(hours=1),
                "add_or_update_note",
                {"title": "Trip", "content": "v2"},
                "Note 'Trip' updated successfully.",
            ),
        ])
    )

    state = timelines.state_at("Trip", _T)

    assert state is not None
    assert state.quality == "known"
    assert state.content == "v1\nflights"


def test_without_earlier_evidence_the_next_read_stands_in() -> None:
    timelines = build_note_timelines(
        _history([
            (
                _T + timedelta(hours=2),
                "get_note",
                {"title": "Pool"},
                _read("Pool", "old"),
            ),
        ])
    )

    state = timelines.state_at("Pool", _T)

    assert state is not None
    assert (state.quality, state.content) == ("inferred", "old")


def test_failed_writes_are_not_history() -> None:
    timelines = build_note_timelines(
        _history([
            (_T - timedelta(days=1), "get_note", {"title": "A"}, _read("A", "kept")),
            (
                _T - timedelta(hours=1),
                "add_or_update_note",
                {"title": "A", "content": "never written"},
                "Error: write refused",
            ),
        ])
    )

    state = timelines.state_at("A", _T)

    assert state is not None
    assert state.content == "kept"


async def _note(db: Database, title: str, content: str) -> None:
    await db.notes.add_or_update(
        title=title,
        content=content,
        write_policy=NoteWritePolicy.UNCONSTRAINED,
        provenance=NoteProvenanceStamp.internal(),
    )


async def _stamp(
    db: Database, title: str, *, created: datetime, updated: datetime
) -> None:
    await db.execute(
        update(notes_table)
        .where(notes_table.c.title == title)
        .values(created_at=created, updated_at=updated)
    )


async def test_rewind_restores_notes_and_drops_the_future(
    db_engine: AsyncEngine,
) -> None:
    db = Database(db_engine)
    await _note(db, "Trip", "v1\nflights\nhotel")
    await _stamp(
        db, "Trip", created=_T - timedelta(days=30), updated=_T + timedelta(days=5)
    )
    await _note(db, "Later", "made after the case")
    await _stamp(
        db, "Later", created=_T + timedelta(days=1), updated=_T + timedelta(days=1)
    )
    await _note(db, "Untouched", "same")
    await _stamp(
        db, "Untouched", created=_T - timedelta(days=30), updated=_T - timedelta(days=3)
    )
    for at, text in (
        (_T - timedelta(hours=1), "before"),
        (_T + timedelta(hours=1), "after"),
    ):
        empty = TurnTaintState.empty().to_metadata()
        await db.message_history.add_message(
            UserMessage(
                content=text, taint_metadata=empty, authorship_taint_metadata=empty
            ),
            interface_type="web",
            conversation_id="c1",
            timestamp=at,
        )
    timelines = build_note_timelines(
        _history([
            (
                _T - timedelta(days=2),
                "get_note",
                {"title": "Trip"},
                _read("Trip", "v1"),
            ),
            (
                _T - timedelta(days=1),
                "add_or_update_note",
                {"title": "Trip", "content": "flights", "append": True},
                "Note 'Trip' updated successfully.",
            ),
            (
                _T - timedelta(days=4),
                "get_note",
                {"title": "Gone"},
                _read("Gone", "deleted later"),
            ),
            (
                _T + timedelta(days=2),
                "delete_note",
                {"title": "Gone"},
                json.dumps({"success": True}),
            ),
        ])
    )

    report = await rewind_to(db, _T, timelines)

    notes = {
        row["title"]: row["content"]
        for row in await db.fetch_all(
            select(notes_table.c.title, notes_table.c.content)
        )
    }
    assert notes == {
        "Trip": "v1\nflights",
        "Untouched": "same",
        "Gone": "deleted later",
    }
    assert report.notes_rewound == ["Trip"]
    assert report.notes_removed == ["Later"]
    assert report.notes_restored == ["Gone"]
    remaining = await db.fetch_all(select(message_history_table.c.content))
    assert [row["content"] for row in remaining] == ["before"]
