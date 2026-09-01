"""Test for attachment response functionality in the web UI."""

import asyncio
import io
import json
import os
import sys
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, TypedDict

import pytest
from PIL import Image

from family_assistant.llm import LLMOutput, ToolCallFunction, ToolCallItem
from family_assistant.storage.base import attachment_metadata_table
from tests.functional.web.conftest import WebTestFixture
from tests.functional.web.pages.chat_page import ChatPage
from tests.helpers import wait_for_condition
from tests.mocks.mock_llm import RuleBasedMockLLMClient


class ConversationHistoryMessage(TypedDict, total=False):
    """Subset of conversation message fields used by this test."""

    role: str
    content: object


def should_handle_attach_tool_follow_up(args: dict) -> bool:
    """Handle follow-up LLM call after attach_to_response tool execution.

    Returns True if the messages contain an attach_to_response tool result,
    indicating this is a follow-up call that should return empty content.
    """
    messages = args.get("messages", [])
    # Check if there's an attach_to_response tool result in the messages
    has_attach_tool_result = any(
        msg.role == "tool" and msg.name == "attach_to_response" for msg in messages
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

    # ast-grep-ignore: no-raw-transaction-management - test fixture setup, outside the application transaction model
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
                created_at=datetime(2024, 1, 1, 10, 0, 0, tzinfo=UTC),
            )
        )


# The attachment-preview render can exceed the 30s locator wait under CI load;
# rerun on failure like the sibling Playwright UI tests rather than flaking the
# whole suite. Tracked as a frontend timing flake to fix at the source.
@pytest.mark.flaky(reruns=3, reruns_delay=2)
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
    attachment_id = str(uuid.uuid4())
    llm_initial_response = "Of course, here is your photo"

    # Mock the LLM to respond with attach_to_response tool call - only on initial request
    def should_trigger_attach_tool(args: dict) -> bool:
        messages = args.get("messages", [])
        # Check if user message contains the trigger phrase
        has_trigger = any(
            "send this image back" in (msg.content or "").lower()
            for msg in messages
            if msg.role == "user"
        )
        # Check if there are already tool results (indicating this is a follow-up call)
        has_tool_results = any(msg.role == "tool" for msg in messages)
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
                content="✓"  # Minimal acknowledgment that satisfies Pydantic validation
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

    # An image attachment is shown inline in the reply, without the user having to
    # expand the tool group that queued it.
    response_image = page.locator('[data-testid="response-image"]').first
    await response_image.wait_for(state="visible", timeout=30000)
    assert attachment_id in (await response_image.get_attribute("src") or "")

    # Clicking the inline image opens the full-screen viewer, and Escape closes it.
    await page.locator('[data-testid="response-image-trigger"]').first.click()
    lightbox_image = page.locator('[data-testid="image-lightbox-image"]')
    await lightbox_image.wait_for(state="visible", timeout=10000)
    assert attachment_id in (await lightbox_image.get_attribute("src") or "")
    await page.keyboard.press("Escape")
    await lightbox_image.wait_for(state="detached", timeout=10000)

    await chat_page.wait_for_attachments_ready(timeout=30000)

    # Verify the attachment display is shown to the user.
    await page.locator('[data-testid="attachment-preview"]').first.wait_for(
        state="visible",
        timeout=30000,
    )

    attachment_url = f"{web_test_fixture.base_url}/api/attachments/{attachment_id}"
    response = await page.request.get(attachment_url)
    assert response.status == 200, (
        f"Attachment {attachment_id} should be accessible, got {response.status}"
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
    web_test_fixture: WebTestFixture,
    mock_llm_client: RuleBasedMockLLMClient,
) -> None:
    """Test that multiple attachments from attach_to_response are displayed correctly."""
    mock_llm_client.rules = []
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
        str(uuid.uuid4()),
        str(uuid.uuid4()),
    ]
    llm_response = "Here are both images you requested"

    # Mock the LLM to respond with multiple attachments - only on initial request
    def should_trigger_multi_attach_tool(args: dict) -> bool:
        messages = args.get("messages", [])
        # Check if user message contains the trigger phrase
        has_trigger = any(
            "send me both images" in (msg.content or "").lower()
            for msg in messages
            if msg.role == "user"
        )
        # Check if there are already tool results (indicating this is a follow-up call)
        has_tool_results = any(msg.role == "tool" for msg in messages)
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
                content="✓"  # Minimal acknowledgment that satisfies Pydantic validation
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
            web_test_fixture,
            attachment_id,
            conversation_id,
            chat_page.base_url,
        )

    await chat_page.send_message("send me both images")

    # Wait for assistant response to complete, then for attachment tool to be ready
    await chat_page.wait_for_assistant_response(timeout=30000)
    await chat_page.wait_for_attachments_ready(timeout=30000)

    # Verify the attachment display is shown to the user.
    await page.locator('[data-testid="attachment-preview"]').first.wait_for(
        state="visible",
        timeout=30000,
    )

    # Verify attachment previews are now available
    async def attachment_preview_count() -> int:
        await chat_page.expand_tool_groups()
        attachment_previews = page.locator('[data-testid="attachment-preview"]')
        count = await attachment_previews.count()
        if count == 0:
            return 0
        await attachment_previews.first.wait_for(state="visible", timeout=2000)
        return count

    try:
        preview_count = await wait_for_condition(
            attachment_preview_count,
            timeout=30.0,
            description="attachment previews after wait_for_attachments_ready",
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

    # Verify the generated attachment URLs are usable. Preview rendering is
    # covered by the dedicated single-attachment flow test; this test focuses on
    # attach_to_response returning multiple accessible attachments.
    for attachment_id in attachment_ids:
        attachment_url = f"{web_test_fixture.base_url}/api/attachments/{attachment_id}"
        response = await page.request.get(attachment_url)
        assert response.status == 200, (
            f"Attachment {attachment_id} should be accessible, got {response.status}"
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
            "send invalid image" in (msg.content or "").lower()
            for msg in messages
            if msg.role == "user"
        )
        # Check if there are already tool results (indicating this is a follow-up call)
        has_tool_results = any(msg.role == "tool" for msg in messages)
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
                content="✓"  # Minimal acknowledgment that satisfies Pydantic validation
            ),
        ),
    ]

    await chat_page.navigate_to_chat()
    await chat_page.send_message("send invalid image")

    # Wait for the expected conversation content rather than generic streaming completion.
    # In this fail-fast error path, the tool can error before the UI settles into the
    # normal assistant/tool rendering sequence.
    await chat_page.wait_for_messages_with_content(
        {"user": "send invalid image", "assistant": llm_response},
        timeout=30000,
    )

    # Wait for the streaming interaction to finish before asserting the final DOM state.
    await chat_page.wait_for_streaming_complete(timeout=30000)

    tool_call_locator = page.locator(chat_page.MESSAGE_TOOL_CALL)
    if await tool_call_locator.count() > 0:
        tool_call_element = tool_call_locator.first
        await tool_call_element.wait_for(state="attached", timeout=5000)
        tool_call_text = await tool_call_element.text_content(timeout=2000)
        print(
            f"Tool call rendered with text: {tool_call_text[:100] if tool_call_text else 'None'}"
        )
        assert tool_call_text is not None and "Attachments" in tool_call_text

    conversation_id = await chat_page.get_current_conversation_id()
    assert conversation_id is not None, "Conversation ID should be available"

    async def get_history_error_messages() -> list[ConversationHistoryMessage]:
        response = await page.request.get(
            f"{web_test_fixture.base_url}/api/v1/chat/conversations/{conversation_id}/messages?limit=0"
        )
        assert response.status == 200, (
            f"Conversation messages API should succeed, got {response.status}"
        )
        data = await response.json()
        return [
            message
            for message in data.get("messages", [])
            if message.get("role") == "error"
        ]

    # Invalid attachments can legitimately surface as an error in several ways, and
    # the error rendering lands asynchronously after the stream's error event is
    # processed:
    # 1. a tool error result,
    # 2. fallback previews marked "failed to load",
    # 3. an assistant "encountered an error" message, or
    # 4. a persisted error-role history message.
    # That rendering can lag wait_for_streaming_complete (especially under the heavy
    # CPU contention of the full test run), so poll for any of these signals rather
    # than taking a single snapshot. A one-shot snapshot was the historical flake:
    # it raced the post-stream re-render and then fell back to the persisted error
    # message, which is not guaranteed to exist because the mid-stream failure can
    # prevent that error write from committing.
    last_error_state: dict[str, object] = {}

    async def invalid_attachment_error_signal() -> dict[str, object] | None:
        tool_result_element = page.locator('[data-testid="tool-result"]')
        tool_result_texts: list[str] = []
        for i in range(await tool_result_element.count()):
            tool_result_text = await tool_result_element.nth(i).text_content(
                timeout=2000
            )
            if tool_result_text:
                tool_result_texts.append(tool_result_text.lower())

        attachment_previews = page.locator('[data-testid="attachment-preview"]')
        preview_count = await attachment_previews.count()
        preview_texts: list[str] = []
        for i in range(preview_count):
            preview_text = await attachment_previews.nth(i).text_content()
            preview_texts.append((preview_text or "").lower())

        assistant_message_elements = page.locator(
            '[data-testid="assistant-message-content"]'
        )
        assistant_texts: list[str] = []
        for i in range(await assistant_message_elements.count()):
            assistant_text = await assistant_message_elements.nth(i).text_content()
            assistant_texts.append((assistant_text or "").lower())

        error_found = any(
            "error" in text or "failed" in text or "no valid attachments found" in text
            for text in tool_result_texts
        )
        previews_show_load_failure = preview_count > 0 and all(
            "failed to load" in text for text in preview_texts
        )
        # The assistant surfaces a streaming error in two shapes depending on
        # whether it had already produced text: a standalone "Sorry, I encountered
        # an error ..." message, or an "An error also occurred during this response"
        # suffix appended to the partial answer (see ChatApp handleStreamingComplete).
        # The invalid-attachment turn produces the latter, so both phrasings must be
        # recognised here.
        assistant_error_found = any(
            "encountered an error" in text or "an error also occurred" in text
            for text in assistant_texts
        )

        # The persisted error message is only consulted when no inline signal is
        # present yet, to avoid an API round-trip on every poll iteration.
        history_error_messages: list[ConversationHistoryMessage] = []
        if not (error_found or previews_show_load_failure or assistant_error_found):
            history_error_messages = await get_history_error_messages()

        last_error_state.update(
            error_found=error_found,
            previews_show_load_failure=previews_show_load_failure,
            assistant_error_found=assistant_error_found,
            history_error_found=bool(history_error_messages),
            tool_result_texts=tool_result_texts,
            preview_texts=preview_texts,
            assistant_texts=assistant_texts,
            history_error_messages=history_error_messages,
        )

        if (
            error_found
            or previews_show_load_failure
            or assistant_error_found
            or history_error_messages
        ):
            return last_error_state
        return None

    try:
        await wait_for_condition(
            invalid_attachment_error_signal,
            timeout=30.0,
            description="invalid attachment error signal",
        )
    except TimeoutError as exc:
        raise AssertionError(
            "Expected invalid attachment UI state but none appeared within 30s: "
            f"{last_error_state}"
        ) from exc

    print("Error handling test completed - invalid attachment handled appropriately")


@pytest.mark.playwright
async def test_tool_attachment_persistence_after_page_reload(
    web_test_fixture: WebTestFixture,
    mock_llm_client: RuleBasedMockLLMClient,
) -> None:
    """
    Test that tool-generated attachments persist after page reload.

    This is the core test for the bug fix: tool attachments were being stored
    but not registered in the database, causing them to 404 after page reload.
    """
    page = web_test_fixture.page
    chat_page = ChatPage(page, web_test_fixture.base_url)

    # Create a test attachment to simulate a tool-generated attachment
    attachment_id = str(uuid.uuid4())

    # Mock the LLM to respond with attach_to_response tool call
    def should_trigger_attach_tool(args: dict) -> bool:
        messages = args.get("messages", [])
        # Check if user message contains the trigger phrase
        has_trigger = any(
            "show attachment" in (msg.content or "").lower()
            for msg in messages
            if msg.role == "user"
        )
        # Check if there are already tool results (indicating this is a follow-up call)
        has_tool_results = any(msg.role == "tool" for msg in messages)
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
                content="✓"  # Minimal acknowledgment that satisfies Pydantic validation
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
        web_test_fixture,
        attachment_id,
        conversation_id,
        web_test_fixture.base_url,
    )

    await chat_page.send_message("show attachment")

    # Wait for tool execution and attachment display
    await chat_page.wait_for_assistant_response(timeout=30000)
    await chat_page.wait_for_attachments_ready(timeout=30000)
    print("[DEBUG] Attachment tool ready with content")

    # Verify attachment is displayed initially
    attachment_preview = page.locator('[data-testid="attachment-preview"]').first
    await attachment_preview.wait_for(state="visible", timeout=30000)
    print("[DEBUG] Attachment preview is visible")

    # Verify the attachment actually loads (not a 404)
    attachment_url = f"{web_test_fixture.base_url}/api/attachments/{attachment_id}"
    img_response = await page.request.get(attachment_url)
    assert img_response.status == 200, (
        f"Attachment should be accessible initially, got {img_response.status}"
    )

    # Ensure the conversation (including tool call) is persisted before reload
    await chat_page.wait_for_conversation_saved(timeout=30000)

    # CRITICAL TEST: Reload the page
    # Reloading page to test persistence...
    await page.reload()

    # Wait for the app shell and the known conversation content to be rehydrated.
    # A generic "assistant response complete" wait is too loose immediately after a
    # hard reload and can race React initialization under full-suite load.
    await chat_page.wait_for_load(wait_for_app_ready=True)
    await chat_page.wait_for_messages_with_content(
        {"user": "show attachment", "assistant": llm_response},
        timeout=30000,
    )
    await chat_page.wait_for_attachments_ready(timeout=30000)

    # THE BUG FIX TEST: Verify attachment is still accessible after reload.
    # This focuses on the persistence invariant that used to break: after a
    # reload, the conversation still exposes the attachment and the attachment
    # endpoint no longer 404s.
    try:
        await page.locator('[data-testid="attachment-preview"]').first.wait_for(
            state="visible",
            timeout=30000,
        )

        attachment_url_after_reload = (
            f"{web_test_fixture.base_url}/api/attachments/{attachment_id}"
        )
        img_response_after_reload = await page.request.get(attachment_url_after_reload)
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
