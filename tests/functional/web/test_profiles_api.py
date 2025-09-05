"""
Simplified functional tests for the profiles API endpoint.

Tests the /v1/profiles endpoint that provides available service profiles
for the chat interface profile switching functionality.
"""

import pytest

from tests.functional.web.conftest import WebTestFixture


@pytest.mark.asyncio
class TestProfilesAPI:
    """Test suite for the /v1/profiles API endpoint."""

    async def test_get_profiles_returns_success(
        self, web_test_fixture: WebTestFixture
    ) -> None:
        """Test that profiles endpoint returns successful response."""
        page = web_test_fixture.page
        base_url = web_test_fixture.base_url

        # Navigate to the profiles API endpoint and check response
        await page.goto(f"{base_url}/api/v1/profiles")

        # Get the page content (which should be JSON)
        content = await page.text_content("body")

        # Basic validation that we got a JSON response
        assert content is not None
        assert "profiles" in content
        assert "default_profile_id" in content

        # The page should not show an error
        error_indicators = await page.locator("text=error").count()
        assert error_indicators == 0

    async def test_profiles_endpoint_accessible(
        self, web_test_fixture: WebTestFixture
    ) -> None:
        """Test that profiles endpoint is accessible without errors."""
        page = web_test_fixture.page
        base_url = web_test_fixture.base_url

        # Navigate to the profiles API endpoint
        response = await page.goto(f"{base_url}/api/v1/profiles")

        # Should not be a 4xx or 5xx error
        assert response is not None
        assert response.status < 400, f"API returned error status: {response.status}"

    @pytest.mark.flaky(reruns=3, reruns_delay=2)
    async def test_profiles_content_type(
        self, web_test_fixture: WebTestFixture
    ) -> None:
        """Test that profiles endpoint returns JSON content type."""
        page = web_test_fixture.page
        base_url = web_test_fixture.base_url

        # Navigate to the profiles API endpoint
        response = await page.goto(f"{base_url}/api/v1/profiles")

        assert response is not None
        headers = response.headers
        content_type = headers.get("content-type", "")
        assert "application/json" in content_type
