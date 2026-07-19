"""Tests for POST /api/v1/chat/voice-sessions (native voice transcript save)."""

from datetime import UTC, datetime

import httpx
import pytest
from sqlalchemy.ext.asyncio import AsyncEngine

from family_assistant.assistant import Assistant
from family_assistant.llm.messages import UserMessage
from family_assistant.storage.context import get_db_context
from tests.helpers import wait_for_condition


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
    async with get_db_context(web_only_assistant.database_engine) as db_context:
        rows = await db_context.message_history.get_recent_with_metadata(
            interface_type="web", conversation_id=conversation_id
        )
    assert len(rows) == 3
    assert all(row["taint_metadata_version"] == "runtime_v1" for row in rows)
    assert rows[0]["taint_metadata_json"] is not None
    assert rows[1]["taint_metadata_json"] is not None
    assert rows[2]["taint_metadata_json"] is not None
    assert rows[0]["taint_metadata_json"].get("max_tier") == "trusted_user"
    assert rows[1]["taint_metadata_json"].get("max_tier") == "unknown_external"
    assert rows[2]["taint_metadata_json"].get("max_tier") == "trusted_user"


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
    async with get_db_context(db_engine) as db_context:
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
