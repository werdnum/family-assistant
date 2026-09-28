"""
Playwright tests for profile switching UI functionality.

Tests the profile selector component and profile switching behavior
in the web chat interface.
"""

import pytest
from playwright.async_api import expect

from tests.functional.web.conftest import WebTestFixture
from tests.functional.web.pages.chat_page import ChatPage


@pytest.mark.playwright
class TestProfileSwitchingUI:
    """Test suite for the profile switching UI functionality."""

    async def test_profile_options_displayed(
        self, web_test_fixture_readonly: WebTestFixture
    ) -> None:
        """Test that profile options are displayed in the dropdown."""
        page = web_test_fixture_readonly.page
        base_url = web_test_fixture_readonly.base_url

        await page.goto(f"{base_url}/chat")

        # Open the profile selector
        profile_selector = page.locator('button[role="combobox"]').first
        await profile_selector.click()

        # Wait for dropdown to appear
        await page.wait_for_selector('[role="listbox"]')

        # Check that we have profile options
        profile_options = page.locator('[role="option"]')
        option_count = await profile_options.count()
        assert option_count > 0, "No profile options found in dropdown"

        # Verify we have expected profiles
        option_texts = await profile_options.all_text_contents()

        # Should contain common profile types
        has_assistant = any(
            "Assistant" in text or "default" in text for text in option_texts
        )
        assert has_assistant, f"Assistant profile not found in options: {option_texts}"

    async def test_profile_selection_changes_ui(
        self, web_test_fixture_readonly: WebTestFixture
    ) -> None:
        """Test that selecting a profile updates the UI."""
        page = web_test_fixture_readonly.page
        base_url = web_test_fixture_readonly.base_url

        await page.goto(f"{base_url}/chat")

        # Wait for profile selector to be fully loaded
        profile_selector = page.locator('button[role="combobox"]').first
        await expect(profile_selector).to_be_visible()

        # Get initial profile selection
        initial_text = await profile_selector.text_content()
        assert initial_text is not None, "Profile selector should have text content"

        # Open dropdown and select a different profile
        await profile_selector.click()

        # Wait for dropdown options to appear
        await page.wait_for_selector('[role="option"]', timeout=5000)
        profile_options = page.locator('[role="option"]')

        options_count = await profile_options.count()
        assert options_count > 1, (
            f"Should have multiple profile options, found {options_count}"
        )

        # Find a different profile option by checking all options
        option_selected = False
        for i in range(options_count):
            option = profile_options.nth(i)
            option_text = await option.text_content()

            # Skip if this is the currently selected option or has no text
            if not option_text:
                continue

            option_text_clean = option_text.strip()
            initial_text_clean = initial_text.strip()

            # Look for text that contains the profile name but is different from current
            # For test profiles, we expect: Assistant, Test_browser, Test_research
            if (
                option_text_clean != initial_text_clean
                and
                # Check if this option contains a different profile name
                any(
                    profile_name in option_text_clean
                    for profile_name in ["Assistant", "Test_browser", "Test_research"]
                )
                and initial_text_clean not in option_text_clean
            ):
                await option.click()
                option_selected = True

                # Wait for the selector to update with new text
                await page.wait_for_function(
                    f"""() => {{
                        const selector = document.querySelector('button[role="combobox"]');
                        return selector && selector.textContent.trim() !== '{initial_text_clean}';
                    }}""",
                    timeout=5000,
                )

                # Verify the selection changed
                new_text = await profile_selector.text_content()
                assert new_text is not None, (
                    "Profile selector should still have text after change"
                )
                assert new_text.strip() != initial_text_clean, (
                    f"Profile selection did not change: '{new_text.strip()}' == '{initial_text_clean}'"
                )
                break

        assert option_selected, (
            f"Could not find a different profile option to select. Found options count: {options_count}, initial text: '{initial_text}'"
        )

    async def test_profile_persistence_across_refresh(
        self, web_test_fixture_readonly: WebTestFixture
    ) -> None:
        """Test that profile selection persists across page refreshes."""
        page = web_test_fixture_readonly.page
        base_url = web_test_fixture_readonly.base_url

        await page.goto(f"{base_url}/chat")

        profile_selector = page.get_by_role("combobox", name="Processing profile")
        await expect(profile_selector).to_have_text("Assistant")
        await profile_selector.click()
        await page.get_by_role("option", name="Test_browser", exact=False).click()
        await expect(profile_selector).to_have_text("Test_browser")

        await page.reload()

        await expect(profile_selector).to_have_text("Test_browser")

    async def test_profile_switching_creates_new_conversation(
        self, web_test_fixture: WebTestFixture
    ) -> None:
        """Test that switching profiles creates a new conversation."""
        page = web_test_fixture.page
        base_url = web_test_fixture.base_url
        chat_page = ChatPage(page, base_url)

        await chat_page.navigate_to_chat()
        await chat_page.send_message("Conversation before profile switch")
        await chat_page.wait_for_message_content("Test response from mock LLM")
        original_url = page.url

        profile_selector = page.get_by_role("combobox", name="Processing profile")
        await profile_selector.click()
        await page.get_by_role("option", name="Test_browser", exact=False).click()

        await expect(page).not_to_have_url(original_url)
        await expect(profile_selector).to_have_text("Test_browser")
        await expect(page.locator('[data-testid="user-message"]')).to_have_count(0)
        await expect(page.locator('[data-testid="assistant-message"]')).to_have_count(0)

    async def test_profile_selector_loading_state(
        self, web_test_fixture_readonly: WebTestFixture
    ) -> None:
        """Test that profile selector handles loading states properly."""
        page = web_test_fixture_readonly.page
        base_url = web_test_fixture_readonly.base_url

        await page.goto(f"{base_url}/chat")

        # Wait for the page to start loading
        await page.wait_for_load_state("domcontentloaded")

        # Profile selector should appear once loading is complete
        profile_selector = page.locator('button[role="combobox"]').first

        # Wait for profile selector to appear and be ready for interaction
        # This should happen once the profiles API call completes
        await page.wait_for_function(
            """() => {
                // Check if profile selector is present and visible
                const selector = document.querySelector('button[role="combobox"]');
                if (!selector) return false;
                
                // Check if it's not in loading state (should have actual profile text, not "Loading...")
                const textContent = selector.textContent || '';
                return selector.offsetParent !== null && // is visible
                       textContent.trim() !== '' && 
                       !textContent.includes('Loading');
            }""",
            timeout=15000,
        )

        # After loading, profile selector should be visible and interactive
        await expect(profile_selector).to_be_visible()

        # Should have profile text content (not loading text)
        selector_text = await profile_selector.text_content()
        assert selector_text is not None, "Profile selector should have text content"
        assert "Loading" not in selector_text, (
            f"Profile selector should not show loading text: '{selector_text}'"
        )

        # Should be able to click it to open dropdown
        await profile_selector.click()
        dropdown = page.locator('[role="listbox"]')
        await expect(dropdown).to_be_visible(timeout=5000)

    async def test_profile_selector_error_handling(
        self, web_test_fixture_readonly: WebTestFixture
    ) -> None:
        """Test that profile selector handles API errors gracefully."""
        page = web_test_fixture_readonly.page
        base_url = web_test_fixture_readonly.base_url

        # Intercept API calls and simulate error
        await page.route(
            "**/api/v1/profiles",
            lambda route: route.fulfill(
                status=500,
                content_type="application/json",
                body='{"detail": "Internal server error"}',
            ),
        )

        await page.goto(f"{base_url}/chat")

        # Should show error state instead of crashing
        error_indicator = page.locator("text=Error loading profiles")
        await expect(error_indicator).to_be_visible(timeout=5000)

    async def test_profile_descriptions_shown(
        self, web_test_fixture_readonly: WebTestFixture
    ) -> None:
        """Test that profile descriptions are shown in the dropdown."""
        page = web_test_fixture_readonly.page
        base_url = web_test_fixture_readonly.base_url

        await page.goto(f"{base_url}/chat")

        await page.get_by_role("combobox", name="Processing profile").click()
        browser_option = page.get_by_role("option", name="Test_browser", exact=False)
        await expect(browser_option).to_contain_text("Test browser profile for web UI")

    async def test_profile_selector_accessibility(
        self, web_test_fixture_readonly: WebTestFixture
    ) -> None:
        """Test that profile selector is accessible."""
        page = web_test_fixture_readonly.page
        base_url = web_test_fixture_readonly.base_url

        await page.goto(f"{base_url}/chat")

        profile_selector = page.get_by_role("combobox", name="Processing profile")

        # Wait for element to be fully interactive before testing keyboard accessibility
        await expect(profile_selector).to_be_visible()
        await expect(profile_selector).to_be_enabled()

        # Should be keyboard accessible
        await profile_selector.focus()
        await expect(profile_selector).to_be_focused()

        # Should open with Enter key
        await page.keyboard.press("Enter")

        # Dropdown should appear
        dropdown = page.locator('[role="listbox"]')
        await expect(dropdown).to_be_visible()

    async def test_profile_switching_with_messaging(
        self, web_test_fixture: WebTestFixture
    ) -> None:
        """Test that profile switching works correctly with messaging."""
        page = web_test_fixture.page
        base_url = web_test_fixture.base_url
        chat_page = ChatPage(page, base_url)

        await chat_page.navigate_to_chat()
        profile_selector = page.get_by_role("combobox", name="Processing profile")
        await profile_selector.click()
        await page.get_by_role("option", name="Test_browser", exact=False).click()
        await expect(profile_selector).to_have_text("Test_browser")

        async with page.expect_request(
            lambda request: (
                request.method == "POST" and request.url.endswith("/api/v1/chat/turns")
            )
        ) as turn_request:
            await chat_page.send_message("Test message with browser profile")

        turn_payload = (await turn_request.value).post_data_json
        assert turn_payload is not None
        assert turn_payload["profile_id"] == "test_browser"
        await chat_page.wait_for_message_content("Test response from mock LLM")
