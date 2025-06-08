"""Test notes web UI functionality."""

import httpx
import pytest
from sqlalchemy.ext.asyncio import AsyncEngine

from family_assistant.storage.context import DatabaseContext
from family_assistant.web.app_creator import app as actual_app


@pytest.mark.asyncio
async def test_notes_ui_endpoints_accessible(test_db_engine: AsyncEngine) -> None:
    """Test that notes UI endpoints are accessible and don't crash."""
    from family_assistant import storage

    async with DatabaseContext(engine=test_db_engine) as db_context:
        # Add test notes with different include_in_prompt values
        await storage.add_or_update_note(
            db_context, "Test Note", "Test content", include_in_prompt=True
        )
        await storage.add_or_update_note(
            db_context, "Excluded Note", "Excluded content", include_in_prompt=False
        )

    # Create test client
    transport = httpx.ASGITransport(app=actual_app)
    async with httpx.AsyncClient(
        transport=transport, base_url="http://testserver"
    ) as client:
        # Test that endpoints don't crash
        endpoints = [
            ("/", "Notes list"),
            ("/notes/add", "Add note form"),
            ("/notes/edit/Test Note", "Edit existing note form"),
            ("/notes/edit/NonExistent", "Edit non-existent note form"),
        ]

        for endpoint, description in endpoints:
            response = await client.get(endpoint)
            # Accept redirects (307) or not found (404) or success (200)
            # The important thing is no server errors (5xx)
            assert response.status_code < 500, (
                f"{description} at {endpoint} failed with status {response.status_code}"
            )


@pytest.mark.asyncio
async def test_notes_save_endpoint_accessible(test_db_engine: AsyncEngine) -> None:
    """Test that the save endpoint is accessible."""
    # Create test client
    transport = httpx.ASGITransport(app=actual_app)
    async with httpx.AsyncClient(
        transport=transport, base_url="http://testserver"
    ) as client:
        # Test POST to save endpoint
        response = await client.post(
            "/notes/save",
            data={
                "title": "Test",
                "content": "Test",
                "include_in_prompt": "true",
            },
            follow_redirects=False,
        )
        # Should not crash (status < 500)
        assert response.status_code < 500, (
            f"Save endpoint failed with status {response.status_code}"
        )
