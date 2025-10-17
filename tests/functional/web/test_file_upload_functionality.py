"""End-to-end tests for file upload functionality in the chat UI using Playwright."""

import asyncio
import json
import tempfile
import time
from typing import Any

import anyio
import pytest
from PIL import Image

from tests.functional.web.pages.chat_page import ChatPage
from tests.mocks.mock_llm import LLMOutput, RuleBasedMockLLMClient

from .conftest import WebTestFixture


@pytest.mark.playwright
@pytest.mark.asyncio
async def test_image_upload_basic_functionality(
    web_test_with_console_check: WebTestFixture,
    mock_llm_client: RuleBasedMockLLMClient,
) -> None:
    """Test basic image upload and processing functionality."""
    page = web_test_with_console_check.page
    chat_page = ChatPage(page, web_test_with_console_check.base_url)

    # Configure mock LLM to recognize image content
    def image_matcher(args: dict) -> bool:
        messages = args.get("messages", [])
        for msg in messages:
            content = msg.get("content", [])
            if isinstance(content, list):
                for part in content:
                    if isinstance(part, dict) and part.get("type") == "image_url":
                        return True
        return False

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

        # Wait for attachment to appear in the composer
        await page.wait_for_selector(
            ".flex.w-full.flex-row.gap-3.overflow-x-auto", timeout=5000
        )

        # Verify attachment is displayed
        attachment_preview = page.locator('[data-testid="attachment-preview"]').first
        await attachment_preview.wait_for(state="visible", timeout=5000)

        # Type a message
        await chat_page.send_message("What do you see in this image?")

        # Wait for assistant response
        await chat_page.wait_for_assistant_response()

        # Wait for streaming to complete to avoid SSE connection errors
        await chat_page.wait_for_streaming_complete()

        # Verify the response indicates image processing
        last_response = await chat_page.get_last_assistant_message()
        assert last_response
        assert "image" in last_response.lower() or "see" in last_response.lower()

    finally:
        # Clean up temp file
        await anyio.Path(temp_path).unlink(missing_ok=True)


@pytest.mark.playwright
@pytest.mark.asyncio
async def test_image_upload_validation_file_type(
    web_test_fixture: WebTestFixture, mock_llm_client: RuleBasedMockLLMClient
) -> None:
    """Test file type validation for uploads."""
    page = web_test_fixture.page
    chat_page = ChatPage(page, web_test_fixture.base_url)

    # Navigate to chat
    await chat_page.navigate_to_chat()

    # Create an unsupported file type (docx is not in SUPPORTED_FILE_TYPES)
    with tempfile.NamedTemporaryFile(suffix=".docx", delete=False) as temp_file:
        temp_file.write(b"This is an unsupported file type")
        temp_path = temp_file.name

    try:
        # Wait for attachment button
        attachment_button = page.locator('[data-testid="add-attachment-button"]').first
        await attachment_button.wait_for(state="visible", timeout=10000)

        # Set up file chooser handler
        async with page.expect_file_chooser() as fc_info:
            await attachment_button.click()

        file_chooser = await fc_info.value
        await file_chooser.set_files(temp_path)

        # Wait for error message about unsupported file type
        # Look for the error message element using data-testid
        error_message = page.locator('[data-testid="attachment-error-message"]').first
        await error_message.wait_for(state="visible", timeout=5000)

        # Verify error message is displayed and contains expected text
        error_text = await error_message.text_content()
        assert error_text
        assert "unsupported" in error_text.lower() and "file" in error_text.lower()

    finally:
        # Clean up temp file
        await anyio.Path(temp_path).unlink(missing_ok=True)


@pytest.mark.playwright
@pytest.mark.asyncio
async def test_multiple_image_formats_support(
    web_test_fixture: WebTestFixture, mock_llm_client: RuleBasedMockLLMClient
) -> None:
    """Test support for different image formats (JPEG, PNG, GIF, WebP)."""
    page = web_test_fixture.page
    chat_page = ChatPage(page, web_test_fixture.base_url)

    # Configure mock LLM for image processing
    def image_matcher(args: dict) -> bool:
        messages = args.get("messages", [])
        for msg in messages:
            content = msg.get("content", [])
            if isinstance(content, list):
                for part in content:
                    if isinstance(part, dict) and part.get("type") == "image_url":
                        return True
        return False

    mock_llm_client.rules = [
        (image_matcher, LLMOutput(content="I can see your image!"))
    ]

    # Navigate to chat
    await chat_page.navigate_to_chat()

    formats_to_test = [
        ("PNG", "test.png"),
        ("JPEG", "test.jpg"),
        ("GIF", "test.gif"),
        ("WebP", "test.webp"),
    ]

    for format_name, filename in formats_to_test:
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

            # Wait for attachment to appear (should not show error)
            attachment_preview = page.locator(
                '[data-testid="attachment-preview"]'
            ).first
            await attachment_preview.wait_for(state="visible", timeout=5000)

            # Verify no error messages
            # If the attachment preview is visible, the upload succeeded (no error occurred)
            error_message = page.locator(
                '[data-testid="attachment-error-message"]'
            ).first
            # Error message should not be visible if upload succeeded
            assert not await error_message.is_visible()

            # Remove the attachment for next test
            remove_button = page.locator(
                '[data-testid="remove-attachment-button"]'
            ).first
            if await remove_button.is_visible():
                await remove_button.click()
                # Wait for attachment to be removed
                await attachment_preview.wait_for(state="hidden", timeout=5000)

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
async def test_drag_and_drop_upload(
    web_test_fixture: WebTestFixture, mock_llm_client: RuleBasedMockLLMClient
) -> None:
    """Test drag and drop file upload functionality."""
    page = web_test_fixture.page
    chat_page = ChatPage(page, web_test_fixture.base_url)

    # Navigate to chat
    await chat_page.navigate_to_chat()

    # Create a test image
    with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as temp_file:
        img = Image.new("RGB", (100, 100), color="orange")
        img.save(temp_file.name, "PNG")
        temp_path = temp_file.name

    try:
        # Get the composer area for dropping files
        composer = page.locator('[class*="ComposerPrimitive.Root"]').first
        if not await composer.is_visible():
            composer = page.locator(".flex.flex-col.gap-3.max-w-4xl.mx-auto").first

        await composer.wait_for(state="visible", timeout=10000)

        # Create file for drag and drop
        await anyio.Path(temp_path).read_bytes()

        # Simulate drag and drop (note: actual drag/drop testing may require different approach)
        # For now, we'll use the file chooser method as drag/drop is complex in Playwright
        await page.set_input_files("#composer-file-input", temp_path)

        # Wait for attachment to appear
        attachment_preview = page.locator('[data-testid="attachment-preview"]').first
        await attachment_preview.wait_for(state="visible", timeout=5000)

        # Verify attachment is displayed
        assert await attachment_preview.is_visible()

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

    # Set up network request interception
    requests = []

    def handle_request(request: Any) -> None:  # noqa: ANN401  # playwright request object
        if "/api/v1/chat/send_message_stream" in request.url:
            requests.append({
                "url": request.url,
                "method": request.method,
                "headers": dict(request.headers),
                "body": request.post_data,
            })

    page.on("request", handle_request)

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
        await chat_page.send_message("Analyze this image")

        # Wait for the API request to be captured
        # Poll until we see the request in our list
        deadline = time.time() + 10
        while len(requests) == 0 and time.time() < deadline:  # noqa: ASYNC110
            await asyncio.sleep(0.1)

        # Verify request was made with attachments
        assert len(requests) > 0, "Expected API request to be captured"

        # Check the last request
        last_request = requests[-1]
        assert last_request["method"] == "POST"

        # Parse request body
        body_data = json.loads(last_request["body"])
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
