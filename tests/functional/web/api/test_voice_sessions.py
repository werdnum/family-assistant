"""Tests for POST /api/v1/chat/voice-sessions (native voice transcript save)."""

from datetime import UTC, datetime
from typing import TYPE_CHECKING, cast
from uuid import uuid4

import httpx
import pytest
from sqlalchemy.ext.asyncio import AsyncEngine

from family_assistant.assistant import Assistant
from family_assistant.llm.messages import UserMessage
from family_assistant.services.notifier import NotificationMetadata
from family_assistant.storage.database import Database
from family_assistant.tools import LOCAL_TOOL_REGISTRATIONS
from family_assistant.tools.infrastructure import LocalToolsProvider
from tests.helpers import wait_for_condition

if TYPE_CHECKING:
    from family_assistant.web.web_chat_interface import WebChatInterface


class HandoffNotifier:
    enabled = True

    def __init__(self) -> None:
        self.calls: list[tuple[str, str, NotificationMetadata | None]] = []

    async def send_notification(
        self,
        user_identifier: str,
        title: str,
        body: str,
        db_context: Database,
        *,
        metadata: NotificationMetadata | None = None,
    ) -> None:
        self.calls.append((user_identifier, title, metadata))


@pytest.mark.asyncio
async def test_voice_session_persists_as_listable_conversation(
    web_only_assistant: Assistant,
) -> None:
    """A saved voice session becomes its own web conversation, listed and ordered."""
    assert web_only_assistant.fastapi_app is not None
    transport = httpx.ASGITransport(app=web_only_assistant.fastapi_app)
    async with httpx.AsyncClient(
        transport=transport, base_url="http://testserver"
    ) as client:
        response = await client.post(
            "/api/v1/chat/voice-sessions",
            json={
                "turns": [
                    {"role": "user", "text": "what's the weather"},
                    {"role": "assistant", "text": "it's sunny"},
                    {"role": "user", "text": "thanks"},
                ]
            },
        )
        assert response.status_code == 200
        data = response.json()
        assert data["message_count"] == 3
        conversation_id = data["conversation_id"]
        assert conversation_id.startswith("web_conv_")

        async def conversation_visible() -> dict | None:
            listing = await client.get(
                "/api/v1/chat/conversations", params={"interface_type": "web"}
            )
            if listing.status_code != 200:
                return None
            ids = [c["conversation_id"] for c in listing.json()["conversations"]]
            return listing.json() if conversation_id in ids else None

        await wait_for_condition(
            conversation_visible,
            description="voice conversation to appear in the web list",
        )

        messages_response = await client.get(
            f"/api/v1/chat/conversations/{conversation_id}/messages"
        )
        assert messages_response.status_code == 200
        messages = messages_response.json()["messages"]
        assert [(m["role"], m["content"]) for m in messages] == [
            ("user", "what's the weather"),
            ("assistant", "it's sunny"),
            ("user", "thanks"),
        ]
        # Each user line opens a new turn that its assistant reply shares, so the
        # transcript groups into distinct turns rather than one collapsed turn.
        assert messages[0]["turn_id"] == messages[1]["turn_id"]
        assert messages[2]["turn_id"] != messages[0]["turn_id"]

    # Every transcript row (user AND assistant) is persisted with explicit
    # runtime taint metadata rather than version=None.
    assert web_only_assistant.database_engine is not None
    db_context = Database(web_only_assistant.database_engine)
    rows = await db_context.message_history.get_recent_with_metadata(
        interface_type="web", conversation_id=conversation_id
    )
    assert len(rows) == 3
    assert all(row["taint_metadata_version"] == "runtime_v2" for row in rows)
    assert rows[0]["taint_metadata_json"] is not None
    assert rows[1]["taint_metadata_json"] is not None
    assert rows[2]["taint_metadata_json"] is not None
    assert rows[0]["taint_metadata_json"].get("max_tier") == "trusted_user"
    assert rows[1]["taint_metadata_json"].get("max_tier") == "unknown_external"
    assert rows[2]["taint_metadata_json"].get("max_tier") == "trusted_user"


@pytest.mark.asyncio
async def test_voice_handoff_and_transcript_share_one_conversation(
    web_only_assistant: Assistant,
) -> None:
    assert web_only_assistant.fastapi_app is not None
    web_only_assistant.fastapi_app.state.processing_service.tools_provider = (
        LocalToolsProvider(
            registrations=[
                registration
                for registration in LOCAL_TOOL_REGISTRATIONS
                if registration.name == "send_to_my_chat"
            ]
        )
    )
    notifier = HandoffNotifier()
    web_chat = cast(
        "WebChatInterface", web_only_assistant.fastapi_app.state.chat_interfaces["web"]
    )
    web_chat.notifier = notifier
    conversation_id = f"web_conv_{uuid4()}"
    transport = httpx.ASGITransport(app=web_only_assistant.fastapi_app)
    async with httpx.AsyncClient(
        transport=transport, base_url="http://testserver"
    ) as client:
        handoff = await client.post(
            "/api/tools/execute/send_to_my_chat",
            json={
                "voice_conversation_id": conversation_id,
                "arguments": {
                    "title": "Directions",
                    "content": "Follow this route home.",
                    "action_kind": "open_url",
                    "action_url": "https://maps.example.test/route",
                },
            },
        )
        assert handoff.status_code == 200, handoff.text
        assert handoff.json()["success"] is True
        assert len(notifier.calls) == 1
        assert notifier.calls[0][1] == "Directions"
        assert notifier.calls[0][2] == NotificationMetadata(
            category="FAMILY_ASSISTANT_MESSAGE",
            conversation_id=conversation_id,
            action_kind="open_url",
            action_url="https://maps.example.test/route",
        )

        transcript = await client.post(
            "/api/v1/chat/voice-sessions",
            json={
                "conversation_id": conversation_id,
                "turns": [
                    {
                        "role": "user",
                        "text": "send directions",
                        "timestamp": "2026-01-01T12:00:00Z",
                    },
                    {
                        "role": "assistant",
                        "text": "sent",
                        "timestamp": "2026-01-01T12:00:02Z",
                    },
                ],
            },
        )
        assert transcript.status_code == 200, transcript.text
        messages = await client.get(
            f"/api/v1/chat/conversations/{conversation_id}/messages"
        )
        assert messages.status_code == 200, messages.text
        visible = messages.json()["messages"]
        assert [(row["role"], row["content"]) for row in visible] == [
            ("user", "send directions"),
            ("assistant", "sent"),
            (
                "assistant",
                "**Directions**\n\nFollow this route home.\n\n<https://maps.example.test/route>",
            ),
        ]
        assert web_only_assistant.database_engine is not None
        history = Database(web_only_assistant.database_engine)
        rows = await history.message_history.get_recent_with_metadata(
            interface_type="web", conversation_id=conversation_id
        )
        handoff_row = next(
            row for row in rows if row["content"] == visible[2]["content"]
        )
        assert handoff_row["processing_profile_id"] == "default_assistant"


@pytest.mark.asyncio
async def test_voice_handoff_refuses_foreign_conversation_and_invalid_url(
    web_only_assistant: Assistant,
    db_engine: AsyncEngine,
) -> None:
    assert web_only_assistant.fastapi_app is not None
    web_only_assistant.fastapi_app.state.processing_service.tools_provider = (
        LocalToolsProvider(
            registrations=[
                registration
                for registration in LOCAL_TOOL_REGISTRATIONS
                if registration.name == "send_to_my_chat"
            ]
        )
    )
    foreign_id = f"web_conv_{uuid4()}"
    db = Database(db_engine)
    await db.message_history.add_message(
        UserMessage(content="private"),
        interface_type="web",
        conversation_id=foreign_id,
        timestamp=datetime.now(UTC),
        user_id="another_user",
    )
    transport = httpx.ASGITransport(app=web_only_assistant.fastapi_app)
    async with httpx.AsyncClient(
        transport=transport, base_url="http://testserver"
    ) as client:
        arguments = {
            "title": "Directions",
            "content": "Tap to open",
            "action_kind": "open_url",
            "action_url": "https://maps.example.test/route",
        }
        foreign = await client.post(
            "/api/tools/execute/send_to_my_chat",
            json={"voice_conversation_id": foreign_id, "arguments": arguments},
        )
        assert foreign.status_code == 404

        own_id = f"web_conv_{uuid4()}"
        invalid = await client.post(
            "/api/tools/execute/send_to_my_chat",
            json={
                "voice_conversation_id": own_id,
                "arguments": {**arguments, "action_url": "javascript:alert(1)"},
            },
        )
        assert invalid.status_code == 200
        assert "Error:" in invalid.json()["result"]["text"]
        messages = await client.get(f"/api/v1/chat/conversations/{own_id}/messages")
        assert messages.status_code == 200
        assert messages.json()["messages"] == []


@pytest.mark.asyncio
async def test_voice_session_generates_distinct_conversation_ids(
    web_only_assistant: Assistant,
) -> None:
    """Each save without a client id lands in its own conversation."""
    assert web_only_assistant.fastapi_app is not None
    transport = httpx.ASGITransport(app=web_only_assistant.fastapi_app)
    async with httpx.AsyncClient(
        transport=transport, base_url="http://testserver"
    ) as client:
        payload = {"turns": [{"role": "user", "text": "hi"}]}
        first = await client.post("/api/v1/chat/voice-sessions", json=payload)
        second = await client.post("/api/v1/chat/voice-sessions", json=payload)
        assert first.status_code == 200
        assert second.status_code == 200
        assert first.json()["conversation_id"] != second.json()["conversation_id"]


@pytest.mark.asyncio
async def test_voice_session_accepts_unused_client_conversation_id(
    web_only_assistant: Assistant,
) -> None:
    """A client may supply a fresh (unused) conversation id."""
    assert web_only_assistant.fastapi_app is not None
    transport = httpx.ASGITransport(app=web_only_assistant.fastapi_app)
    async with httpx.AsyncClient(
        transport=transport, base_url="http://testserver"
    ) as client:
        response = await client.post(
            "/api/v1/chat/voice-sessions",
            json={
                "conversation_id": "web_conv_client_supplied",
                "turns": [{"role": "user", "text": "hi"}],
            },
        )
        assert response.status_code == 200
        assert response.json()["conversation_id"] == "web_conv_client_supplied"


@pytest.mark.asyncio
async def test_voice_session_rejects_foreign_conversation_id(
    web_only_assistant: Assistant,
    db_engine: AsyncEngine,
) -> None:
    """Appending to another user's conversation is refused (404), so it can't be
    hijacked into a multi-owner conversation that disappears for its real owner."""
    foreign_conversation_id = "web_conv_owned_by_someone_else"
    db_context = Database(db_engine)
    await db_context.message_history.add_message(
        UserMessage(content="not yours"),
        interface_type="web",
        conversation_id=foreign_conversation_id,
        timestamp=datetime(2026, 1, 1, 12, 0, 0, tzinfo=UTC),
        turn_id="foreign-turn",
        user_id="some_other_user",
    )

    assert web_only_assistant.fastapi_app is not None
    transport = httpx.ASGITransport(app=web_only_assistant.fastapi_app)
    async with httpx.AsyncClient(
        transport=transport, base_url="http://testserver"
    ) as client:
        response = await client.post(
            "/api/v1/chat/voice-sessions",
            json={
                "conversation_id": foreign_conversation_id,
                "turns": [{"role": "user", "text": "let me in"}],
            },
        )
        assert response.status_code == 404


@pytest.mark.asyncio
async def test_voice_session_without_profile_records_the_default_profile(
    web_only_assistant: Assistant,
) -> None:
    """An omitted profile means the default, which is what the token endpoint
    resolves an omitted profile to. Recording it (rather than nothing) is what
    lets the conversation be reopened: history is read back filtered by profile,
    and an unstamped transcript matches no profile at all."""
    app = web_only_assistant.fastapi_app
    assert app is not None
    default_profile_id = app.state.processing_service.service_config.id
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(
        transport=transport, base_url="http://testserver"
    ) as client:
        response = await client.post(
            "/api/v1/chat/voice-sessions",
            json={"turns": [{"role": "user", "text": "hi"}]},
        )
        assert response.status_code == 200
        conversation_id = response.json()["conversation_id"]

        messages = await client.get(
            f"/api/v1/chat/conversations/{conversation_id}/messages",
            params={"include_conversation_profile": True},
        )
        assert messages.status_code == 200
        assert messages.json()["latest_user_profile_id"] == default_profile_id


@pytest.mark.asyncio
async def test_voice_session_records_the_profile_the_session_ran_under(
    web_only_assistant: Assistant,
) -> None:
    """The profile the client echoes back from its ephemeral token is what the
    transcript is filed under, so reopening the conversation lands on the profile
    that holds its history rather than on whichever one the user last picked."""
    app = web_only_assistant.fastapi_app
    assert app is not None
    default_profile_id = app.state.processing_service.service_config.id
    other_profile_id = next(
        profile_id
        for profile_id, service in app.state.processing_services.items()
        if profile_id != default_profile_id and service.kind != "remote"
    )
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(
        transport=transport, base_url="http://testserver"
    ) as client:
        response = await client.post(
            "/api/v1/chat/voice-sessions",
            json={
                "turns": [{"role": "user", "text": "hi"}],
                "profile_id": other_profile_id,
            },
        )
        assert response.status_code == 200
        conversation_id = response.json()["conversation_id"]

        messages = await client.get(
            f"/api/v1/chat/conversations/{conversation_id}/messages",
            params={"include_conversation_profile": True},
        )
        assert messages.status_code == 200
        assert messages.json()["latest_user_profile_id"] == other_profile_id


@pytest.mark.asyncio
async def test_voice_session_rejects_unknown_profile(
    web_only_assistant: Assistant,
) -> None:
    """A stamp no profile answers to reads back as history nothing can load, so
    it is refused rather than stored."""
    assert web_only_assistant.fastapi_app is not None
    transport = httpx.ASGITransport(app=web_only_assistant.fastapi_app)
    async with httpx.AsyncClient(
        transport=transport, base_url="http://testserver"
    ) as client:
        response = await client.post(
            "/api/v1/chat/voice-sessions",
            json={
                "turns": [{"role": "user", "text": "hi"}],
                "profile_id": "no_such_profile",
            },
        )
        assert response.status_code == 400


@pytest.mark.asyncio
async def test_voice_session_rejects_empty_turns(
    web_only_assistant: Assistant,
) -> None:
    """An empty transcript is a client error, not a phantom conversation."""
    assert web_only_assistant.fastapi_app is not None
    transport = httpx.ASGITransport(app=web_only_assistant.fastapi_app)
    async with httpx.AsyncClient(
        transport=transport, base_url="http://testserver"
    ) as client:
        response = await client.post("/api/v1/chat/voice-sessions", json={"turns": []})
        assert response.status_code == 400
