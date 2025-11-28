"""
Voice mode integration tests.

These tests verify:
1. Voice page loads correctly
2. Tool execution API works correctly (tested directly)

Note: Full WebSocket integration with the Google GenAI SDK cannot be reliably
mocked at the transport layer because the SDK's session object initialization
depends on internal state management that isn't preserved when intercepting
WebSocket connections. For full voice mode testing, use real Gemini API
credentials with VCR cassettes.
"""

import json
from datetime import UTC, datetime, timedelta

import pytest
from playwright.async_api import Page, Route

from tests.functional.web.conftest import WebTestFixture


@pytest.mark.playwright
@pytest.mark.asyncio
async def test_voice_page_loads_with_start_button(
    web_test_fixture: WebTestFixture,
) -> None:
    """Test that the voice page loads correctly with a visible Start button."""
    page = web_test_fixture.page
    base_url = web_test_fixture.base_url

    await page.goto(f"{base_url}/voice")

    # Should have a visible Start button
    start_button = page.locator("button:has-text('Start')")
    await start_button.wait_for(timeout=10000)
    assert await start_button.is_visible()


@pytest.mark.playwright
@pytest.mark.asyncio
async def test_tool_execution_api_list_notes(web_test_fixture: WebTestFixture) -> None:
    """Test the tool execution API endpoint directly.

    This tests the backend's /api/tools/execute/{name} endpoint which is
    called by the voice mode frontend when Gemini requests tool execution.
    """
    page = web_test_fixture.page
    base_url = web_test_fixture.base_url

    # Call the tool execution API directly via page.evaluate
    result = await page.evaluate(
        """async (baseUrl) => {
            const response = await fetch(`${baseUrl}/api/tools/execute/list_notes`, {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ arguments: {} })
            });
            return {
                status: response.status,
                data: await response.json()
            };
        }""",
        base_url,
    )

    assert result["status"] == 200, f"Expected 200 status, got {result['status']}"
    assert "success" in result["data"] or "result" in result["data"], (
        f"Expected success or result in response, got: {result['data']}"
    )


@pytest.mark.playwright
@pytest.mark.asyncio
async def test_tool_execution_api_nonexistent_tool(
    web_test_fixture: WebTestFixture,
) -> None:
    """Test that the tool execution API returns an error for unknown tools."""
    page = web_test_fixture.page
    base_url = web_test_fixture.base_url

    result = await page.evaluate(
        """async (baseUrl) => {
            const response = await fetch(`${baseUrl}/api/tools/execute/nonexistent_tool_xyz`, {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ arguments: {} })
            });
            return {
                status: response.status,
                data: await response.json()
            };
        }""",
        base_url,
    )

    # API returns 500 for tool execution errors (including unknown tools)
    assert result["status"] in {404, 500}, (
        f"Expected 404 or 500 status for unknown tool, got {result['status']}"
    )


async def _setup_mock_token_endpoint(page: Page, base_url: str) -> None:
    """Set up mock response for the ephemeral token endpoint."""
    expires_at = (datetime.now(UTC) + timedelta(minutes=30)).isoformat()

    async def mock_token_response(route: Route) -> None:
        """Return a mock ephemeral token response."""
        await route.fulfill(
            status=200,
            content_type="application/json",
            body=json.dumps({
                "token": "test-ephemeral-token-12345",
                "expires_at": expires_at,
                "tools": [
                    {
                        "functionDeclarations": [
                            {
                                "name": "list_notes",
                                "description": "List all notes from the system",
                                "parameters": {"type": "OBJECT", "properties": {}},
                            },
                        ]
                    }
                ],
                "system_instruction": "You are a helpful voice assistant.",
                "model": "gemini-2.5-flash-preview-native-audio-dialog",
            }),
        )

    await page.route(f"{base_url}/api/gemini/ephemeral-token", mock_token_response)


@pytest.mark.playwright
@pytest.mark.asyncio
async def test_voice_start_fetches_token(web_test_fixture: WebTestFixture) -> None:
    """Test that clicking Start fetches an ephemeral token.

    Note: This only tests up to the token fetch, not the full WebSocket flow.
    """
    page = web_test_fixture.page
    base_url = web_test_fixture.base_url

    # Set up mock token endpoint
    await _setup_mock_token_endpoint(page, base_url)

    # Navigate to voice page
    await page.goto(f"{base_url}/voice")
    await page.wait_for_selector("button:has-text('Start')", timeout=10000)

    # Click Start Call and wait for token request using Playwright's expect_request
    async with page.expect_request(
        "**/api/gemini/ephemeral-token", timeout=10000
    ) as request_info:
        await page.click("button:has-text('Start')")

    request = await request_info.value
    assert "/api/gemini/ephemeral-token" in request.url, (
        "Token request should be made when starting call"
    )
