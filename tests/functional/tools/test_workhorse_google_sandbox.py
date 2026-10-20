"""The shipped Gmail and Drive tools against the workhorse eval's Google sandbox.

Nothing between the tool and the network is faked: the integration config,
the encrypted stored connection, the real credential resolver refreshing a
token, and the tools' own scope checks and response parsing all run as in
production. Only the token endpoint and the data API are the sandbox's.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import TYPE_CHECKING
from zoneinfo import ZoneInfo

from family_assistant.eval.workhorse.google_sandbox import (
    eval_google_integration,
    google_transport,
    seed_google_connection,
)
from family_assistant.eval.workhorse.google_world import (
    GoogleWorld,
    WorldAttachment,
    WorldFile,
    WorldMessage,
    build_google_world,
)
from family_assistant.llm.tool_call import ToolCallFunction, ToolCallItem
from family_assistant.services.credential_encryption import CredentialEncryption
from family_assistant.services.google_provider import GOOGLE_PROVIDER
from family_assistant.services.oauth_credentials import OAuthCredentialResolver
from family_assistant.storage.database import Database
from family_assistant.storage.types import MessageHistoryRow
from family_assistant.tools.google_data import (
    drive_get_file_tool,
    drive_search_tool,
    drive_write_file_tool,
    gmail_get_message_tool,
    gmail_search_tool,
)
from family_assistant.tools.types import ToolExecutionContext

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncEngine

    from family_assistant.eval.workhorse.fake_google import FakeGoogleBackend

_NOW = datetime(2026, 9, 1, tzinfo=UTC)

_WORLD = GoogleWorld(
    messages=[
        WorldMessage(
            id="m1",
            thread_id="t1",
            sent_at=datetime(2026, 8, 20, tzinfo=UTC),
            date_header="Thu, 20 Aug 2026 00:00:00 +0000",
            from_="Clinic <bookings@clinic.example>",
            to="andrew@example.com",
            subject="Appointment confirmation",
            snippet="Your appointment at Paediatric Dermatology",
            body="Level 5, Acute Services Building, St Leonards.",
            attachments=[
                WorldAttachment(
                    filename="referral.pdf",
                    attachment_id="att-1",
                    part_id="1",
                    mime_type="application/pdf",
                    size=1234,
                )
            ],
            first_seen_at=datetime(2026, 8, 21, tzinfo=UTC),
        ),
        WorldMessage(
            id="m2",
            thread_id="t2",
            sent_at=datetime(2026, 8, 22, tzinfo=UTC),
            date_header="Sat, 22 Aug 2026 00:00:00 +0000",
            from_="School <office@school.example>",
            to="andrew@example.com",
            subject="Newsletter",
            snippet="This week at school",
            first_seen_at=datetime(2026, 8, 23, tzinfo=UTC),
        ),
    ],
    files=[
        WorldFile(
            id="f1",
            name="Pool maintenance log",
            mime_type="application/vnd.google-apps.document",
            owner_name="Andrew",
            owner_email="andrew@example.com",
            modified_time=datetime(2026, 8, 1, tzinfo=UTC),
            web_view_link="https://drive.example/f1",
            content="Cleaned the chlorinator cell.",
            first_seen_at=datetime(2026, 8, 2, tzinfo=UTC),
        )
    ],
)


async def _sandbox(
    db_engine: AsyncEngine,
) -> tuple[ToolExecutionContext, FakeGoogleBackend]:
    db = Database(db_engine)
    integration = eval_google_integration(require_taint_enforcement=True)
    await seed_google_connection(
        db, integration, user_id="andrew", account_email="andrew@example.com"
    )
    transport, backend = google_transport(_WORLD, _NOW)
    resolver = OAuthCredentialResolver(
        GOOGLE_PROVIDER,
        integration,
        CredentialEncryption(integration.credential_encryption_key.get_secret_value()),
        transport.token_http_client,
        None,
    )
    context = ToolExecutionContext(
        interface_type="web",
        conversation_id="eval",
        user_name="Andrew",
        turn_id="turn-1",
        db_context=db,
        processing_service=None,
        clock=None,
        plugins=None,
        event_sources=None,
        attachment_registry=None,
        credential_resolvers={GOOGLE_PROVIDER.name: resolver},
        api_backend=transport.api_backend,
        timezone=ZoneInfo("Australia/Sydney"),
        user_id="andrew",
    )
    return context, backend


async def test_gmail_search_and_read_through_the_real_chain(
    db_engine: AsyncEngine,
) -> None:
    context, backend = await _sandbox(db_engine)

    listing = await gmail_search_tool(context, "dermatology appointment")
    message = await gmail_get_message_tool(context, "m1")

    assert [m["id"] for m in json.loads(listing.get_text())["messages"]] == ["m1"]
    assert "Level 5, Acute Services Building" in message.get_text()
    assert "referral.pdf (attachment_id: att-1" in message.get_text()
    assert backend.unbacked == []


async def test_drive_search_and_export_through_the_real_chain(
    db_engine: AsyncEngine,
) -> None:
    context, backend = await _sandbox(db_engine)

    listing = await drive_search_tool(context, "name contains 'pool'")
    document = await drive_get_file_tool(context, "f1")

    assert [f["id"] for f in json.loads(listing.get_text())["files"]] == ["f1"]
    assert document.get_text() == "Cleaned the chlorinator cell."
    assert backend.unbacked == []


async def test_drive_writes_are_captured_and_readable_back(
    db_engine: AsyncEngine,
) -> None:
    """The write tool's own folder lookup, creation and upload all reach the fake."""
    context, backend = await _sandbox(db_engine)

    written = await drive_write_file_tool(
        context, name="Packing list", content="Passports\nChargers"
    )

    assert backend.unbacked == []
    assert [w["kind"] for w in backend.writes] == ["drive_create", "drive_create"]
    assert backend.writes[1]["content"] == "Passports\nChargers"
    file_id = str(backend.writes[1]["id"])
    assert file_id in written.get_text()
    document = await drive_get_file_tool(context, file_id)
    assert document.get_text() == "Passports\nChargers"


def _recorded(
    calls: list[tuple[str, str, dict[str, object], str]],
) -> dict[tuple[str, str], list[MessageHistoryRow]]:
    rows: list[MessageHistoryRow] = []
    for index, (call_id, name, arguments, content) in enumerate(calls):
        for role in ("assistant", "tool"):
            rows.append(
                MessageHistoryRow(
                    internal_id=index * 2 + (role == "tool"),
                    interface_type="web",
                    conversation_id="c1",
                    interface_message_id=None,
                    turn_id="t1",
                    thread_root_id=None,
                    timestamp=_NOW,
                    role=role,
                    content=content if role == "tool" else None,
                    tool_calls=[
                        ToolCallItem(
                            id=call_id,
                            type="function",
                            function=ToolCallFunction(name=name, arguments=arguments),
                        )
                    ]
                    if role == "assistant"
                    else None,
                    reasoning_info=None,
                    tool_call_id=call_id if role == "tool" else None,
                    error_traceback=None,
                    processing_profile_id="default_assistant",
                    subconversation_id=None,
                    user_id="andrew",
                    attachments=None,
                    tool_name=name if role == "tool" else None,
                    provider_metadata=None,
                    taint_metadata_json=None,
                    taint_metadata_version=None,
                    taint_metadata=None,
                    is_internal=False,
                    echoed_from_conversation_id=None,
                    moved_to_conversation_id=None,
                )
            )
    return {("web", "c1"): rows}


async def test_what_the_tools_render_rebuilds_the_same_world(
    db_engine: AsyncEngine,
) -> None:
    """The world is built from tool output, so the tools' output must parse back."""
    context, _ = await _sandbox(db_engine)
    listing = await gmail_search_tool(context, "dermatology")
    message = await gmail_get_message_tool(context, "m1")
    files = await drive_search_tool(context, "name contains 'pool'")
    document = await drive_get_file_tool(context, "f1")

    rebuilt = build_google_world(
        _recorded([
            ("c1", "gmail_search", {"query": "dermatology"}, listing.get_text()),
            ("c2", "gmail_get_message", {"message_id": "m1"}, message.get_text()),
            ("c3", "drive_search", {"query": "pool"}, files.get_text()),
            ("c4", "drive_get_file", {"file_id": "f1"}, document.get_text()),
        ])
    )

    original = _WORLD.messages[0]
    (message_back,) = rebuilt.messages
    assert message_back.model_dump(exclude={"first_seen_at"}) == original.model_dump(
        exclude={"first_seen_at"}
    )
    (file_back,) = rebuilt.files
    assert file_back.model_dump(exclude={"first_seen_at"}) == _WORLD.files[
        0
    ].model_dump(exclude={"first_seen_at"})
