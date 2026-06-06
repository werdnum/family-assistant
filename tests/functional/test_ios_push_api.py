"""Functional tests for iOS APNs push token API endpoints."""

import httpx
import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine

from family_assistant.storage.ios_push_token import ios_push_tokens_table


@pytest.mark.asyncio
async def test_register_token_creates_row(
    api_client: httpx.AsyncClient,
    db_engine: AsyncEngine,
) -> None:
    """POST /api/ios/push-tokens stores the device token."""
    response = await api_client.post(
        "/api/ios/push-tokens",
        json={
            "device_token": "abc123",
            "environment": "sandbox",
            "bundle_id": "com.example.app",
        },
    )

    assert response.status_code == 200
    data = response.json()
    assert data["status"] == "success"
    assert "id" in data

    async with db_engine.begin() as conn:  # type: ignore[attr-defined]
        result = await conn.execute(
            select(ios_push_tokens_table).where(
                ios_push_tokens_table.c.id == int(data["id"])
            )
        )
        row = result.fetchone()
        assert row is not None
        assert row.device_token == "abc123"
        assert row.environment == "sandbox"
        assert row.bundle_id == "com.example.app"


@pytest.mark.asyncio
async def test_register_token_defaults_to_production(
    api_client: httpx.AsyncClient,
    db_engine: AsyncEngine,
) -> None:
    """Environment defaults to production when omitted."""
    response = await api_client.post(
        "/api/ios/push-tokens", json={"device_token": "def456"}
    )

    assert response.status_code == 200
    async with db_engine.begin() as conn:  # type: ignore[attr-defined]
        result = await conn.execute(select(ios_push_tokens_table))
        row = result.fetchone()
        assert row is not None
        assert row.environment == "production"


@pytest.mark.asyncio
async def test_register_token_is_idempotent(
    api_client: httpx.AsyncClient,
    db_engine: AsyncEngine,
) -> None:
    """Re-registering the same token updates it rather than duplicating."""
    await api_client.post(
        "/api/ios/push-tokens",
        json={"device_token": "tok", "environment": "production"},
    )
    await api_client.post(
        "/api/ios/push-tokens",
        json={"device_token": "tok", "environment": "sandbox"},
    )

    async with db_engine.begin() as conn:  # type: ignore[attr-defined]
        result = await conn.execute(select(ios_push_tokens_table))
        rows = result.fetchall()
        assert len(rows) == 1
        assert rows[0].environment == "sandbox"


@pytest.mark.asyncio
async def test_unregister_token_removes_row(
    api_client: httpx.AsyncClient,
    db_engine: AsyncEngine,
) -> None:
    """DELETE /api/ios/push-tokens/{token} removes the token."""
    await api_client.post(
        "/api/ios/push-tokens",
        json={"device_token": "to-delete", "environment": "production"},
    )

    response = await api_client.delete("/api/ios/push-tokens/to-delete")
    assert response.status_code == 200
    assert response.json()["status"] == "success"

    async with db_engine.begin() as conn:  # type: ignore[attr-defined]
        result = await conn.execute(select(ios_push_tokens_table))
        assert result.fetchall() == []


@pytest.mark.asyncio
async def test_unregister_nonexistent_returns_not_found(
    api_client: httpx.AsyncClient,
) -> None:
    """Deleting a token that does not exist returns not_found."""
    response = await api_client.delete("/api/ios/push-tokens/nope")
    assert response.status_code == 200
    assert response.json()["status"] == "not_found"
