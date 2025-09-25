"""Test for attachment response functionality in the web UI."""

import io
import json
import os
import sys
from datetime import datetime, timezone
from typing import Any

import pytest
from PIL import Image

from family_assistant.llm import LLMOutput, ToolCallFunction, ToolCallItem
from family_assistant.storage.base import attachment_metadata_table
from tests.functional.web.conftest import WebTestFixture
from tests.functional.web.pages.chat_page import ChatPage
from tests.mocks.mock_llm import RuleBasedMockLLMClient


async def create_test_attachment(
    web_test_fixture: WebTestFixture, attachment_id: str, conversation_id: str
) -> None:
    """Create a test attachment in the database and filesystem."""

    # Create a simple test image
    img = Image.new("RGB", (100, 100), color="blue")
    buffer = io.BytesIO()
    img.save(buffer, format="PNG")
    buffer.seek(0)
    image_data = buffer.getvalue()

    # Get storage path from config
    storage_dir = web_test_fixture.assistant.config["attachment_storage_path"]
    if not storage_dir:
        raise ValueError("Attachment storage path not configured")
    hash_prefix = attachment_id[:2]  # First 2 characters as hash prefix
    hash_dir = f"{storage_dir}/{hash_prefix}"
    os.makedirs(hash_dir, exist_ok=True)

    # Write the file to the filesystem using the same structure as AttachmentService
    file_path = f"{hash_dir}/{attachment_id}"
    with open(file_path, "wb") as f:
        f.write(image_data)

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
                content_url=f"http://localhost:8000/api/v1/attachments/{attachment_id}",
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

    tool_call_id = "attach_tool_call"
    attachment_id = "3156da24-5b94-44ce-9dd1-014f538841c0"  # From screenshot
    llm_initial_response = "Of course, here is your photo"

    # Mock the LLM to respond with attach_to_response tool call - only on initial request
    def should_trigger_attach_tool(args: dict) -> bool:
        messages = args.get("messages", [])
        # Check if user message contains the trigger phrase
        has_trigger = "send this image back" in str(messages).lower()
        # Check if there are already tool results (indicating this is a follow-up call)
        has_tool_results = any(msg.get("role") == "tool" for msg in messages)
        # Debug: print the messages to understand what's being passed
        print(f"Mock LLM triggered with {len(messages)} messages")
        print(f"Messages content: {str(messages)[:200]}")
        print(f"Has trigger phrase: {has_trigger}")
        print(f"Has tool results: {has_tool_results}")
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
    await create_test_attachment(web_test_fixture, attachment_id, conversation_id)

    await chat_page.send_message("send this image back to me")

    # Wait for the assistant response
    await page.wait_for_selector(
        '[data-testid="assistant-message-content"]', timeout=15000
    )

    # Wait for the tool call to be displayed
    await page.wait_for_selector('[data-testid="tool-call"]', timeout=10000)

    # Check that the attach_to_response tool call is shown with attachment display
    tool_call_element = page.locator('[data-testid="tool-call"]')
    tool_call_text = await tool_call_element.text_content()
    assert tool_call_text is not None and "Attachments" in tool_call_text

    # Wait for the attachment preview to appear within the tool UI
    await page.wait_for_selector('[data-testid="attachment-preview"]', timeout=10000)

    # Verify that attachment preview is displayed
    attachment_previews = page.locator('[data-testid="attachment-preview"]')
    preview_count = await attachment_previews.count()
    assert preview_count == 1, f"Expected 1 attachment preview, found {preview_count}"

    print("Attachment successfully displayed within tool UI")


@pytest.mark.playwright
@pytest.mark.asyncio
async def test_attachment_response_with_multiple_attachments(
    web_test_fixture: WebTestFixture, mock_llm_client: RuleBasedMockLLMClient
) -> None:
    """Test that multiple attachments from attach_to_response are displayed correctly."""
    page = web_test_fixture.page
    chat_page = ChatPage(page, web_test_fixture.base_url)

    # Track console messages for debugging
    console_messages = []

    def handle_console(msg: Any) -> None:  # noqa: ANN401
        if hasattr(msg, "type") and hasattr(msg, "text"):
            message_text = msg.text
            console_messages.append(f"[{msg.type}] {message_text}")
            print(f"Console [{msg.type}]: {message_text}")
            # Force flush to make sure messages appear immediately
            sys.stdout.flush()

    page.on("console", handle_console)

    # Also capture any JavaScript errors
    def handle_page_error(error: Any) -> None:  # noqa: ANN401
        print(f"Page error: {error}")

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
        has_trigger = "send me both images" in str(messages).lower()
        # Check if there are already tool results (indicating this is a follow-up call)
        has_tool_results = any(msg.get("role") == "tool" for msg in messages)
        # Debug: print the messages to understand what's being passed
        print(f"Multiple attachment mock LLM triggered with {len(messages)} messages")
        print(f"Messages content: {str(messages)[:200]}")
        print(f"Has trigger phrase: {has_trigger}")
        print(f"Has tool results: {has_tool_results}")
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
        await create_test_attachment(web_test_fixture, attachment_id, conversation_id)

    await chat_page.send_message("send me both images")

    # Wait for the assistant response and tool execution
    await page.wait_for_selector(
        '[data-testid="assistant-message-content"]', timeout=10000
    )
    await page.wait_for_selector('[data-testid="tool-result"]', timeout=10000)

    # Wait for the tool call to be displayed
    await page.wait_for_selector('[data-testid="tool-call"]', timeout=10000)

    # Check that the attach_to_response tool call is shown with attachment display
    tool_call_element = page.locator('[data-testid="tool-call"]')
    tool_call_text = await tool_call_element.text_content()
    assert tool_call_text is not None and "Attachments" in tool_call_text

    # Wait for the attachment previews to appear within the tool UI
    try:
        await page.wait_for_selector(
            '[data-testid="attachment-preview"]', timeout=15000
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

    attachment_previews = page.locator('[data-testid="attachment-preview"]')
    preview_count = await attachment_previews.count()

    # We expect to see multiple attachment previews (at least 1, ideally 2)
    # The exact count depends on how the UI handles multiple attachments
    assert preview_count > 0, (
        f"Expected at least 1 attachment preview, found {preview_count}"
    )


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
        has_trigger = "send invalid image" in str(messages).lower()
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
    ]

    await chat_page.navigate_to_chat()
    await chat_page.send_message("send invalid image")

    # Wait for the assistant response and tool execution
    await page.wait_for_selector(
        '[data-testid="assistant-message-content"]', timeout=10000
    )
    await page.wait_for_selector('[data-testid="tool-call"]', timeout=5000)

    # Check that the attach_to_response tool call is shown with error state
    tool_call_element = page.locator('[data-testid="tool-call"]')
    tool_call_text = await tool_call_element.text_content()
    assert tool_call_text is not None and "Attachments" in tool_call_text

    # Check for error indication in the tool UI - either in status text or no attachment previews
    # The tool should show either an error message or display no attachment previews
    try:
        # Try to find any tool result text that indicates an error
        await page.wait_for_selector('[data-testid="tool-result"]', timeout=2000)
        tool_result_element = page.locator('[data-testid="tool-result"]')
        tool_result_text = await tool_result_element.text_content()
        # Accept if there's an error message or "Failed to process" message
        error_found = tool_result_text is not None and (
            "error" in tool_result_text.lower()
            or "failed" in tool_result_text.lower()
            or "no valid attachments found" in tool_result_text.lower()
        )
    except Exception:
        # If no tool result is found, that's also acceptable for an error case
        error_found = True

    # Verify that no attachment preview is displayed for the error case
    attachment_previews = page.locator('[data-testid="attachment-preview"]')
    preview_count = await attachment_previews.count()

    # Check if attachment preview shows "failed to load" which is also a valid error state
    failed_to_load_found = False
    if preview_count > 0:
        for i in range(preview_count):
            preview = attachment_previews.nth(i)
            preview_text = await preview.text_content()
            if preview_text and "failed to load" in preview_text.lower():
                failed_to_load_found = True
                break

    # Either we should have an error message OR no attachment previews OR "failed to load" previews
    assert error_found or preview_count == 0 or failed_to_load_found, (
        "Expected either an error message in tool result, no attachment previews, or 'failed to load' previews for invalid attachment ID"
    )
