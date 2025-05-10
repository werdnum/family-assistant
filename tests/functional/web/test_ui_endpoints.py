"""
End-to-end tests for basic UI endpoint accessibility.
Ensures that top-level UI pages load without server errors (status code < 500).
"""

import httpx
import pytest
from fastapi import FastAPI

# Import the main FastAPI application instance
# In a typical test setup, this might be provided by a more complex fixture
# that also handles test-specific configurations (e.g., test database).
from family_assistant.web.app_creator import app as actual_app
from family_assistant.web.auth import AUTH_ENABLED

# Base UI endpoints accessible regardless of auth state (or will redirect to login if auth is on)
# For pages that expect data (e.g., editing a specific note), we test with a
# non-existent item to ensure it returns a client error (like 404) rather than a server error (500).
BASE_UI_ENDPOINTS = [
    ("/", "Notes List Page"),
    ("/notes/add", "Add Note Form Page"),
    ("/notes/edit/non_existent_note_for_test", "Edit Non-Existent Note Form Page"),
    ("/docs/", "Documentation Index Page (may redirect)"),
    ("/docs/USER_GUIDE.md", "USER_GUIDE.md Document Page"),
    ("/history", "Message History Page"),
    ("/tools", "Available Tools Page"),
    ("/tasks", "Tasks List Page"),
    ("/vector-search", "Vector Search Page"),
    ("/documents/upload", "Document Upload Page"),  # New endpoint
    ("/settings/tokens", "Manage API Tokens UI Page"),
]

# UI endpoints related to authentication, typically only active if AUTH_ENABLED is true
AUTH_UI_ENDPOINTS = [
    ("/auth/login", "Login Page"),
    # Add other auth-related UI GET endpoints here if necessary, e.g., a registration page
]

ALL_UI_ENDPOINTS_TO_TEST = BASE_UI_ENDPOINTS
if AUTH_ENABLED:
    ALL_UI_ENDPOINTS_TO_TEST.extend(AUTH_UI_ENDPOINTS)


@pytest.fixture(scope="session")
def app_fixture() -> FastAPI:
    """
    Provides the FastAPI application instance for testing.
    In a real test suite, this fixture might live in conftest.py and
    could include logic to override dependencies (e.g., database connections)
    with test-specific versions.
    """
    return actual_app


@pytest.mark.asyncio
@pytest.mark.parametrize("path, description", ALL_UI_ENDPOINTS_TO_TEST)
async def test_ui_endpoint_accessibility(
    path: str, description: str, app_fixture: FastAPI
) -> None:
    """
    Tests that a given UI endpoint is accessible and does not return a server error.
    It follows redirects and asserts that the final status code is less than 500.
    """
    transport = httpx.ASGITransport(app=app_fixture)
    async with httpx.AsyncClient(
        transport=transport, base_url="http://testserver"
    ) as client:
        response = await client.get(path)
        assert response.status_code < 500, (
            f"UI endpoint '{description}' at '{path}' should be accessible "
            f"(status < 500), but got {response.status_code}. "
            f"Response text (first 500 chars): {response.text[:500]}"
        )
