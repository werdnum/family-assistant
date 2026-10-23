"""Rebuilding the household's Tuit tasks from recorded Tuit tool results."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

import pytest

from family_assistant.eval.workhorse.tuit_world import TuitWorld, build_tuit_world
from family_assistant.llm.tool_call import ToolCallFunction, ToolCallItem
from family_assistant.storage.types import MessageHistoryRow

if TYPE_CHECKING:
    from collections.abc import Mapping

pytestmark = pytest.mark.no_db

_T = datetime(2026, 10, 1, tzinfo=UTC)
_FA_USER = "alex@example.com"


def _iso(moment: datetime) -> str:
    return moment.isoformat().replace("+00:00", "Z")


def _task(
    task_id: str, title: str, *, at: datetime, **fields: object
) -> dict[str, object]:
    return {
        "id": task_id,
        "seq": 1,
        "title": title,
        "owner": "alex",
        "visibility": "household",
        "next_actor": {"kind": "user", "user": "alex"},
        "actor_since": _iso(at),
        "state": "open",
        "revision": 1,
        "created_at": _iso(at),
        "updated_at": _iso(at),
        "created_by": {"user": "alex", "agent": "family-assistant"},
        **fields,
    }


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
        turn_id="t1",
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
        user_id=_FA_USER,
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
    calls: list[tuple[datetime, str, Mapping[str, object], object]],
) -> dict[tuple[str, str], list[MessageHistoryRow]]:
    rows: list[MessageHistoryRow] = []
    for index, (at, name, arguments, result) in enumerate(calls):
        call = ToolCallItem(
            id=f"call-{index}",
            type="function",
            function=ToolCallFunction(name=name, arguments=dict(arguments)),
        )
        rows.append(_row(index * 2, "assistant", at, tool_calls=[call]))
        rows.append(
            _row(
                index * 2 + 1,
                "tool",
                at,
                content=result if isinstance(result, str) else json.dumps(result),
                tool_call_id=call.id,
                tool_name=name,
            )
        )
    return {("web", "c1"): rows}


def _build(
    calls: list[tuple[datetime, str, Mapping[str, object], object]],
) -> TuitWorld:
    return build_tuit_world(_history(calls), user_ids={_FA_USER: "alex"})


def test_a_case_sees_each_task_as_it_stood() -> None:
    created = _T - timedelta(days=3)
    before = _task("aaaa1111", "Ring the vet", at=created)
    after = _task(
        "aaaa1111",
        "Ring the vet about Milo",
        at=created,
        updated_at=_iso(_T + timedelta(days=1)),
        revision=2,
    )
    later = _task("bbbb2222", "Book the plumber", at=_T + timedelta(days=2))
    world = _build([
        (_T - timedelta(days=1), "tuit_find_tasks", {}, {"tasks": [before]}),
        (
            _T + timedelta(days=1),
            "tuit_get_task",
            {"task_id": "aaaa1111"},
            {"task": after},
        ),
        (_T + timedelta(days=3), "tuit_find_tasks", {}, {"tasks": [after, later]}),
    ])

    vet = world.task("aaaa1111")
    assert vet is not None
    assert len(vet.observations) == 3
    seen = vet.as_of(_T)
    assert seen is not None
    assert seen.title == "Ring the vet"
    latest = vet.as_of(_T + timedelta(days=5))
    assert latest is not None
    assert latest.title == "Ring the vet about Milo"
    plumber = world.task("bbbb2222")
    assert plumber is not None
    assert plumber.as_of(_T) is None
    assert world.users == ["alex"]


def test_a_task_first_seen_later_is_shown_before_it_closed() -> None:
    created = _T - timedelta(days=5)
    done = _task(
        "cccc3333",
        "Renew the passport",
        at=created,
        state="done",
        closed_at=_iso(_T + timedelta(days=1)),
        attachments=[
            {
                "id": "x",
                "url": "https://example.com/a",
                "title": "Form",
                "added_at": _iso(_T + timedelta(hours=1)),
            }
        ],
    )
    world = _build([(_T + timedelta(days=2), "tuit_find_tasks", {}, {"tasks": [done]})])

    task = world.task("cccc3333")
    assert task is not None
    at_t = task.as_of(_T)
    assert at_t is not None
    assert at_t.state == "open"
    assert at_t.closed_at is None
    assert at_t.attachments == []
    latest = task.as_of(_T + timedelta(days=3))
    assert latest is not None
    assert latest.state == "done"


_CREATED = _T - timedelta(days=2)
_NOTE_AT = _T - timedelta(hours=1)
_VET = _task("dddd4444", "Find a vet", at=_CREATED)
_AFTER_NOTE = {**_VET, "updated_at": _iso(_NOTE_AT), "revision": 2}
_CHECKPOINT = (
    _NOTE_AT,
    "tuit_checkpoint",
    {"task_id": "dddd4444", "note": "Called the first one", "next_actor": "me"},
    {"task": _AFTER_NOTE, "status": {"pinned": False}},
)


def test_activity_is_pooled_with_notes_written_but_never_read_back() -> None:
    created = _CREATED
    entry = {
        "id": 7,
        "kind": "research",
        "body": "Found two vets nearby",
        "happened_at": _iso(created),
        "recorded_at": _iso(created),
        "author": {"user": "alex", "agent": "chatgpt"},
    }
    task = _VET
    world = _build([
        (
            created,
            "tuit_get_task",
            {"task_id": "dddd4444"},
            {"task": task, "activity": [entry]},
        ),
        (
            created,
            "tuit_get_task",
            {"task_id": "dddd4444"},
            {"task": task, "activity": [entry]},
        ),
        _CHECKPOINT,
    ])

    vet = world.task("dddd4444")
    assert vet is not None
    assert [(a.id, a.kind, a.body) for a in vet.activity] == [
        (7, "research", "Found two vets nearby"),
        (None, "note", "Called the first one"),
    ]
    written = vet.activity[1]
    assert written.recorded_at == _NOTE_AT
    assert written.author == {"user": "alex", "agent": "family-assistant"}
    assert [a.id for a in vet.activity_as_of(_T - timedelta(days=1))] == [7]


def test_a_written_note_read_back_later_is_kept_once() -> None:
    read_back = {
        "id": 8,
        "kind": "note",
        "body": "Called the first one",
        "happened_at": _iso(_NOTE_AT),
        "recorded_at": _iso(_NOTE_AT),
        "author": {"user": "alex", "agent": "family-assistant"},
    }
    world = _build([
        _CHECKPOINT,
        (
            _T,
            "tuit_get_task",
            {"task_id": "dddd4444"},
            {"task": _AFTER_NOTE, "activity": [read_back]},
        ),
    ])

    vet = world.task("dddd4444")
    assert vet is not None
    assert [(a.id, a.body) for a in vet.activity] == [(8, "Called the first one")]


def test_failures_and_other_trackers_are_ignored() -> None:
    world = _build([
        (
            _T,
            "tuit_get_task",
            {"task_id": "nope"},
            {"error": "not_found", "message": "No task nope"},
        ),
        (_T, "tuit_checkpoint", {}, "Error executing tool 'tuit_checkpoint': invalid"),
        (_T, "find_tasks", {}, [{"id": 2, "title": "Old tracker task", "done": False}]),
        (_T, "list_projects", {}, [{"id": 4, "title": "Inbox"}]),
    ])

    assert world.tasks == []
    assert world.users == ["alex"]


def test_pins_follow_the_reader_over_time() -> None:
    created = _T - timedelta(days=4)
    task = _task("eeee5555", "Pay the rates", at=created)
    now = {
        "as_of": _iso(_T),
        "plan": [
            {
                "item": {"why": "deadline soon", "pinned": True, "task": task},
                "done": False,
            }
        ],
        "new_items": [],
        "also": [],
        "urgent": [],
    }
    world = _build([
        (_T - timedelta(days=1), "tuit_now", {}, now),
        (
            _T + timedelta(days=1),
            "tuit_pin_task",
            {"task_id": "eeee5555", "pinned": False},
            {"task": task, "status": {"pinned": False}},
        ),
    ])

    rates = world.task("eeee5555")
    assert rates is not None
    assert rates.pinned_as_of(_T) == {"alex"}
    assert rates.pinned_as_of(_T + timedelta(days=2)) == set()
    assert rates.pinned_as_of(_T - timedelta(days=2)) == {"alex"}


def test_feed_only_tasks_are_stubs_with_their_creator() -> None:
    created = _T - timedelta(days=1)
    changes = {
        "events": [
            {
                "seq": 1,
                "type": "created",
                "at": _iso(created),
                "task": {
                    "id": "ffff6666",
                    "title": "Fix the gate",
                    "state": "open",
                    "visibility": "household",
                },
                "actor": {"user": "sam", "agent": None},
                "data": {},
            }
        ],
        "cursor": "1",
    }
    world = _build([(_T, "tuit_get_changes", {}, changes)])

    gate = world.task("ffff6666")
    assert gate is not None
    assert all(o.stub for o in gate.observations)
    snapshot = gate.as_of(_T)
    assert snapshot is not None
    assert snapshot.title == "Fix the gate"
    assert snapshot.owner == "sam"
    assert gate.created_at == created
    assert gate.as_of(created - timedelta(seconds=1)) is None
    assert world.users == ["alex", "sam"]


def test_queues_exist_from_their_creation() -> None:
    queue = {
        "id": "qqqq7777",
        "name": "Home jobs",
        "owner": "alex",
        "visibility": "private",
        "enabled": True,
        "config": {"areas": ["home"], "visible_limit": 5},
        "revision": 1,
        "created_at": _iso(_T - timedelta(days=1)),
        "updated_at": _iso(_T - timedelta(days=1)),
        "created_by_agent": "chatgpt",
    }
    world = _build([(_T, "tuit_list_queues", {}, {"queues": [queue]})])

    assert [q.id for q in world.queues] == ["qqqq7777"]
    snapshot = world.queues[0].as_of(_T)
    assert snapshot is not None
    assert snapshot.config == {"areas": ["home"], "visible_limit": 5}
    assert world.queues[0].as_of(_T - timedelta(days=2)) is None
