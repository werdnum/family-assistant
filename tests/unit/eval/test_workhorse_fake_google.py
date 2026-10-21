"""Tests for the workhorse eval's Google world and its fake Gmail/Drive backend."""

from __future__ import annotations

import base64
import json
from datetime import UTC, datetime

import pytest

from family_assistant.eval.workhorse.fake_google import FakeGoogleBackend
from family_assistant.eval.workhorse.google_search import (
    drive_known_folders,
    drive_query_matches,
    gmail_query_matches,
)
from family_assistant.eval.workhorse.google_world import (
    GoogleWorld,
    WorldFile,
    WorldMessage,
    build_google_world,
)
from family_assistant.llm.tool_call import ToolCallFunction, ToolCallItem
from family_assistant.storage.types import MessageHistoryRow

pytestmark = pytest.mark.no_db

_NOW = datetime(2026, 9, 1, tzinfo=UTC)


def _message(**overrides: object) -> WorldMessage:
    fields: dict[str, object] = {
        "id": "m1",
        "thread_id": "t1",
        "sent_at": datetime(2026, 8, 20, tzinfo=UTC),
        "date_header": "Thu, 20 Aug 2026 00:00:00 +0000",
        "from_": "Clinic <bookings@clinic.example>",
        "to": "andrew@example.com",
        "subject": "Appointment confirmation",
        "snippet": "Your appointment at Paediatric Dermatology",
        "body": "Level 5, Acute Services Building, St Leonards.",
        "first_seen_at": datetime(2026, 8, 21, tzinfo=UTC),
    }
    fields.update(overrides)
    return WorldMessage.model_validate(fields)


def _file(**overrides: object) -> WorldFile:
    fields: dict[str, object] = {
        "id": "f1",
        "name": "Pool maintenance log",
        "mime_type": "application/vnd.google-apps.document",
        "owner_name": "Andrew",
        "owner_email": "andrew@example.com",
        "modified_time": datetime(2026, 8, 1, tzinfo=UTC),
        "web_view_link": None,
        "content": "Cleaned the chlorinator cell.",
        "first_seen_at": datetime(2026, 8, 2, tzinfo=UTC),
    }
    fields.update(overrides)
    return WorldFile.model_validate(fields)


@pytest.mark.parametrize(
    ("query", "expected"),
    [
        ("dermatology", True),
        ("Dermatology appointment", True),
        ("dermatology podiatry", False),
        ("dermatology OR podiatry", True),
        ("from:clinic.example", True),
        ("from:school", False),
        ("subject:confirmation", True),
        ('"Acute Services"', True),
        ("-dermatology", False),
        ("after:2026/08/01 before:2026/09/01", True),
        ("after:2026/08/21", False),
        ("newer_than:30d", True),
        ("newer_than:7d", False),
        ("has:attachment", False),
        ("in:inbox label:important dermatology", True),
        ("dermatology -label:spam", True),
        # OR binds tighter than the implied AND: clinic (podiatry OR dermatology).
        ("clinic podiatry OR dermatology", True),
        ("school podiatry OR dermatology", False),
        # Whole words, not prefixes.
        ("derm", False),
        ("from:me", False),
        ("to:me", True),
        ("in:sent", False),
        ("is:draft", False),
        ("after:1755648000", True),
        ("dermatology -in:trash -category:promotions", True),
    ],
)
def test_gmail_query_approximation(query: str, expected: bool) -> None:
    assert (
        gmail_query_matches(_message(), query, _NOW, me=("andrew@example.com",))
        is expected
    )


def test_unmodelled_gmail_operators_are_reported_not_decided() -> None:
    unmodelled: set[str] = set()

    matched = gmail_query_matches(
        _message(), "dermatology is:unread", _NOW, unmodelled=unmodelled
    )

    assert matched
    assert unmodelled == {"is:unread"}


def test_an_unlisted_drive_folder_is_empty_and_reported() -> None:
    listed = _file(matched_queries=["'folder-1' in parents"])
    other = _file(id="f2", name="Elsewhere")
    known = drive_known_folders([listed, other])
    unmodelled: set[str] = set()

    in_listed = [
        f.id
        for f in (listed, other)
        if drive_query_matches(f, "'folder-1' in parents", known_folders=known)
    ]
    in_unlisted = [
        f.id
        for f in (listed, other)
        if drive_query_matches(
            f, "'folder-9' in parents", known_folders=known, unmodelled=unmodelled
        )
    ]

    assert in_listed == ["f1"]
    assert in_unlisted == []
    assert unmodelled == {"unlisted folder folder-9"}


def test_attachment_operators_are_unknown_for_listing_only_messages() -> None:
    """A listing carries no attachment metadata, so it cannot rule a message out."""
    listed = _message(body=None)

    assert gmail_query_matches(listed, "dermatology has:attachment", _NOW)
    assert gmail_query_matches(listed, "filename:pdf", _NOW)


@pytest.mark.parametrize(
    ("query", "expected"),
    [
        ("name contains 'pool'", True),
        ("name contains 'chlorinator'", False),
        ("fullText contains 'chlorinator'", True),
        ("fullText contains 'pump' or name contains 'pool'", True),
        ("name contains 'pool' and mimeType = 'application/pdf'", False),
        ("trashed = false", True),
    ],
)
def test_drive_query_approximation(query: str, expected: bool) -> None:
    assert drive_query_matches(_file(), query) is expected


def _row(
    internal_id: int,
    role: str,
    *,
    content: str | None = None,
    tool_calls: list[ToolCallItem] | None = None,
    tool_call_id: str | None = None,
    tool_name: str | None = None,
) -> MessageHistoryRow:
    return MessageHistoryRow(
        internal_id=internal_id,
        interface_type="web",
        conversation_id="c1",
        interface_message_id=None,
        turn_id="t1",
        thread_root_id=None,
        timestamp=datetime(2026, 8, 25, tzinfo=UTC),
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


def _rows(
    calls: list[tuple[str, str, dict[str, object], str]],
) -> dict[tuple[str, str], list[MessageHistoryRow]]:
    """Assistant rows calling each tool, then the tool rows answering them."""
    rows: list[MessageHistoryRow] = []
    for index, (call_id, name, arguments, content) in enumerate(calls):
        call = ToolCallItem(
            id=call_id,
            type="function",
            function=ToolCallFunction(name=name, arguments=arguments),
        )
        rows.append(_row(index * 2, "assistant", tool_calls=[call]))
        rows.append(
            _row(
                index * 2 + 1,
                "tool",
                content=content,
                tool_call_id=call_id,
                tool_name=name,
            )
        )
    return {("web", "c1"): rows}


_LISTING = json.dumps({
    "messages": [
        {
            "id": "m1",
            "thread_id": "t1",
            "date": "Thu, 20 Aug 2026 00:00:00 +0000",
            "from": "Clinic <bookings@clinic.example>",
            "to": "andrew@example.com",
            "subject": "Appointment confirmation",
            "snippet": "Your appointment at Paediatric Dermatology",
        },
        {
            "id": "m2",
            "thread_id": "t2",
            "date": "Fri, 21 Aug 2026 00:00:00 +0000",
            "from": "School <office@school.example>",
            "to": "andrew@example.com",
            "subject": "Newsletter",
            "snippet": "This week at school",
        },
    ],
    "more_results_available": False,
})

_RENDERED = (
    "From: Clinic <bookings@clinic.example>\n"
    "To: andrew@example.com\n"
    "Subject: Appointment confirmation\n"
    "Date: Thu, 20 Aug 2026 00:00:00 +0000\n"
    "\n"
    "Level 5, Acute Services Building, St Leonards.\n"
    "\nAttachments: referral.pdf (attachment_id: att-1, part_id: 1, "
    "application/pdf, 1234 bytes)"
)


def test_world_pools_listings_with_fetched_bodies() -> None:
    world = build_google_world(
        _rows([
            ("c1", "gmail_search", {"query": "dermatology"}, _LISTING),
            ("c2", "gmail_get_message", {"message_id": "m1"}, _RENDERED),
            ("c3", "gmail_get_message", {"message_id": "m9"}, "Error: not found"),
        ])
    )

    by_id = {m.id: m for m in world.messages}
    assert set(by_id) == {"m1", "m2"}
    assert by_id["m1"].body == "Level 5, Acute Services Building, St Leonards."
    assert by_id["m1"].attachments[0].filename == "referral.pdf"
    assert by_id["m1"].attachments[0].size == 1234
    assert by_id["m2"].body is None


def test_world_keeps_drive_text_but_not_attachment_stubs() -> None:
    listing = json.dumps({
        "files": [
            {
                "id": "f1",
                "name": "Pool maintenance log",
                "mime_type": "application/vnd.google-apps.document",
                "owner": {"display_name": "Andrew", "email": "andrew@example.com"},
                "modified_time": "2026-08-01T00:00:00Z",
                "web_view_link": "https://drive.example/f1",
            },
            {
                "id": "f2",
                "name": "plate.bgcode",
                "mime_type": "application/octet-stream",
                "owner": {},
                "modified_time": "2026-08-02T00:00:00Z",
                "web_view_link": None,
            },
        ]
    })
    world = build_google_world(
        _rows([
            ("c1", "drive_search", {"query": "name contains 'pool'"}, listing),
            ("c2", "drive_get_file", {"file_id": "f1"}, "Cleaned the cell."),
            (
                "c3",
                "drive_get_file",
                {"file_id": "f2"},
                "Stored plate.bin (10 bytes) as attachment abc.",
            ),
        ])
    )

    by_id = {f.id: f for f in world.files}
    assert by_id["f1"].content == "Cleaned the cell."
    assert by_id["f2"].content is None


def test_reconstruction_does_not_depend_on_which_was_recorded_first() -> None:
    """A fetch recorded before the listing still gets the listing's metadata."""
    listing = json.dumps({
        "files": [
            {
                "id": "f1",
                "name": "Pool maintenance log",
                "mime_type": "application/vnd.google-apps.document",
                "owner": {"display_name": "Andrew", "email": "andrew@example.com"},
                "modified_time": "2026-08-01T00:00:00Z",
                "web_view_link": None,
            }
        ]
    })
    world = build_google_world(
        _rows([
            ("c1", "gmail_get_message", {"message_id": "m1"}, _RENDERED),
            ("c2", "gmail_search", {"query": "dermatology"}, _LISTING),
            ("c3", "drive_get_file", {"file_id": "f1"}, "Cleaned the cell."),
            ("c4", "drive_search", {"query": "pool"}, listing),
        ])
    )

    message = next(m for m in world.messages if m.id == "m1")
    assert message.thread_id == "t1"
    assert message.body == "Level 5, Acute Services Building, St Leonards."
    (file,) = world.files
    assert file.name == "Pool maintenance log"
    assert file.modified_time == datetime(2026, 8, 1, tzinfo=UTC)
    assert file.content == "Cleaned the cell."


def test_a_file_edited_later_stays_visible_to_earlier_cases() -> None:
    def listing(modified: str) -> str:
        return json.dumps({
            "files": [
                {
                    "id": "f1",
                    "name": "Pool maintenance log",
                    "mime_type": "text/plain",
                    "owner": {},
                    "modified_time": modified,
                    "web_view_link": None,
                }
            ]
        })

    world = build_google_world(
        _rows([
            ("c1", "drive_search", {"query": "pool"}, listing("2026-08-01T00:00:00Z")),
            ("c2", "drive_search", {"query": "pool"}, listing("2026-09-15T00:00:00Z")),
        ])
    )

    assert [f.id for f in world.as_of(_NOW).files] == ["f1"]


def test_a_case_sees_only_what_existed_when_it_was_asked() -> None:
    world = GoogleWorld(
        messages=[
            _message(),
            _message(id="later", sent_at=datetime(2026, 9, 5, tzinfo=UTC)),
        ],
        files=[_file()],
    )

    visible = world.as_of(_NOW)

    assert [m.id for m in visible.messages] == ["m1"]


async def test_fake_serves_gmail_in_googles_shapes() -> None:
    backend = FakeGoogleBackend(
        world=GoogleWorld(messages=[_message()], files=[]), now=_NOW
    )

    listing = await backend.request(
        method="GET",
        url="https://gmail.googleapis.com/gmail/v1/users/me/messages",
        access_token="t",
        params={"q": "dermatology", "maxResults": "10"},
    )
    full = await backend.request(
        method="GET",
        url="https://gmail.googleapis.com/gmail/v1/users/me/messages/m1",
        access_token="t",
        params={"format": "full"},
    )

    assert listing.json()["messages"] == [{"id": "m1", "threadId": "t1"}]
    part = full.json()["payload"]["parts"][0]
    body = base64.urlsafe_b64decode(part["body"]["data"] + "==").decode()
    assert body == "Level 5, Acute Services Building, St Leonards."
    assert backend.unbacked == []


async def test_a_listing_only_message_serves_its_snippet_as_partial() -> None:
    backend = FakeGoogleBackend(
        world=GoogleWorld(messages=[_message(body=None)], files=[]), now=_NOW
    )

    full = await backend.request(
        method="GET",
        url="https://gmail.googleapis.com/gmail/v1/users/me/messages/m1",
        access_token="t",
        params={"format": "full"},
    )

    part = full.json()["payload"]["parts"][0]
    body = base64.urlsafe_b64decode(part["body"]["data"] + "==").decode()
    assert body == "Your appointment at Paediatric Dermatology …"
    assert backend.partial == ["gmail message m1: snippet only"]


async def test_unrecorded_content_is_unbacked_not_invented() -> None:
    backend = FakeGoogleBackend(
        world=GoogleWorld(messages=[], files=[_file(content=None)]), now=_NOW
    )

    export = await backend.request(
        method="GET",
        url="https://www.googleapis.com/drive/v3/files/f1/export",
        access_token="t",
        params={"mimeType": "text/plain"},
    )
    attachment = await backend.request(
        method="GET",
        url="https://gmail.googleapis.com/gmail/v1/users/me/messages/m1/attachments/a1",
        access_token="t",
    )

    assert export.status_code == 404
    assert attachment.status_code == 404
    assert len(backend.unbacked) == 2


async def test_writes_are_recorded_and_never_sent() -> None:
    backend = FakeGoogleBackend(world=GoogleWorld(messages=[], files=[]), now=_NOW)

    response = await backend.request(
        method="POST",
        url="https://gmail.googleapis.com/gmail/v1/users/me/drafts",
        access_token="t",
        content=b'{"message": {"raw": "abc"}}',
        content_type="application/json",
    )

    assert response.status_code == 200
    assert backend.writes[0]["kind"] == "gmail_draft"
