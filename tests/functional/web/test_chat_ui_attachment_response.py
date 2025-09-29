"""Test for attachment response functionality in the web UI."""

import asyncio
import io
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pytest
from PIL import Image

from family_assistant.llm import LLMOutput, ToolCallFunction, ToolCallItem
from family_assistant.storage.base import attachment_metadata_table
from tests.functional.web.conftest import WebTestFixture
from tests.functional.web.pages.chat_page import ChatPage
from tests.mocks.mock_llm import RuleBasedMockLLMClient


def should_handle_attach_tool_follow_up(args: dict) -> bool:
    """Handle follow-up LLM call after attach_to_response tool execution.

    Returns True if the messages contain an attach_to_response tool result,
    indicating this is a follow-up call that should return empty content.
    """
    messages = args.get("messages", [])
    # Check if there's an attach_to_response tool result in the messages
    has_attach_tool_result = any(
        msg.get("role") == "tool" and msg.get("name") == "attach_to_response"
        for msg in messages
    )
    return has_attach_tool_result


async def create_test_attachment(
    web_test_fixture: WebTestFixture,
    attachment_id: str,
    conversation_id: str,
    base_url: str,
) -> None:
    """Create a test attachment in the database and filesystem."""

    # Create a simple test image
    img = Image.new("RGB", (100, 100), color="blue")
    buffer = io.BytesIO()
    img.save(buffer, format="PNG")
    buffer.seek(0)
    image_data = buffer.getvalue()

    # Get storage path from the assistant's attachment registry, not config
    # The config might have a different path than what the service actually uses
    attachment_registry = web_test_fixture.assistant.attachment_registry
    if attachment_registry is None:
        raise ValueError("AttachmentRegistry not available")
    storage_dir = str(attachment_registry.storage_path)
    # Test attachment storage dir: {storage_dir}
    hash_prefix = attachment_id[:2]  # First 2 characters as hash prefix
    hash_dir = f"{storage_dir}/{hash_prefix}"
    os.makedirs(hash_dir, exist_ok=True)
    # Created hash dir: {hash_dir}

    # Write the file to the filesystem using the same structure as AttachmentService
    # AttachmentService expects files to have extensions, so add .png for our image
    file_path = f"{hash_dir}/{attachment_id}.png"
    await asyncio.to_thread(Path(file_path).write_bytes, image_data)

    # Insert attachment metadata directly into database
    db_engine = web_test_fixture.assistant.database_engine
    if db_engine is None:
        raise ValueError("Database engine not available")

    async with db_engine.begin() as conn:
        await conn.execute(
            attachment_metadata_table.insert().values(
                attachment_id=attachment_id,
                source_type="user",
                source_id="test_user",
                mime_type="image/png",
                description="Test attachment for attach_to_response",
                size=len(image_data),
                content_url=f"{base_url}/api/attachments/{attachment_id}",
                storage_path=file_path,
                conversation_id=conversation_id,
                created_at=datetime(2024, 1, 1, 10, 0, 0, tzinfo=timezone.utc),
            )
        )


@pytest.mark.playwright
@pytest.mark.asyncio
async def test_attachment_response_flow(
    web_test_fixture: WebTestFixture, mock_llm_client: RuleBasedMockLLMClient
) -> None:
    """Test that the attach_to_response tool successfully displays attachments in the UI."""
    page = web_test_fixture.page
    chat_page = ChatPage(page, web_test_fixture.base_url)

    # Track console messages for debugging - fail on errors
    console_messages = []
    console_errors = []
    page_errors = []

    def handle_console(msg: Any) -> None:  # noqa: ANN401
        if hasattr(msg, "type") and hasattr(msg, "text"):
            message_text = msg.text
            console_messages.append(f"[{msg.type}] {message_text}")
            print(f"Console [{msg.type}]: {message_text}")

            # Track error messages separately
            if msg.type.lower() == "error":
                console_errors.append(message_text)

            # Force flush to make sure messages appear immediately
            sys.stdout.flush()

    page.on("console", handle_console)

    # Also capture any JavaScript errors
    def handle_page_error(error: Any) -> None:  # noqa: ANN401
        error_msg = str(error)
        page_errors.append(error_msg)
        print(f"Page error: {error_msg}")
        sys.stdout.flush()

    page.on("pageerror", handle_page_error)

    tool_call_id = "attach_tool_call"
    attachment_id = "3156da24-5b94-44ce-9dd1-014f538841c0"  # From screenshot
    llm_initial_response = "Of course, here is your photo"

    # Mock the LLM to respond with attach_to_response tool call - only on initial request
    def should_trigger_attach_tool(args: dict) -> bool:
        messages = args.get("messages", [])
        # Check if user message contains the trigger phrase
        has_trigger = any(
            "send this image back" in msg.get("content", "").lower()
            for msg in messages
            if msg.get("role") == "user"
        )
        # Check if there are already tool results (indicating this is a follow-up call)
        has_tool_results = any(msg.get("role") == "tool" for msg in messages)
        # Only trigger if we have the phrase but no tool results yet
        return has_trigger and not has_tool_results

    mock_llm_client.rules = [
        (
            should_trigger_attach_tool,
            LLMOutput(
                content=llm_initial_response,
                tool_calls=[
                    ToolCallItem(
                        id=tool_call_id,
                        type="function",
                        function=ToolCallFunction(
                            name="attach_to_response",
                            arguments=json.dumps({"attachment_ids": [attachment_id]}),
                        ),
                    )
                ],
            ),
        ),
        (
            should_handle_attach_tool_follow_up,
            LLMOutput(
                content=""  # Empty content to avoid appending additional text
            ),
        ),
    ]
    mock_llm_client.default_response = LLMOutput(
        content="Default response without tools"
    )

    await chat_page.navigate_to_chat()

    # Extract conversation ID from the page URL
    page_url = page.url
    conversation_id = (
        page_url.split("conversation_id=")[-1]
        if "conversation_id=" in page_url
        else "web_conv_test"
    )

    # Create a test attachment in the database
    await create_test_attachment(
        web_test_fixture, attachment_id, conversation_id, chat_page.base_url
    )

    await chat_page.send_message("send this image back to me")

    # Wait for assistant response to complete, then for attachment tool to be ready
    await chat_page.wait_for_assistant_response(timeout=30000)
    await chat_page.wait_for_attachments_ready(timeout=30000)

    # Verify that the attach_to_response tool call is shown with attachment display
    tool_call_element = page.locator('[data-ui="tool-call-content"]')
    tool_call_text = await tool_call_element.text_content()
    assert tool_call_text is not None and "Attachments" in tool_call_text

    # Verify that attachment preview is displayed
    attachment_previews = page.locator('[data-testid="attachment-preview"]')
    preview_count = await attachment_previews.count()
    assert preview_count == 1, f"Expected 1 attachment preview, found {preview_count}"

    # Check for images within attachment previews and verify they load
    images = page.locator('[data-testid="attachment-preview"] img')
    image_count = await images.count()

    if image_count > 0:
        print(f"Found {image_count} images in attachment previews")

        # Check that at least one image has loaded correctly
        for i in range(image_count):
            img = images.nth(i)
            src = await img.get_attribute("src")
            natural_width = await img.evaluate("(element) => element.naturalWidth")

            print(f"Image {i}: src={src}, naturalWidth={natural_width}")

            # Verify image source uses correct API path (not v1)
            if src and "/api/" in src:
                assert "/api/attachments/" in src, (
                    f"Image src should use /api/attachments/ not /api/v1/attachments/. Got: {src}"
                )

            # At least one image should have loaded (naturalWidth > 0)
            if natural_width and natural_width > 0:
                print(f"Image {i} loaded successfully with width {natural_width}")
                break
        else:
            # No images loaded successfully
            raise AssertionError(
                f"No images loaded successfully. Found {image_count} images but none had naturalWidth > 0"
            )

    # CRITICAL: Fail test if any console errors occurred
    if console_errors:
        print(f"Console errors detected: {console_errors}")
        raise AssertionError(f"Test failed due to console errors: {console_errors}")

    if page_errors:
        print(f"Page errors detected: {page_errors}")
        raise AssertionError(f"Test failed due to page errors: {page_errors}")

    print("Attachment successfully displayed within tool UI with no console errors")


@pytest.mark.playwright
@pytest.mark.asyncio
async def test_attachment_response_with_multiple_attachments(
    web_test_fixture: WebTestFixture, mock_llm_client: RuleBasedMockLLMClient
) -> None:
    """Test that multiple attachments from attach_to_response are displayed correctly."""
    page = web_test_fixture.page
    chat_page = ChatPage(page, web_test_fixture.base_url)

    # Track console messages for debugging - fail on errors
    console_messages = []
    console_errors = []
    page_errors = []

    def handle_console(msg: Any) -> None:  # noqa: ANN401
        if hasattr(msg, "type") and hasattr(msg, "text"):
            message_text = msg.text
            console_messages.append(f"[{msg.type}] {message_text}")
            print(f"Console [{msg.type}]: {message_text}")

            # Track error messages separately
            if msg.type.lower() == "error":
                console_errors.append(message_text)

            # Force flush to make sure messages appear immediately
            sys.stdout.flush()

    page.on("console", handle_console)

    # Also capture any JavaScript errors
    def handle_page_error(error: Any) -> None:  # noqa: ANN401
        error_msg = str(error)
        page_errors.append(error_msg)
        print(f"Page error: {error_msg}")
        sys.stdout.flush()

    page.on("pageerror", handle_page_error)

    tool_call_id = "multi_attach_tool_call"
    attachment_ids = [
        "3156da24-5b94-44ce-9dd1-014f538841c0",
        "4267eb35-6ca5-55df-ae12-125f649952d1",
    ]
    llm_response = "Here are both images you requested"

    # Mock the LLM to respond with multiple attachments - only on initial request
    def should_trigger_multi_attach_tool(args: dict) -> bool:
        messages = args.get("messages", [])
        # Check if user message contains the trigger phrase
        has_trigger = any(
            "send me both images" in msg.get("content", "").lower()
            for msg in messages
            if msg.get("role") == "user"
        )
        # Check if there are already tool results (indicating this is a follow-up call)
        has_tool_results = any(msg.get("role") == "tool" for msg in messages)
        # Only trigger if we have the phrase but no tool results yet
        return has_trigger and not has_tool_results

    mock_llm_client.rules = [
        (
            should_trigger_multi_attach_tool,
            LLMOutput(
                content=llm_response,
                tool_calls=[
                    ToolCallItem(
                        id=tool_call_id,
                        type="function",
                        function=ToolCallFunction(
                            name="attach_to_response",
                            arguments=json.dumps({"attachment_ids": attachment_ids}),
                        ),
                    )
                ],
            ),
        ),
        (
            should_handle_attach_tool_follow_up,
            LLMOutput(
                content=""  # Empty content to avoid appending additional text
            ),
        ),
    ]
    mock_llm_client.default_response = LLMOutput(content="Default response")

    await chat_page.navigate_to_chat()

    # Extract conversation ID from the page URL
    page_url = page.url
    conversation_id = (
        page_url.split("conversation_id=")[-1]
        if "conversation_id=" in page_url
        else "web_conv_test"
    )

    # Create test attachments in the database
    for attachment_id in attachment_ids:
        await create_test_attachment(
            web_test_fixture, attachment_id, conversation_id, chat_page.base_url
        )

    await chat_page.send_message("send me both images")

    # Wait for assistant response to complete, then for attachment tool to be ready
    await chat_page.wait_for_assistant_response(timeout=30000)
    await chat_page.wait_for_attachments_ready(timeout=30000)

    # Verify that the attach_to_response tool call is shown with attachment display
    tool_call_element = page.locator('[data-ui="tool-call-content"]')
    tool_call_text = await tool_call_element.text_content()
    assert tool_call_text is not None and "Attachments" in tool_call_text

    # Verify attachment previews are now available
    try:
        # The wait_for_attachments_ready should have already ensured these exist
        attachment_previews = page.locator('[data-testid="attachment-preview"]')
        preview_count = await attachment_previews.count()
        assert preview_count > 0, (
            "Attachment previews should be available after wait_for_attachments_ready"
        )
    except Exception:
        # If attachment previews not found, get console errors to help debug
        print("Console messages captured by Playwright:")
        for message in console_messages:
            print(f"  {message}")

        # Also try to get any runtime console logs
        console_messages_js = await page.evaluate("() => window.consoleMessages || []")
        if console_messages_js:
            print("Console messages from window.consoleMessages:")
            for message in console_messages_js:
                print(f"  {message}")

        # Also get the current page HTML for debugging
        html = await page.content()
        print("Current page HTML (first 2000 chars):")
        print(html[:2000])
        raise

    # Get attachment preview count for logging - these should already be available
    attachment_previews = page.locator('[data-testid="attachment-preview"]')
    preview_count = await attachment_previews.count()
    print(f"Found {preview_count} attachment previews")

    # Check for images within attachment previews and verify they load
    images = page.locator('[data-testid="attachment-preview"] img')
    image_count = await images.count()

    if image_count > 0:
        print(f"Found {image_count} images in attachment previews")

        # Check that at least one image has loaded correctly
        for i in range(image_count):
            img = images.nth(i)
            src = await img.get_attribute("src")
            natural_width = await img.evaluate("(element) => element.naturalWidth")

            print(f"Image {i}: src={src}, naturalWidth={natural_width}")

            # Verify image source uses correct API path (not v1)
            if src and "/api/" in src:
                assert "/api/attachments/" in src, (
                    f"Image src should use /api/attachments/ not /api/v1/attachments/. Got: {src}"
                )

            # At least one image should have loaded (naturalWidth > 0)
            if natural_width and natural_width > 0:
                print(f"Image {i} loaded successfully with width {natural_width}")
                break
        else:
            # No images loaded successfully
            raise AssertionError(
                f"No images loaded successfully. Found {image_count} images but none had naturalWidth > 0"
            )

    # CRITICAL: Fail test if any console errors occurred
    if console_errors:
        print(f"Console errors detected: {console_errors}")
        raise AssertionError(f"Test failed due to console errors: {console_errors}")

    if page_errors:
        print(f"Page errors detected: {page_errors}")
        raise AssertionError(f"Test failed due to page errors: {page_errors}")

    print("Multiple attachment test completed successfully with no console errors")


@pytest.mark.playwright
@pytest.mark.asyncio
async def test_attachment_response_error_handling(
    web_test_fixture: WebTestFixture, mock_llm_client: RuleBasedMockLLMClient
) -> None:
    """Test error handling when attach_to_response fails."""
    page = web_test_fixture.page
    chat_page = ChatPage(page, web_test_fixture.base_url)

    tool_call_id = "error_attach_tool_call"
    invalid_attachment_id = "invalid-attachment-id"
    llm_response = "Let me try to send that image"

    # Mock the LLM to use an invalid attachment ID - only on initial request
    def should_trigger_error_attach_tool(args: dict) -> bool:
        messages = args.get("messages", [])
        # Check if user message contains the trigger phrase
        has_trigger = any(
            "send invalid image" in msg.get("content", "").lower()
            for msg in messages
            if msg.get("role") == "user"
        )
        # Check if there are already tool results (indicating this is a follow-up call)
        has_tool_results = any(msg.get("role") == "tool" for msg in messages)
        # Only trigger if we have the phrase but no tool results yet
        return has_trigger and not has_tool_results

    mock_llm_client.rules = [
        (
            should_trigger_error_attach_tool,
            LLMOutput(
                content=llm_response,
                tool_calls=[
                    ToolCallItem(
                        id=tool_call_id,
                        type="function",
                        function=ToolCallFunction(
                            name="attach_to_response",
                            arguments=json.dumps({
                                "attachment_ids": [invalid_attachment_id]
                            }),
                        ),
                    )
                ],
            ),
        ),
        (
            should_handle_attach_tool_follow_up,
            LLMOutput(
                content=""  # Empty content to avoid appending additional text
            ),
        ),
    ]

    await chat_page.navigate_to_chat()
    await chat_page.send_message("send invalid image")

    # Wait for assistant response to complete, then for attachment tool to be ready (or error)
    await chat_page.wait_for_assistant_response(timeout=30000)

    # Use a more lenient wait for error cases - the tool might not render full content
    try:
        await chat_page.wait_for_attachments_ready(timeout=30000)
        tool_call_rendered = True
    except AssertionError:
        # If the tool doesn't render at all, that's also a valid error state
        tool_call_rendered = False
        print("Tool call failed to render - this is acceptable for error cases")

    if tool_call_rendered:
        # If tool rendered, verify it shows error state appropriately
        tool_call_element = page.locator('[data-ui="tool-call-content"]')
        tool_call_count = await tool_call_element.count()

        if tool_call_count > 0:
            tool_call_text = await tool_call_element.text_content()
            print(
                f"Tool call rendered with text: {tool_call_text[:100] if tool_call_text else 'None'}"
            )

            # Check for error indication in the tool UI
            tool_result_element = page.locator('[data-testid="tool-result"]')
            tool_result_count = await tool_result_element.count()

            error_found = False
            if tool_result_count > 0:
                # Check if tool result indicates an error
                tool_result_text = await tool_result_element.text_content()
                error_found = tool_result_text is not None and (
                    "error" in tool_result_text.lower()
                    or "failed" in tool_result_text.lower()
                    or "no valid attachments found" in tool_result_text.lower()
                )

            # Verify that no valid attachment preview is displayed for the error case
            attachment_previews = page.locator('[data-testid="attachment-preview"]')
            preview_count = await attachment_previews.count()

            # Check if any attachment previews show error states
            failed_to_load_found = False
            if preview_count > 0:
                for i in range(preview_count):
                    preview = attachment_previews.nth(i)
                    preview_text = await preview.text_content()
                    if preview_text and "failed to load" in preview_text.lower():
                        failed_to_load_found = True
                        break

            # For error cases, we should have either:
            # 1. Error message in tool result OR
            # 2. No attachment previews OR
            # 3. "Failed to load" previews OR
            # 4. No tool result at all (which indicates the tool failed)
            assert (
                error_found
                or preview_count == 0
                or failed_to_load_found
                or tool_result_count == 0
            ), (
                f"Expected error handling: error_found={error_found}, "
                f"preview_count={preview_count}, failed_to_load_found={failed_to_load_found}, "
                f"tool_result_count={tool_result_count}"
            )

    print("Error handling test completed - invalid attachment handled appropriately")


@pytest.mark.playwright
async def test_tool_attachment_persistence_after_page_reload(
    web_test_fixture: WebTestFixture, mock_llm_client: RuleBasedMockLLMClient
) -> None:
    """
    Test that tool-generated attachments persist after page reload.

    This is the core test for the bug fix: tool attachments were being stored
    but not registered in the database, causing them to 404 after page reload.
    """
    page = web_test_fixture.page
    chat_page = ChatPage(page, web_test_fixture.base_url)

    # Create a test attachment to simulate a tool-generated attachment
    attachment_id = "3156da24-5b94-44ce-9dd1-014f538841c0"  # Valid UUID

    # Mock the LLM to respond with attach_to_response tool call
    def should_trigger_attach_tool(args: dict[str, list[dict[str, str]]]) -> bool:
        messages = args.get("messages", [])
        # Check if user message contains the trigger phrase
        has_trigger = any(
            "show attachment" in msg.get("content", "").lower()
            for msg in messages
            if msg.get("role") == "user"
        )
        # Check if there are already tool results (indicating this is a follow-up call)
        has_tool_results = any(msg.get("role") == "tool" for msg in messages)
        # Only trigger if we have the phrase but no tool results yet
        return has_trigger and not has_tool_results

    tool_call_id = "test-tool-persistence-call-123"
    llm_response = "I'll show you the attachment now."

    mock_llm_client.rules = [
        (
            should_trigger_attach_tool,
            LLMOutput(
                content=llm_response,
                tool_calls=[
                    ToolCallItem(
                        id=tool_call_id,
                        type="function",
                        function=ToolCallFunction(
                            name="attach_to_response",
                            arguments=json.dumps({"attachment_ids": [attachment_id]}),
                        ),
                    )
                ],
            ),
        ),
        (
            should_handle_attach_tool_follow_up,
            LLMOutput(
                content=""  # Empty content to avoid appending additional text
            ),
        ),
    ]

    # Navigate and trigger the tool attachment
    await chat_page.navigate_to_chat()

    # Extract conversation ID from the page URL
    page_url = page.url
    conversation_id = (
        page_url.split("conversation_id=")[-1]
        if "conversation_id=" in page_url
        else "web_conv_test"
    )

    # Create a test attachment in the database with the correct conversation ID
    await create_test_attachment(
        web_test_fixture, attachment_id, conversation_id, web_test_fixture.base_url
    )

    await chat_page.send_message("show attachment")

    # Wait for tool execution and attachment display
    await chat_page.wait_for_assistant_response(timeout=30000)
    await chat_page.wait_for_attachments_ready(timeout=30000)
    print("[DEBUG] Attachment tool ready with content")

    # Verify attachment is displayed initially
    attachment_preview = page.locator('[data-testid="attachment-preview"]').first
    await attachment_preview.wait_for(state="visible", timeout=5000)
    print("[DEBUG] Attachment preview is visible")

    # Get the attachment URL from the img element
    img_element = attachment_preview.locator("img").first
    await img_element.wait_for(state="visible", timeout=5000)
    attachment_url = await img_element.get_attribute("src")
    assert attachment_url is not None, "Attachment should have a valid URL"

    # Convert relative URL to absolute if needed for API requests
    if attachment_url.startswith("/"):
        attachment_url = f"{web_test_fixture.base_url}{attachment_url}"

    # Verify the attachment actually loads (not a 404)
    img_response = await page.request.get(attachment_url)
    assert img_response.status == 200, (
        f"Attachment should be accessible initially, got {img_response.status}"
    )

    # Ensure the conversation (including tool call) is persisted before reload
    await chat_page.wait_for_conversation_saved(timeout=30000)

    # CRITICAL TEST: Reload the page
    # Reloading page to test persistence...
    await page.reload()
    await page.wait_for_load_state("networkidle")

    # Wait for the chat history to reload and attachment tool to be ready
    await chat_page.wait_for_assistant_response(timeout=30000)
    await chat_page.wait_for_attachments_ready(timeout=30000)

    # THE BUG FIX TEST: Verify attachment is still accessible after reload
    try:
        attachment_preview_after_reload = page.locator(
            '[data-testid="attachment-preview"]'
        ).first
        await attachment_preview_after_reload.wait_for(state="visible", timeout=10000)

        # Get the attachment URL after reload
        img_element_after_reload = attachment_preview_after_reload.locator("img").first
        await img_element_after_reload.wait_for(state="visible", timeout=10000)
        attachment_url_after_reload = await img_element_after_reload.get_attribute(
            "src"
        )

        # CORE ASSERTION: The attachment should still be accessible (not 404)
        if attachment_url_after_reload:
            # Convert relative URL to absolute if needed for API requests
            if attachment_url_after_reload.startswith("/"):
                attachment_url_after_reload = (
                    f"{web_test_fixture.base_url}{attachment_url_after_reload}"
                )

            img_response_after_reload = await page.request.get(
                attachment_url_after_reload
            )
            assert img_response_after_reload.status == 200, (
                f"TOOL ATTACHMENT PERSISTENCE BUG! "
                f"Got {img_response_after_reload.status} when accessing {attachment_url_after_reload} after page reload. "
                f"This indicates the attachment was not properly registered in the database."
            )

        print(
            "[SUCCESS] Tool attachment persisted after page reload - bug fix verified!"
        )

    except Exception as e:
        # If we can't find the attachment after reload, that's the bug!
        pytest.fail(
            f"TOOL ATTACHMENT PERSISTENCE BUG! "
            f"Tool attachment disappeared after page reload: {e}. "
            f"This indicates tool attachments are stored as files but not registered in the database."
        )
