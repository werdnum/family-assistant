"""
End-to-end tests for basic UI endpoint accessibility.
Ensures that top-level UI pages load without server errors (status code < 500).
"""

import httpx
import pytest

from family_assistant.assistant import Assistant
from family_assistant.web.app_creator import app as fastapi_app
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
    ("/chat", "Chat Interface Page"),
    ("/chat/conversations", "Chat Conversations List Page"),
    ("/settings/tokens", "Manage API Tokens UI Page"),
    ("/events", "Events List Page"),
    ("/events/non_existent_event", "Event Detail Page"),
    ("/event-listeners", "Event Listeners List Page"),
    ("/event-listeners/new", "Create Event Listener Page"),
    ("/event-listeners/99999", "Event Listener Detail Page"),
    ("/errors/", "Error Logs List Page"),
]

# UI endpoints related to authentication, typically only active if AUTH_ENABLED is true
AUTH_UI_ENDPOINTS = [
    ("/auth/login", "Login Page"),
    # Add other auth-related UI GET endpoints here if necessary, e.g., a registration page
]

ALL_UI_ENDPOINTS_TO_TEST = BASE_UI_ENDPOINTS
if AUTH_ENABLED:
    ALL_UI_ENDPOINTS_TO_TEST.extend(AUTH_UI_ENDPOINTS)


@pytest.mark.asyncio
async def test_ui_endpoint_accessibility(web_only_assistant: Assistant) -> None:
    """
    Tests that all UI endpoints are accessible and do not return server errors.
    This single test checks all endpoints to avoid the overhead of setting up
    the web_only_assistant fixture multiple times.
    """
    # Use the fastapi app directly - the web_only_assistant fixture ensures it's properly configured
    # with database setup and all dependencies
    transport = httpx.ASGITransport(app=fastapi_app)
    async with httpx.AsyncClient(
        transport=transport, base_url="http://testserver"
    ) as client:
        failures = []
        for path, description in ALL_UI_ENDPOINTS_TO_TEST:
            try:
                response = await client.get(path)
                if response.status_code >= 500:
                    failures.append(
                        f"UI endpoint '{description}' at '{path}' returned {response.status_code}. "
                        f"Response text (first 500 chars): {response.text[:500]}"
                    )
            except Exception as e:
                failures.append(
                    f"UI endpoint '{description}' at '{path}' raised exception: {type(e).__name__}: {str(e)}"
                )

        if failures:
            pytest.fail("The following endpoints failed:\n" + "\n".join(failures))
