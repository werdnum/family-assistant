"""End-to-end tests for file upload functionality in the chat UI using Playwright."""

import tempfile

import anyio
import pytest
from PIL import Image

from family_assistant.llm.messages import (
    ImageUrlContentPart,
    TextContentPart,
    UserMessage,
)
from tests.functional.web.conftest import WebTestFixture
from tests.functional.web.pages.chat_page import ChatPage
from tests.mocks.mock_llm import LLMOutput, RuleBasedMockLLMClient


@pytest.mark.flaky(
    reruns=2,
    reason="Timing issue in SQLite CI environment - 404 on attachment retrieval or Failed to process attachment errors",
)
@pytest.mark.playwright
@pytest.mark.asyncio
async def test_image_upload_basic_functionality(
    web_test_with_console_check: WebTestFixture,
    mock_llm_client: RuleBasedMockLLMClient,
) -> None:
    """Test basic image upload and processing functionality."""
    page = web_test_with_console_check.page
    chat_page = ChatPage(page, web_test_with_console_check.base_url)

    def image_matcher(args: dict) -> bool:
        return any(
            isinstance(msg, UserMessage)
            and isinstance(msg.content, list)
            and any(isinstance(part, ImageUrlContentPart) for part in msg.content)
            for msg in args.get("messages", [])
        )

    mock_llm_client.rules = [
        (
            image_matcher,
            LLMOutput(
                content="I can see the image you uploaded! It appears to be a test image. How can I help you with it?"
            ),
        )
    ]

    mock_llm_client.default_response = LLMOutput(
        content="I received your message but no image was detected."
    )

    # Navigate to chat
    await chat_page.navigate_to_chat()

    # Create a test image file
    with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as temp_file:
        # Create a simple test image
        img = Image.new("RGB", (100, 100), color="red")
        img.save(temp_file.name, "PNG")
        temp_path = temp_file.name

    try:
        # Wait for attachment button to be visible
        attachment_button = page.locator('[data-testid="add-attachment-button"]').first
        await attachment_button.wait_for(state="visible", timeout=10000)

        # Set up file chooser handler before triggering the click
        async with page.expect_file_chooser() as fc_info:
            await attachment_button.click()

        file_chooser = await fc_info.value
        await file_chooser.set_files(temp_path)

        # Verify attachment is displayed
        attachment_preview = page.locator('[data-testid="attachment-preview"]').first
        await attachment_preview.wait_for(state="visible", timeout=5000)

        # Type a message
        await chat_page.send_message("What do you see in this image?")

        # Wait for assistant response
        await chat_page.wait_for_assistant_response()

        # Wait for streaming to complete to avoid SSE connection errors
        await chat_page.wait_for_streaming_complete()

        await chat_page.wait_for_message_content("I can see the image you uploaded!")
        assert "no image was detected" not in (
            await chat_page.get_last_assistant_message()
        )

    finally:
        # Clean up temp file
        await anyio.Path(temp_path).unlink(missing_ok=True)


@pytest.mark.playwright
@pytest.mark.asyncio
async def test_send_button_usable_after_attaching_a_file(
    web_test_fixture: WebTestFixture, mock_llm_client: RuleBasedMockLLMClient
) -> None:
    """The send button sends the turn after a file is attached.

    The composer disables sending while an attachment is still uploading, so an
    attachment that never finishes uploading leaves the send button greyed out
    with no way for the user to send at all. Other tests here send with Enter,
    which the composer allows regardless, so only clicking the button covers it.
    """
    page = web_test_fixture.page
    chat_page = ChatPage(page, web_test_fixture.base_url)

    mock_llm_client.default_response = LLMOutput(content="Got your file.")

    await chat_page.navigate_to_chat()

    with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as temp_file:
        Image.new("RGB", (50, 50), color="blue").save(temp_file.name, "PNG")
        temp_path = temp_file.name

    try:
        attachment_button = page.locator('[data-testid="add-attachment-button"]').first
        await attachment_button.wait_for(state="visible", timeout=10000)

        async with page.expect_file_chooser() as fc_info:
            await attachment_button.click()
        file_chooser = await fc_info.value
        await file_chooser.set_files(temp_path)

        chat_input = page.locator('[data-testid="chat-input"]')
        await chat_input.click()
        await chat_input.type("What do you see?")

        # Playwright waits for the button to be enabled, so the upload finishing
        # is what this asserts; it times out on an attachment stuck uploading.
        await page.locator('[data-testid="send-button"]').click(timeout=15000)

        await chat_page.wait_for_message_content("Got your file.", timeout=30000)

    finally:
        await anyio.Path(temp_path).unlink(missing_ok=True)


@pytest.mark.playwright
@pytest.mark.asyncio
async def test_file_of_an_unusual_type_can_be_attached_and_sent(
    web_test_fixture: WebTestFixture, mock_llm_client: RuleBasedMockLLMClient
) -> None:
    """No file is turned away for its type.

    A 3D model or an accounts export is not something a model reads directly,
    which is not a reason to refuse the upload: the assistant can still open it
    with its attachment tools. This covers the whole path -- the picker offers
    the file, the upload is accepted, and the turn sends.
    """
    page = web_test_fixture.page
    chat_page = ChatPage(page, web_test_fixture.base_url)

    mock_llm_client.default_response = LLMOutput(content="Got your model file.")

    await chat_page.navigate_to_chat()

    with tempfile.NamedTemporaryFile(suffix=".stl", delete=False) as temp_file:
        temp_file.write(b"solid bracket\nendsolid bracket\n")
        temp_path = temp_file.name

    try:
        attachment_button = page.locator('[data-testid="add-attachment-button"]').first
        await attachment_button.wait_for(state="visible", timeout=10000)

        async with page.expect_file_chooser() as fc_info:
            await attachment_button.click()
        file_chooser = await fc_info.value
        await file_chooser.set_files(temp_path)

        attachment_preview = page.locator('[data-testid="attachment-preview"]').first
        await attachment_preview.wait_for(state="visible", timeout=5000)

        error_message = page.locator('[data-testid="attachment-error-message"]')
        assert await error_message.count() == 0

        chat_input = page.locator('[data-testid="chat-input"]')
        await chat_input.click()
        await chat_input.type("What is this?")
        await page.locator('[data-testid="send-button"]').click(timeout=15000)

        await chat_page.wait_for_message_content("Got your model file.", timeout=30000)

    finally:
        await anyio.Path(temp_path).unlink(missing_ok=True)


@pytest.mark.playwright
@pytest.mark.asyncio
async def test_multiple_image_formats_support(
    web_test_fixture: WebTestFixture, mock_llm_client: RuleBasedMockLLMClient
) -> None:
    """Test support for different image formats (JPEG, PNG, GIF, WebP)."""
    page = web_test_fixture.page
    chat_page = ChatPage(page, web_test_fixture.base_url)

    # Navigate to chat
    await chat_page.navigate_to_chat()

    formats_to_test = [
        ("PNG", "test.png"),
        ("JPEG", "test.jpg"),
        ("GIF", "test.gif"),
        ("WebP", "test.webp"),
    ]

    for format_name, filename in formats_to_test:
        prompt = f"Can you see the {format_name} image?"
        expected_reply = f"I can see your {format_name} image!"

        def image_matcher(args: dict, expected_prompt: str = prompt) -> bool:
            return any(
                isinstance(msg, UserMessage)
                and isinstance(msg.content, list)
                and any(
                    isinstance(part, TextContentPart) and expected_prompt in part.text
                    for part in msg.content
                )
                and any(isinstance(part, ImageUrlContentPart) for part in msg.content)
                for msg in args.get("messages", [])
            )

        mock_llm_client.rules = [(image_matcher, LLMOutput(content=expected_reply))]
        mock_llm_client.default_response = LLMOutput(
            content=f"The {format_name} image was not received."
        )

        with tempfile.NamedTemporaryFile(
            suffix=f".{filename.split('.')[-1]}", delete=False
        ) as temp_file:
            # Create test image in the specified format
            img = Image.new("RGB", (50, 50), color="green")

            # Convert to format
            if format_name == "WebP":
                img.save(temp_file.name, "WebP")
            elif format_name == "GIF":
                img.save(temp_file.name, "GIF")
            else:
                img.save(temp_file.name, format_name)

            temp_path = temp_file.name

        try:
            # Upload the image
            attachment_button = page.locator(
                '[data-testid="add-attachment-button"]'
            ).first

            async with page.expect_file_chooser() as fc_info:
                await attachment_button.click()

            file_chooser = await fc_info.value
            await file_chooser.set_files(temp_path)

            # Wait for the attachment to appear before sending it.
            attachment_preview = page.locator(
                '[data-testid="attachment-preview"]'
            ).first
            await attachment_preview.wait_for(state="visible", timeout=5000)

            await chat_page.send_message(prompt)
            await chat_page.wait_for_message_content(expected_reply, timeout=30000)
            assert "not received" not in await chat_page.get_last_assistant_message()
            await chat_page.wait_for_streaming_complete()

        finally:
            # Clean up temp file
            await anyio.Path(temp_path).unlink(missing_ok=True)


@pytest.mark.playwright
@pytest.mark.asyncio
async def test_attachment_removal_functionality(
    web_test_fixture: WebTestFixture, mock_llm_client: RuleBasedMockLLMClient
) -> None:
    """Test removing attachments before sending.

    NOTE: This test is skipped because attachment removal requires custom implementation
    with useExternalStoreRuntime. The AttachmentPrimitive.Remove component expects
    the runtime to handle removal, but external store runtimes need to implement
    this manually.
    """
    page = web_test_fixture.page
    chat_page = ChatPage(page, web_test_fixture.base_url)

    # Navigate to chat
    await chat_page.navigate_to_chat()

    # Create a test image
    with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as temp_file:
        img = Image.new("RGB", (100, 100), color="yellow")
        img.save(temp_file.name, "PNG")
        temp_path = temp_file.name

    try:
        # Upload the image
        attachment_button = page.locator('[data-testid="add-attachment-button"]').first

        async with page.expect_file_chooser() as fc_info:
            await attachment_button.click()

        file_chooser = await fc_info.value
        await file_chooser.set_files(temp_path)

        # Wait for attachment to appear with data-testid
        attachment_preview = page.locator('[data-testid="attachment-preview"]').first
        await attachment_preview.wait_for(state="visible", timeout=5000)

        # Find and click remove button using data-testid
        remove_button = page.locator('[data-testid="remove-attachment-button"]').first
        await remove_button.wait_for(state="visible", timeout=5000)
        await remove_button.click()

        # Verify attachment is removed - check that it's no longer visible
        # The attachment might still be in DOM but hidden, so check visibility instead of detached
        await attachment_preview.wait_for(state="hidden", timeout=5000)

    finally:
        # Clean up temp file
        await anyio.Path(temp_path).unlink(missing_ok=True)


@pytest.mark.playwright
@pytest.mark.asyncio
async def test_image_preview_dialog(
    web_test_fixture: WebTestFixture, mock_llm_client: RuleBasedMockLLMClient
) -> None:
    """Test image preview dialog functionality."""
    page = web_test_fixture.page
    chat_page = ChatPage(page, web_test_fixture.base_url)

    # Navigate to chat
    await chat_page.navigate_to_chat()

    # Create a test image
    with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as temp_file:
        img = Image.new("RGB", (200, 200), color="purple")
        img.save(temp_file.name, "PNG")
        temp_path = temp_file.name

    try:
        # Upload the image
        attachment_button = page.locator('[data-testid="add-attachment-button"]').first

        async with page.expect_file_chooser() as fc_info:
            await attachment_button.click()

        file_chooser = await fc_info.value
        await file_chooser.set_files(temp_path)

        # Wait for attachment to appear
        attachment_preview = page.locator('[data-testid="attachment-preview"]').first
        await attachment_preview.wait_for(state="visible", timeout=5000)

        # Click on the attachment preview to open dialog
        await attachment_preview.click()

        # Wait for dialog to open
        dialog = page.locator('[role="dialog"]').first
        await dialog.wait_for(state="visible", timeout=5000)

        # Verify dialog contains image
        dialog_image = dialog.locator("img").first
        await dialog_image.wait_for(state="visible", timeout=5000)
        assert await dialog_image.is_visible()

        # Close dialog by clicking outside or pressing escape
        await page.keyboard.press("Escape")
        await dialog.wait_for(state="hidden", timeout=5000)

    finally:
        # Clean up temp file
        await anyio.Path(temp_path).unlink(missing_ok=True)


@pytest.mark.playwright
@pytest.mark.asyncio
async def test_api_request_includes_attachments(
    web_test_fixture: WebTestFixture, mock_llm_client: RuleBasedMockLLMClient
) -> None:
    """Test that API requests properly include attachment data."""
    page = web_test_fixture.page
    chat_page = ChatPage(page, web_test_fixture.base_url)

    # Configure mock LLM
    mock_llm_client.default_response = LLMOutput(content="Image received!")

    # Navigate to chat
    await chat_page.navigate_to_chat()

    # Create a test image
    with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as temp_file:
        img = Image.new("RGB", (100, 100), color="cyan")
        img.save(temp_file.name, "PNG")
        temp_path = temp_file.name

    try:
        # Upload and send image
        attachment_button = page.locator('[data-testid="add-attachment-button"]').first

        async with page.expect_file_chooser() as fc_info:
            await attachment_button.click()

        file_chooser = await fc_info.value
        await file_chooser.set_files(temp_path)

        # Wait for attachment and send message
        await page.wait_for_selector('[data-testid="attachment-preview"]', timeout=5000)
        async with page.expect_request(
            lambda request: (
                "/api/v1/chat/turns" in request.url and request.method == "POST"
            )
        ) as request_info:
            await chat_page.send_message("Analyze this image")

        body_data = (await request_info.value).post_data_json
        assert isinstance(body_data, dict)
        assert "attachments" in body_data
        assert body_data["attachments"] is not None
        assert len(body_data["attachments"]) > 0

        # Verify attachment structure
        attachment = body_data["attachments"][0]
        assert attachment["type"] == "image"
        assert "content" in attachment
        # With the new upload flow, content is a server URL, not base64
        assert attachment["content"].startswith("/api/attachments/")

    finally:
        # Clean up temp file
        await anyio.Path(temp_path).unlink(missing_ok=True)


@pytest.mark.flaky(reruns=2)
@pytest.mark.playwright
@pytest.mark.asyncio
async def test_attachment_display_in_message_history(
    web_test_fixture: WebTestFixture, mock_llm_client: RuleBasedMockLLMClient
) -> None:
    """Test that attachments are properly displayed in message history and persist across page refresh."""
    page = web_test_fixture.page
    chat_page = ChatPage(page, web_test_fixture.base_url)

    # Configure mock LLM
    mock_llm_client.default_response = LLMOutput(content="I see your image!")

    # Navigate to chat
    await chat_page.navigate_to_chat()

    # Create a test image
    with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as temp_file:
        img = Image.new("RGB", (100, 100), color="magenta")
        img.save(temp_file.name, "PNG")
        temp_path = temp_file.name

    try:
        # Upload and send image
        attachment_button = page.locator('[data-testid="add-attachment-button"]').first

        async with page.expect_file_chooser() as fc_info:
            await attachment_button.click()

        file_chooser = await fc_info.value
        await file_chooser.set_files(temp_path)

        # Use locator API for attachment preview
        attachment_preview = page.locator('[data-testid="attachment-preview"]').first
        await attachment_preview.wait_for(state="visible", timeout=5000)

        await chat_page.send_message("What's in this image?")

        # Wait for message to be sent and response received
        await chat_page.wait_for_assistant_response()

        # Check that user message displays the attachment
        user_message = page.locator('[data-testid="user-message"]').last
        await user_message.wait_for(state="visible", timeout=10000)

        # Look for attachment display in the user message BEFORE refresh
        image_in_message_before = user_message.locator("img").first
        await image_in_message_before.wait_for(state="visible", timeout=10000)

        # Get the image src URL before refresh
        image_src_before = await image_in_message_before.get_attribute("src")
        assert image_src_before, "Image should have a src attribute before refresh"
        assert "/api/attachments/" in image_src_before, "Image should use server URL"

        # **TEST PERSISTENCE: REFRESH THE PAGE**
        await page.reload()

        # Wait for the page to be fully loaded after reload
        await chat_page.wait_for_load(wait_for_app_ready=True)

        # Wait for assistant response to complete after reload (ensures conversation loaded)
        await chat_page.wait_for_assistant_response()

        # Use locator API (automatically retries) instead of wait_for_selector
        user_message_after = page.locator('[data-testid="user-message"]').last
        await user_message_after.wait_for(state="visible", timeout=10000)

        # Look for image element in the user message after refresh
        image_in_message_after = user_message_after.locator("img").first
        await image_in_message_after.wait_for(state="visible", timeout=10000)

        # Get the image src after refresh
        image_src_after = await image_in_message_after.get_attribute("src")
        assert image_src_after, "Image should have a src attribute after refresh"
        assert "/api/attachments/" in image_src_after, (
            "Image should still use server URL after refresh"
        )

        # The src should be the same (attachment persistence verification)
        assert image_src_before == image_src_after, (
            "Image src should be identical before and after refresh - this verifies attachment persistence"
        )

    finally:
        # Clean up temp file
        await anyio.Path(temp_path).unlink(missing_ok=True)
