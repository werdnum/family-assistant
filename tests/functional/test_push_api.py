"""Functional tests for push notification API endpoints."""

import httpx
import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine

from family_assistant.config_models import AppConfig, PWAConfig
from family_assistant.storage.push_subscription import push_subscriptions_table
from family_assistant.web.app_creator import app as fastapi_app


@pytest.mark.asyncio
async def test_client_config_returns_vapid_key(
    api_client: httpx.AsyncClient,
) -> None:
    """Test that client config endpoint returns VAPID public key."""
    # Set up app.state.config with pwa_config
    original_config = getattr(fastapi_app.state, "config", None)
    fastapi_app.state.config = AppConfig(
        pwa_config=PWAConfig(vapid_public_key="test-public-key-123")
    )

    try:
        response = await api_client.get("/api/client_config")

        assert response.status_code == 200
        data = response.json()
        assert data["vapidPublicKey"] == "test-public-key-123"
    finally:
        # Restore original config
        if original_config is not None:
            fastapi_app.state.config = original_config
        else:
            delattr(fastapi_app.state, "config")


@pytest.mark.asyncio
async def test_client_config_when_no_vapid_key(
    api_client: httpx.AsyncClient,
) -> None:
    """Test client config returns None when VAPID key not configured."""
    # Set up app.state.config with empty pwa_config (no VAPID key)
    original_config = getattr(fastapi_app.state, "config", None)
    fastapi_app.state.config = AppConfig()

    try:
        response = await api_client.get("/api/client_config")

        assert response.status_code == 200
        data = response.json()
        assert data["vapidPublicKey"] is None
    finally:
        # Restore original config
        if original_config is not None:
            fastapi_app.state.config = original_config
        else:
            delattr(fastapi_app.state, "config")


@pytest.mark.asyncio
async def test_subscribe_creates_subscription(
    api_client: httpx.AsyncClient,
    db_engine: AsyncEngine,
) -> None:
    """Test POST /api/push/subscribe creates subscription in database."""
    subscription_data = {
        "endpoint": "https://push.example.com/abc123",
        "keys": {"p256dh": "test-p256dh-key", "auth": "test-auth-secret"},
    }

    response = await api_client.post(
        "/api/push/subscribe", json={"subscription": subscription_data}
    )

    assert response.status_code == 200
    data = response.json()
    assert data["status"] == "success"
    assert "id" in data

    # Verify in database
    async with db_engine.begin() as conn:  # type: ignore[attr-defined]
        result = await conn.execute(
            select(push_subscriptions_table).where(
                push_subscriptions_table.c.id == int(data["id"])
            )
        )
        row = result.fetchone()
        assert row is not None
        assert row.subscription_json == subscription_data


@pytest.mark.asyncio
async def test_unsubscribe_removes_subscription(
    api_client: httpx.AsyncClient,
    db_engine: AsyncEngine,
) -> None:
    """Test POST /api/push/unsubscribe removes subscription."""
    # First create a subscription
    subscription_data = {
        "endpoint": "https://push.example.com/xyz789",
        "keys": {"p256dh": "test-key", "auth": "test-secret"},
    }

    response = await api_client.post(
        "/api/push/subscribe", json={"subscription": subscription_data}
    )
    assert response.status_code == 200

    # Now unsubscribe
    response = await api_client.post(
        "/api/push/unsubscribe",
        json={"endpoint": "https://push.example.com/xyz789"},
    )

    assert response.status_code == 200
    data = response.json()
    assert data["status"] == "success"

    # Verify removed from database
    async with db_engine.begin() as conn:  # type: ignore[attr-defined]
        result = await conn.execute(select(push_subscriptions_table))
        rows = result.fetchall()
        assert len(rows) == 0


@pytest.mark.asyncio
async def test_unsubscribe_nonexistent_returns_not_found(
    api_client: httpx.AsyncClient,
) -> None:
    """Test unsubscribe for non-existent subscription returns not_found."""
    response = await api_client.post(
        "/api/push/unsubscribe",
        json={"endpoint": "https://push.example.com/nonexistent"},
    )

    assert response.status_code == 200
    data = response.json()
    assert data["status"] == "not_found"


@pytest.mark.asyncio
async def test_multiple_subscriptions_per_user(
    api_client: httpx.AsyncClient,
    db_engine: AsyncEngine,
) -> None:
    """Test that a user can have multiple subscriptions."""
    sub1 = {
        "endpoint": "https://push.example.com/device1",
        "keys": {"p256dh": "key1", "auth": "auth1"},
    }
    sub2 = {
        "endpoint": "https://push.example.com/device2",
        "keys": {"p256dh": "key2", "auth": "auth2"},
    }

    resp1 = await api_client.post("/api/push/subscribe", json={"subscription": sub1})
    resp2 = await api_client.post("/api/push/subscribe", json={"subscription": sub2})

    assert resp1.status_code == 200
    assert resp2.status_code == 200

    # Verify both in database
    async with db_engine.begin() as conn:  # type: ignore[attr-defined]
        result = await conn.execute(select(push_subscriptions_table))
        rows = result.fetchall()
        assert len(rows) == 2
