from __future__ import annotations

import httpx
import pytest

from family_assistant.config_models import AppConfig
from family_assistant.web.app_creator import create_app


@pytest.mark.asyncio
async def test_ucp_profile_endpoint_is_public_and_cacheable() -> None:
    app = create_app()
    app.state.config = AppConfig(server_url="https://assistant.example")

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(
        transport=transport, base_url="https://assistant.example"
    ) as client:
        response = await client.get("/.well-known/ucp")

    assert response.status_code == 200
    assert response.headers["cache-control"] == "public, max-age=300"
    body = response.json()
    assert body["ucp"]["version"] == "2026-04-08"
    assert "dev.ucp.shopping" in body["ucp"]["services"]
    assert body["ucp"]["services"]["dev.ucp.shopping"][0]["transport"] == "mcp"
    assert body["ucp"]["payment_handlers"] == {}
    assert body["signing_keys"] == []
