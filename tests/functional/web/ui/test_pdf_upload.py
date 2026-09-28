"""End-to-end tests for PDF upload functionality in the chat UI using Playwright."""

import tempfile
from pathlib import Path

import anyio
import pytest

from family_assistant.llm.messages import TextContentPart, UserMessage
from tests.functional.web.conftest import WebTestFixture
from tests.functional.web.pages.chat_page import ChatPage
from tests.mocks.mock_llm import LLMOutput, MatcherArgs, RuleBasedMockLLMClient


@pytest.mark.playwright
@pytest.mark.asyncio
async def test_pdf_upload_functionality(
    web_test_fixture: WebTestFixture, mock_llm_client: RuleBasedMockLLMClient
) -> None:
    """Test basic PDF upload functionality."""
    page = web_test_fixture.page
    chat_page = ChatPage(page, web_test_fixture.base_url)

    # Navigate to chat
    await chat_page.navigate_to_chat()

    # Create a test PDF file
    with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as temp_file:
        temp_file.write(
            b"%PDF-1.4\n1 0 obj\n<<\n/Type /Catalog\n/Pages 2 0 R\n>>\nendobj\n2 0 obj\n<<\n/Kids [3 0 R]\n/Count 1\n/Type /Pages\n>>\nendobj\n3 0 obj\n<<\n/Parent 2 0 R\n/MediaBox [0 0 612 792]\n/Resources <<\n/ProcSet [/PDF /Text /ImageB /ImageC /ImageI]\n>>\n/Type /Page\n>>\nendobj\ntrailer\n<<\n/Root 1 0 R\n>>\n%%EOF"
        )
        temp_path = temp_file.name

    def has_uploaded_pdf(args: MatcherArgs) -> bool:
        for message in args["messages"]:
            if not isinstance(message, UserMessage):
                continue
            content = message.content
            text = (
                content
                if isinstance(content, str)
                else "\n".join(
                    part.text for part in content if isinstance(part, TextContentPart)
                )
            )
            if Path(temp_path).name in text and "application/pdf" in text:
                return True
        return False

    mock_llm_client.rules = [
        (has_uploaded_pdf, LLMOutput(content="PDF attachment received."))
    ]
    mock_llm_client.default_response = LLMOutput(content="PDF attachment missing.")

    try:
        # Wait for attachment button to be visible
        attachment_button = page.locator('[data-testid="add-attachment-button"]').first
        await attachment_button.wait_for(state="visible", timeout=10000)

        # Set up file chooser handler
        async with page.expect_file_chooser() as fc_info:
            await attachment_button.click()

        file_chooser = await fc_info.value
        await file_chooser.set_files(temp_path)

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
        assert last_response == "PDF attachment received."

    finally:
        # Clean up temp file
        await anyio.Path(temp_path).unlink(missing_ok=True)
