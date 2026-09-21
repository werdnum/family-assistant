"""Functional tests for the privacy policy page against the real application.

The unit tests in ``tests/unit/web/test_privacy_policy_page.py`` mount the router
by hand; these go through the app the assistant actually builds, so they fail if
``create_app`` stops registering the route or the real template configuration
cannot render it.
"""

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient


@pytest.mark.asyncio
async def test_privacy_policy_served_by_real_app(actual_app: FastAPI) -> None:
    """The assistant's own app serves the policy at /privacy."""
    transport = ASGITransport(app=actual_app)
    async with AsyncClient(transport=transport, base_url="http://testserver") as client:
        response = await client.get("/privacy")

    assert response.status_code == 200
    assert "text/html" in response.headers["content-type"]
    assert "Family Assistant Privacy Policy" in response.text


@pytest.mark.asyncio
async def test_privacy_policy_route_is_registered(actual_app: FastAPI) -> None:
    """The route is reachable by name, so links to it cannot silently break."""
    assert actual_app.url_path_for("privacy_policy") == "/privacy"
