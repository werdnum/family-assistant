"""End-to-end tests for PDF upload functionality in the chat UI using Playwright."""

import tempfile

import anyio
import pytest

from tests.functional.web.conftest import WebTestFixture
from tests.functional.web.pages.chat_page import ChatPage
from tests.mocks.mock_llm import LLMOutput, RuleBasedMockLLMClient


@pytest.mark.playwright
@pytest.mark.asyncio
async def test_pdf_upload_functionality(
    web_test_fixture: WebTestFixture, mock_llm_client: RuleBasedMockLLMClient
) -> None:
    """Test basic PDF upload functionality."""
    page = web_test_fixture.page
    chat_page = ChatPage(page, web_test_fixture.base_url)

    # Configure mock LLM to recognize PDF content
    mock_llm_client.default_response = LLMOutput(
        content="I received your PDF document."
    )

    # Navigate to chat
    await chat_page.navigate_to_chat()

    # Create a test PDF file
    with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as temp_file:
        temp_file.write(
            b"%PDF-1.4\n1 0 obj\n<<\n/Type /Catalog\n/Pages 2 0 R\n>>\nendobj\n2 0 obj\n<<\n/Kids [3 0 R]\n/Count 1\n/Type /Pages\n>>\nendobj\n3 0 obj\n<<\n/Parent 2 0 R\n/MediaBox [0 0 612 792]\n/Resources <<\n/ProcSet [/PDF /Text /ImageB /ImageC /ImageI]\n>>\n/Type /Page\n>>\nendobj\ntrailer\n<<\n/Root 1 0 R\n>>\n%%EOF"
        )
        temp_path = temp_file.name

    try:
        # Wait for attachment button to be visible
        attachment_button = page.locator('[data-testid="add-attachment-button"]').first
        await attachment_button.wait_for(state="visible", timeout=10000)

        # Set up file chooser handler
        async with page.expect_file_chooser() as fc_info:
            await attachment_button.click()

        file_chooser = await fc_info.value
        await file_chooser.set_files(temp_path)

        # Wait for attachment to appear in the composer
        # Using a more specific selector for the attachment preview container
        await page.wait_for_selector(
            ".flex.w-full.flex-row.gap-3.overflow-x-auto", timeout=5000
        )

        # Verify attachment is displayed
        attachment_preview = page.locator('[data-testid="attachment-preview"]').first
        await attachment_preview.wait_for(state="visible", timeout=5000)

        # Verify no error message is displayed
        error_message = page.locator('[data-testid="attachment-error-message"]').first
        assert not await error_message.is_visible()

        # Type a message
        await chat_page.send_message("Please analyze this PDF.")

        # Wait for assistant response
        await chat_page.wait_for_assistant_response()

        # Verify the response
        last_response = await chat_page.get_last_assistant_message()
        assert last_response
        assert "PDF" in last_response or "received" in last_response

    finally:
        # Clean up temp file
        await anyio.Path(temp_path).unlink(missing_ok=True)
