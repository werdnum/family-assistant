"""Visual documentation test suite.

This module contains tests that generate screenshots for all major application flows
across different viewports and themes. These tests only run when GENERATE_VISUAL_DOCS=1
environment variable is set.

Usage:
    # Generate all visual documentation
    GENERATE_VISUAL_DOCS=1 pytest -m visual_documentation tests/functional/web/test_visual_documentation.py

    # Generate specific viewport only
    GENERATE_VISUAL_DOCS=1 pytest -m "visual_documentation and mobile" tests/functional/web/

    # Regular tests (these are skipped)
    pytest tests/functional/web/
"""

import os

import pytest
from playwright.async_api import ViewportSize

from tests.functional.web.conftest import VisualDocumentationHelper, WebTestFixture
from tests.functional.web.pages.chat_page import ChatPage
from tests.mocks.mock_llm import LLMOutput, RuleBasedMockLLMClient

# Skip all visual documentation tests unless explicitly enabled
pytestmark = [
    pytest.mark.visual_documentation,
    pytest.mark.skipif(
        not os.getenv("GENERATE_VISUAL_DOCS"),
        reason="Visual documentation only runs when GENERATE_VISUAL_DOCS=1",
    ),
]


class TestChatFlowVisualDocumentation:
    """Visual documentation for chat interface flows."""

    @pytest.mark.parametrize(
        "viewport,theme",
        [
            pytest.param("mobile", "light", marks=pytest.mark.mobile),
            pytest.param("desktop", "light", marks=pytest.mark.desktop),
            pytest.param("desktop", "dark", marks=pytest.mark.desktop),
        ],
    )
    async def test_chat_conversation_flow(
        self,
        web_test_fixture: WebTestFixture,
        mock_llm_client: RuleBasedMockLLMClient,
        request: pytest.FixtureRequest,
        viewport: str,
        theme: str,
    ) -> None:
        """Document the complete chat conversation flow."""
        page = web_test_fixture.page

        # viewport and theme are now passed as parameters directly

        # Set up viewport and theme
        viewport_configs = {
            "mobile": ViewportSize(width=375, height=667),
            "desktop": ViewportSize(width=1280, height=720),
        }
        await page.set_viewport_size(viewport_configs[viewport])

        # Set theme
        await page.evaluate(f"""
            localStorage.setItem('family-assistant-theme', '{theme}');
            window.dispatchEvent(new Event('storage'));
        """)
        await page.wait_for_timeout(500)

        # Create visual documentation helper
        helper = VisualDocumentationHelper(page, viewport, theme)

        # Configure mock LLM response
        mock_llm_client.rules = [
            (
                lambda args: "Hello" in str(args.get("messages", [])),
                LLMOutput(
                    content="Hello! I'm your assistant. How can I help you today?"
                ),
            )
        ]
        mock_llm_client.default_response = LLMOutput(
            content="I'm here to help with your tasks!"
        )

        chat_page = ChatPage(page, web_test_fixture.base_url)

        # Step 1: Navigate to chat
        await chat_page.navigate_to_chat()
        await page.wait_for_timeout(2000)
        await helper.capture_step(
            "chat", "initial-load", "Chat interface after initial load"
        )

        # Step 2: Create new conversation
        await chat_page.create_new_chat()
        await page.wait_for_timeout(1000)
        await helper.capture_step(
            "chat", "new-conversation", "New conversation created"
        )

        # Step 3: Show chat input ready state
        await helper.capture_step(
            "chat", "input-ready", "Chat input ready for user message"
        )

        # Step 4: Send message
        await chat_page.send_message("Hello, assistant!")
        await page.wait_for_timeout(1000)
        await helper.capture_step(
            "chat", "message-sent", "User message sent, waiting for response"
        )

        # Step 5: Wait for response and capture
        await chat_page.wait_for_messages_with_content(
            {
                "user": "Hello, assistant!",
                "assistant": "assistant",
            },
            timeout=20000,
        )
        await helper.capture_step(
            "chat",
            "conversation-complete",
            "Complete conversation with user and assistant messages",
        )

        # Step 6: Mobile-specific sidebar test
        if viewport == "mobile":
            # Test mobile sidebar
            sidebar_toggle = page.locator('[data-testid="sidebar-toggle"]').first
            if await sidebar_toggle.is_visible():
                await sidebar_toggle.click()
                await page.wait_for_timeout(1000)
                await helper.capture_step(
                    "chat", "mobile-sidebar-open", "Mobile sidebar opened"
                )

                # Close sidebar
                await sidebar_toggle.click()
                await page.wait_for_timeout(1000)
                await helper.capture_step(
                    "chat", "mobile-sidebar-closed", "Mobile sidebar closed"
                )

    @pytest.mark.parametrize(
        "viewport,theme",
        [
            pytest.param("desktop", "light", marks=pytest.mark.desktop),
        ],
    )
    async def test_chat_profile_switching_flow(
        self,
        web_test_fixture: WebTestFixture,
        request: pytest.FixtureRequest,
        viewport: str,
        theme: str,
    ) -> None:
        """Document the profile switching interface."""
        page = web_test_fixture.page

        # viewport and theme are now passed as parameters directly

        # Set up viewport and theme
        await page.set_viewport_size(ViewportSize(width=1280, height=720))
        await page.evaluate(f"""
            localStorage.setItem('family-assistant-theme', '{theme}');
            window.dispatchEvent(new Event('storage'));
        """)
        await page.wait_for_timeout(500)

        helper = VisualDocumentationHelper(page, viewport, theme)
        chat_page = ChatPage(page, web_test_fixture.base_url)

        # Navigate to chat
        await chat_page.navigate_to_chat()
        await page.wait_for_timeout(2000)

        # Step 1: Initial state with default profile
        await helper.capture_step(
            "profile-switching", "default-profile", "Chat with default profile selected"
        )

        # Step 2: Click profile selector
        profile_selector = page.locator('[data-testid="profile-selector"]').first
        if await profile_selector.is_visible():
            await profile_selector.click()
            await page.wait_for_timeout(500)
            await helper.capture_step(
                "profile-switching",
                "profile-dropdown-open",
                "Profile selection dropdown opened",
            )

            # Step 3: Select different profile if available
            browser_profile = page.locator('text="test_browser"').first
            if await browser_profile.is_visible():
                await browser_profile.click()
                await page.wait_for_timeout(1000)
                await helper.capture_step(
                    "profile-switching",
                    "profile-switched",
                    "Profile switched to browser profile",
                )


class TestNotesFlowVisualDocumentation:
    """Visual documentation for notes management flows."""

    @pytest.mark.parametrize(
        "viewport,theme",
        [
            pytest.param("mobile", "light", marks=pytest.mark.mobile),
            pytest.param("desktop", "light", marks=pytest.mark.desktop),
        ],
    )
    async def test_notes_crud_flow(
        self,
        web_test_fixture: WebTestFixture,
        request: pytest.FixtureRequest,
        viewport: str,
        theme: str,
    ) -> None:
        """Document the complete notes CRUD flow."""
        page = web_test_fixture.page

        # viewport and theme are now passed as parameters directly

        # Set up viewport and theme
        viewport_configs = {
            "mobile": ViewportSize(width=375, height=667),
            "desktop": ViewportSize(width=1280, height=720),
        }
        await page.set_viewport_size(viewport_configs[viewport])
        await page.evaluate(f"""
            localStorage.setItem('family-assistant-theme', '{theme}');
            window.dispatchEvent(new Event('storage'));
        """)
        await page.wait_for_timeout(500)

        helper = VisualDocumentationHelper(page, viewport, theme)

        # Step 1: Navigate to notes page
        await page.goto(f"{web_test_fixture.base_url}/notes")
        await page.wait_for_load_state("networkidle")
        await page.wait_for_timeout(1000)
        await helper.capture_step("notes", "list-view", "Notes list page initial view")

        # Step 2: Click create note
        create_button = page.locator('[data-testid="create-note-button"]').first
        if await create_button.is_visible():
            await create_button.click()
            await page.wait_for_timeout(1000)
            await helper.capture_step("notes", "create-form", "Create note form opened")

            # Step 3: Fill out form
            title_input = page.locator('[data-testid="note-title-input"]').first
            content_input = page.locator('[data-testid="note-content-input"]').first

            if await title_input.is_visible():
                await title_input.fill("Sample Note for Visual Documentation")
                await content_input.fill(
                    "This is a sample note created for visual documentation purposes."
                )
                await page.wait_for_timeout(500)
                await helper.capture_step(
                    "notes", "form-filled", "Create note form with content filled"
                )

                # Step 4: Save note
                save_button = page.locator('[data-testid="save-note-button"]').first
                if await save_button.is_visible():
                    await save_button.click()
                    await page.wait_for_timeout(2000)
                    await helper.capture_step(
                        "notes",
                        "note-created",
                        "Note successfully created and visible in list",
                    )


class TestNavigationVisualDocumentation:
    """Visual documentation for navigation and responsive behavior."""

    @pytest.mark.parametrize(
        "viewport,theme",
        [
            pytest.param("mobile", "light", marks=pytest.mark.mobile),
            pytest.param("desktop", "light", marks=pytest.mark.desktop),
        ],
    )
    async def test_navigation_responsive_behavior(
        self,
        web_test_fixture: WebTestFixture,
        request: pytest.FixtureRequest,
        viewport: str,
        theme: str,
    ) -> None:
        """Document navigation behavior across viewports."""
        page = web_test_fixture.page

        # viewport and theme are now passed as parameters directly

        # Set up viewport and theme
        viewport_configs = {
            "mobile": ViewportSize(width=375, height=667),
            "desktop": ViewportSize(width=1280, height=720),
        }
        await page.set_viewport_size(viewport_configs[viewport])
        await page.evaluate(f"""
            localStorage.setItem('family-assistant-theme', '{theme}');
            window.dispatchEvent(new Event('storage'));
        """)
        await page.wait_for_timeout(500)

        helper = VisualDocumentationHelper(page, viewport, theme)

        # Navigate to a page with full navigation
        await page.goto(f"{web_test_fixture.base_url}/notes")
        await page.wait_for_load_state("networkidle")
        await page.wait_for_timeout(1000)

        # Step 1: Default navigation state
        await helper.capture_step(
            "navigation",
            f"{viewport}-default",
            f"Default navigation layout on {viewport}",
        )

        if viewport == "desktop":
            # Desktop-specific navigation tests

            # Step 2: Data dropdown
            data_trigger = page.locator("button:has-text('Data')").first
            if await data_trigger.is_visible():
                await data_trigger.click()
                await page.wait_for_timeout(500)
                await helper.capture_step(
                    "navigation",
                    "desktop-data-dropdown",
                    "Desktop Data dropdown opened",
                )

                # Close dropdown by clicking elsewhere
                await page.click("body")
                await page.wait_for_timeout(500)

        elif viewport == "mobile":
            # Mobile-specific navigation tests

            # Look for mobile menu toggle
            mobile_menu = page.locator('[data-testid="mobile-menu-toggle"]').first
            if await mobile_menu.is_visible():
                await mobile_menu.click()
                await page.wait_for_timeout(1000)
                await helper.capture_step(
                    "navigation", "mobile-menu-open", "Mobile navigation menu opened"
                )


class TestDocumentsFlowVisualDocumentation:
    """Visual documentation for document management flows."""

    @pytest.mark.parametrize(
        "viewport,theme",
        [
            pytest.param("desktop", "light", marks=pytest.mark.desktop),
        ],
    )
    async def test_documents_upload_flow(
        self,
        web_test_fixture: WebTestFixture,
        request: pytest.FixtureRequest,
        viewport: str,
        theme: str,
    ) -> None:
        """Document the document upload interface."""
        page = web_test_fixture.page

        # viewport and theme are now passed as parameters directly

        # Set up viewport and theme
        await page.set_viewport_size(ViewportSize(width=1280, height=720))
        await page.evaluate(f"""
            localStorage.setItem('family-assistant-theme', '{theme}');
            window.dispatchEvent(new Event('storage'));
        """)
        await page.wait_for_timeout(500)

        helper = VisualDocumentationHelper(page, viewport, theme)

        # Step 1: Navigate to documents page
        await page.goto(f"{web_test_fixture.base_url}/documents")
        await page.wait_for_load_state("networkidle")
        await page.wait_for_timeout(1000)
        await helper.capture_step("documents", "list-view", "Documents list page")

        # Step 2: Upload interface
        upload_button = page.locator('[data-testid="upload-document-button"]').first
        if await upload_button.is_visible():
            await upload_button.click()
            await page.wait_for_timeout(1000)
            await helper.capture_step(
                "documents", "upload-interface", "Document upload interface opened"
            )


class TestVectorSearchVisualDocumentation:
    """Visual documentation for vector search functionality."""

    @pytest.mark.parametrize(
        "viewport,theme",
        [
            pytest.param("desktop", "light", marks=pytest.mark.desktop),
        ],
    )
    async def test_vector_search_flow(
        self,
        web_test_fixture: WebTestFixture,
        request: pytest.FixtureRequest,
        viewport: str,
        theme: str,
    ) -> None:
        """Document the vector search interface."""
        page = web_test_fixture.page

        # viewport and theme are now passed as parameters directly

        # Set up viewport and theme
        await page.set_viewport_size(ViewportSize(width=1280, height=720))
        await page.evaluate(f"""
            localStorage.setItem('family-assistant-theme', '{theme}');
            window.dispatchEvent(new Event('storage'));
        """)
        await page.wait_for_timeout(500)

        helper = VisualDocumentationHelper(page, viewport, theme)

        # Step 1: Navigate to vector search page
        await page.goto(f"{web_test_fixture.base_url}/vector-search")
        await page.wait_for_load_state("networkidle")
        await page.wait_for_timeout(1000)
        await helper.capture_step(
            "vector-search", "initial-view", "Vector search page initial view"
        )

        # Step 2: Search interface
        search_input = page.locator('[data-testid="search-input"]').first
        if await search_input.is_visible():
            await search_input.fill("sample search query")
            await page.wait_for_timeout(500)
            await helper.capture_step(
                "vector-search", "search-input", "Search query entered"
            )
