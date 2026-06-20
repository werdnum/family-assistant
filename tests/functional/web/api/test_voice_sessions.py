"""Tests for POST /api/v1/chat/voice-sessions (native voice transcript save)."""

import httpx
import pytest

from family_assistant.assistant import Assistant
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
