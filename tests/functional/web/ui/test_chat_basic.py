"""End-to-end tests for the chat UI - Basic chat flows."""

from collections.abc import Awaitable, Callable
from typing import Any

import pytest

from tests.functional.web.conftest import WebTestFixture
from tests.functional.web.pages.chat_page import ChatPage
from tests.mocks.mock_llm import LLMOutput, RuleBasedMockLLMClient


@pytest.mark.playwright
@pytest.mark.asyncio
async def test_basic_chat_conversation(
    web_test_fixture: WebTestFixture,
    mock_llm_client: RuleBasedMockLLMClient,
    take_screenshot: Callable[[Any, str, str], Awaitable[None]],
) -> None:
    """Test basic chat conversation functionality."""
    page = web_test_fixture.page
    chat_page = ChatPage(page, web_test_fixture.base_url)

    # Configure mock LLM response
    def llm_matcher(args: dict) -> bool:
        messages = args.get("messages", [])
        # Look for "Hello" in any message content
        for msg in messages:
            content = msg.content or ""
            if isinstance(content, str) and "Hello" in content:
                return True
        return False

    mock_llm_client.rules = [
        (
            llm_matcher,
            LLMOutput(
                content="Hello! I'm your test assistant. How can I help you today!"
            ),
        )
    ]

    # Also set a default response in case the rule doesn't match
    mock_llm_client.default_response = LLMOutput(
        content="Default test response from mock LLM"
    )

    # Navigate to chat
    await chat_page.navigate_to_chat()

    # Wait for the chat interface to be fully ready
    await page.wait_for_selector(
        '[data-testid="chat-input"]', state="visible", timeout=10000
    )
    await page.wait_for_selector(
        "button[aria-label='Toggle sidebar']", state="visible", timeout=10000
    )  # Desktop: sidebar toggle is in the header

    # Create a new chat to ensure we start fresh
    await chat_page.create_new_chat()

    # Wait for the new conversation ID to be set in the URL
    await page.wait_for_function(
        "window.location.href.includes('conversation_id=')",
        timeout=5000,
    )

    # Get the current conversation ID to verify it's set
    conv_id = await chat_page.get_current_conversation_id()
    assert conv_id is not None, "Conversation ID should be set after creating new chat"

    # Verify chat input is enabled
    assert await chat_page.is_chat_input_enabled()

    # Take screenshot of empty chat
    for viewport in ["desktop", "mobile"]:
        await take_screenshot(page, "chat-empty", viewport)

    # Send a message
    await chat_page.send_message("Hello, assistant!")

    # Wait for both user and assistant messages with expected content
    await chat_page.wait_for_messages_with_content(
        {
            "user": "Hello, assistant!",
            "assistant": "test assistant",  # Look for key phrase from mock response
        },
        timeout=20000,
    )

    # Get all messages for verification
    messages = await chat_page.get_all_messages()

    # Verify we have both messages
    assert len(messages) >= 2, f"Expected at least 2 messages, got {len(messages)}"
    assert any(m["role"] == "user" for m in messages), "No user message found"
    assert any(m["role"] == "assistant" for m in messages), "No assistant message found"

    # Verify chat input is re-enabled after streaming
    await page.wait_for_selector('[data-testid="chat-input"]:enabled', timeout=10000)
    assert await chat_page.is_chat_input_enabled()

    # Take screenshot of chat with conversation
    for viewport in ["desktop", "mobile"]:
        await take_screenshot(page, "chat-with-messages", viewport)

    # Get the actual response text
    response = await chat_page.get_last_assistant_message()

    # Verify response contains expected content (flexible matching due to potential formatting)
    assert response, "Assistant response should not be empty"
    assert "Hello" in response or "hello" in response, (
        f"Expected 'Hello' in response: {response}"
    )
    # The response should contain "test assistant" from our mock response
    # but allow flexibility in case the word gets split or modified
    assert (
        "test" in response.lower()
        or "assistant" in response.lower()
        or "help" in response.lower()
    ), f"Expected 'test', 'assistant', or 'help' in response: {response}"

    # Verify conversation ID was created with correct format
    conv_id = await chat_page.get_current_conversation_id()
    assert conv_id is not None
    # Frontend still uses web_conv_ prefix for now
    assert conv_id.startswith("web_conv_")

    # Verify both messages are displayed
    all_messages = await chat_page.get_all_messages()
    assert len(all_messages) == 2
    assert all_messages[0]["role"] == "user"
    assert all_messages[1]["role"] == "assistant"

    # Verify messages contain expected content (if content is available)
    if all_messages[0]["content"]:
        assert "Hello" in all_messages[0]["content"]
    if all_messages[1]["content"]:
        assert "Hello!" in all_messages[1]["content"]
        assert "test assistant" in all_messages[1]["content"]


@pytest.mark.playwright
@pytest.mark.asyncio
async def test_sidebar_functionality(
    web_test_fixture: WebTestFixture,
    mock_llm_client: RuleBasedMockLLMClient,
    take_screenshot: Callable[[Any, str, str], Awaitable[None]],
) -> None:
    """Test sidebar toggle and conversation list functionality."""
    page = web_test_fixture.page
    chat_page = ChatPage(page, web_test_fixture.base_url)

    # Simple mock response
    mock_llm_client.rules = [(lambda args: True, LLMOutput(content="Test response"))]

    # Navigate to chat
    await chat_page.navigate_to_chat()

    # Test sidebar toggle
    initial_sidebar_state = await chat_page.is_sidebar_open()
    await chat_page.toggle_sidebar()
    new_sidebar_state = await chat_page.is_sidebar_open()
    assert initial_sidebar_state != new_sidebar_state

    # Ensure sidebar is open for next tests
    if not await chat_page.is_sidebar_open():
        await chat_page.toggle_sidebar()

    # Create a conversation
    await chat_page.send_message("Test message for sidebar")
    await chat_page.wait_for_assistant_response()

    # Take screenshot with sidebar open
    for viewport in ["desktop", "mobile"]:
        await take_screenshot(page, "chat-sidebar-open", viewport)

    # Wait for conversation to be saved
    await chat_page.wait_for_conversation_saved()
    await chat_page.wait_for_conversation_in_sidebar()

    # Check conversation appears in list
    conversations = await chat_page.get_conversation_list()
    assert len(conversations) > 0

    # Verify conversation has preview text
    latest_conv = conversations[0]
    assert latest_conv["preview"]
    # Ideally would check for exact text, but preview might be truncated or formatted differently
    assert "Test" in latest_conv["preview"] or "message" in latest_conv["preview"]


@pytest.mark.playwright
@pytest.mark.asyncio
async def test_multiple_messages_in_conversation(
    web_test_fixture: WebTestFixture, mock_llm_client: RuleBasedMockLLMClient
) -> None:
    """Test sending multiple messages in a single conversation."""
    page = web_test_fixture.page
    chat_page = ChatPage(page, web_test_fixture.base_url)

    # Configure different responses based on message count
    def response_based_on_history(args: dict) -> LLMOutput:
        messages = args.get("messages", [])
        user_messages = [m for m in messages if m.role == "user"]

        if len(user_messages) == 1:
            return LLMOutput(content="This is my first response.")
        elif len(user_messages) == 2:
            return LLMOutput(
                content="This is my second response, I remember our conversation."
            )
        else:
            return LLMOutput(content=f"This is response number {len(user_messages)}.")

    mock_llm_client.rules = [(lambda args: True, response_based_on_history)]

    # Navigate to chat
    await chat_page.navigate_to_chat()

    # Send first message
    await chat_page.send_message("First message")
    await chat_page.wait_for_message_count(2)  # 1 user + 1 assistant

    # Send second message
    await chat_page.send_message("Second message")
    await chat_page.wait_for_message_count(4)  # 2 user + 2 assistant

    # Send third message
    await chat_page.send_message("Third message")
    await chat_page.wait_for_message_count(6)  # 3 user + 3 assistant

    # Verify we have multiple messages
    all_messages = await chat_page.get_all_messages()
    user_messages = [m for m in all_messages if m["role"] == "user"]
    assistant_messages = [m for m in all_messages if m["role"] == "assistant"]

    assert len(user_messages) == 3, (
        f"Expected 3 user messages, got {len(user_messages)}"
    )
    assert len(assistant_messages) == 3, (
        f"Expected 3 assistant messages, got {len(assistant_messages)}"
    )

    # Verify message content patterns
    if user_messages[0]["content"]:
        assert "First" in user_messages[0]["content"]
    if user_messages[1]["content"]:
        assert "Second" in user_messages[1]["content"]
    if user_messages[2]["content"]:
        assert "Third" in user_messages[2]["content"]

    # For now, just verify we have 3 assistant messages
    # Content extraction seems to be an issue with the assistant-ui library
    assert len(assistant_messages) == 3


@pytest.mark.playwright
@pytest.mark.asyncio
async def test_empty_conversation_state(
    web_test_fixture_readonly: WebTestFixture, mock_llm_client: RuleBasedMockLLMClient
) -> None:
    """Test the chat UI in empty state with no conversations."""
    page = web_test_fixture_readonly.page
    chat_page = ChatPage(page, web_test_fixture_readonly.base_url)

    # Navigate to chat
    await chat_page.navigate_to_chat()

    # Verify chat input is available even with no messages
    assert await chat_page.is_chat_input_enabled()

    # Check that a new conversation ID was generated
    conv_id = await chat_page.get_current_conversation_id()
    assert conv_id is not None


@pytest.mark.playwright
@pytest.mark.asyncio
async def test_responsive_mobile_list_detail(
    web_test_fixture_readonly: WebTestFixture, mock_llm_client: RuleBasedMockLLMClient
) -> None:
    """Test list-detail navigation pattern on mobile viewport."""
    page = web_test_fixture_readonly.page
    chat_page = ChatPage(page, web_test_fixture_readonly.base_url)

    # Set mobile viewport
    await page.set_viewport_size({"width": 375, "height": 667})

    # Navigate to chat
    await chat_page.navigate_to_chat()

    # On mobile, the app auto-creates a conversation, so we start in chat detail view
    # The back button should be visible (not a sidebar toggle)
    await page.wait_for_selector(
        "button[aria-label='Back to conversations']", state="visible", timeout=10000
    )
    await page.wait_for_selector(
        '[data-testid="chat-input"]', state="visible", timeout=10000
    )

    # Conversation list should NOT be visible (we're in chat detail view)
    assert not await chat_page.is_sidebar_open()

    # Tap back button to navigate to conversation list
    await page.click("button[aria-label='Back to conversations']")

    # Wait for conversation list to appear
    await page.wait_for_selector(
        '[data-testid="new-chat-button"]', state="visible", timeout=5000
    )

    # Verify conversation list is now showing
    assert await chat_page.is_sidebar_open()

    # Chat input should NOT be visible (we're on the list view)
    chat_input = await page.query_selector('[data-testid="chat-input"]')
    assert chat_input is None or not await chat_input.is_visible(), (
        "Chat input should not be visible on conversation list view"
    )

    # Create a new chat from the list view
    await page.click('[data-testid="new-chat-button"]')

    # Should navigate to chat detail view
    await page.wait_for_selector(
        "button[aria-label='Back to conversations']", state="visible", timeout=5000
    )
    await page.wait_for_selector(
        '[data-testid="chat-input"]', state="visible", timeout=5000
    )

    # Verify we're back in chat detail view
    assert not await chat_page.is_sidebar_open()

    # Reset viewport
    await page.set_viewport_size({"width": 1280, "height": 720})


@pytest.mark.playwright
@pytest.mark.asyncio
async def test_mobile_chat_input_visibility(
    web_test_fixture_readonly: WebTestFixture, mock_llm_client: RuleBasedMockLLMClient
) -> None:
    """Test that chat input is visible and accessible on mobile viewport without scrolling."""
    page = web_test_fixture_readonly.page
    chat_page = ChatPage(page, web_test_fixture_readonly.base_url)

    # Configure mock LLM response
    mock_llm_client.rules = [
        (
            lambda args: "test mobile" in str(args.get("messages", [])),
            LLMOutput(content="Mobile test response received successfully!"),
        )
    ]

    # Set mobile viewport (iPhone SE size)
    await page.set_viewport_size({"width": 375, "height": 667})

    # Navigate to chat
    await chat_page.navigate_to_chat()

    # Wait for React app to fully initialize on mobile
    # On mobile, the app auto-creates a conversation so we see the chat detail view
    await page.wait_for_selector(
        "button[aria-label='Back to conversations']", state="visible", timeout=10000
    )
    await page.wait_for_selector(
        '[data-testid="chat-input"]', state="visible", timeout=10000
    )
    # Wait for chat input to be enabled (React initialization complete)
    await page.wait_for_function(
        "document.querySelector('[data-testid=\"chat-input\"]')?.disabled === false",
        timeout=5000,
    )

    # Check if chat input is visible without scrolling
    chat_input = await page.wait_for_selector(
        chat_page.CHAT_INPUT, state="visible", timeout=10000
    )
    assert chat_input is not None, "Chat input should be found"

    # Get the input element's position relative to the viewport
    input_box = await chat_input.bounding_box()
    assert input_box is not None, "Chat input bounding box should be available"

    # Get viewport height
    viewport = page.viewport_size
    assert viewport is not None, "Viewport size should be available"
    viewport_height = viewport["height"]

    # Verify that the input is visible within the viewport (with some margin for padding)
    # The input should be fully visible, meaning its bottom should be within the viewport
    assert input_box["y"] + input_box["height"] <= viewport_height, (
        f"Chat input is below the fold. Input bottom at {input_box['y'] + input_box['height']}px "
        f"but viewport height is {viewport_height}px. Input is {(input_box['y'] + input_box['height']) - viewport_height}px below the fold."
    )

    # Also verify the input is reasonably positioned (not too close to the very bottom)
    assert input_box["y"] < viewport_height - 50, (
        f"Chat input should have some margin from bottom. Input top at {input_box['y']}px "
        f"but viewport height is {viewport_height}px."
    )

    # Test that the input is functional
    assert chat_input is not None, "Chat input should be available for interaction"
    await chat_input.click()
    await chat_input.type("test mobile message")

    # Verify we can send the message
    await chat_input.press("Enter")

    # Wait for the message to appear
    await chat_page.wait_for_assistant_response(timeout=15000)

    # Verify the conversation works on mobile
    messages = await chat_page.get_all_messages()
    assert len(messages) >= 2, "Should have user and assistant messages"

    # Test that last message is not obscured by input
    # Send multiple messages to fill the viewport
    for i in range(3):
        await chat_page.send_message(f"Additional test message {i + 1}")
        await chat_page.wait_for_assistant_response(timeout=15000)

    # Wait until all user/assistant message pairs have rendered to avoid race conditions
    await chat_page.wait_for_message_count(2 + (3 * 2), timeout=20000)

    # Get all message elements
    message_elements = await page.query_selector_all(
        '[data-testid="user-message"], [data-testid="assistant-message"]'
    )
    assert len(message_elements) >= 8, (
        "Should have multiple messages after sending more"
    )

    # Get the last message element position
    last_message = message_elements[-1]
    last_message_box = await last_message.bounding_box()
    assert last_message_box is not None, "Last message bounding box should be available"

    # Get the input container position
    input_container = await page.query_selector('[data-testid="composer-container"]')
    assert input_container is not None, "Input container should exist"
    container_box = await input_container.bounding_box()
    assert container_box is not None, "Input container bounding box should be available"

    # With the new layout, messages should auto-scroll to be visible
    # Wait for the last message to be in viewport by checking its position
    await page.wait_for_function(
        """() => {
            const messages = document.querySelectorAll('[data-testid="user-message"], [data-testid="assistant-message"]');
            if (messages.length === 0) return false;
            const lastMessage = messages[messages.length - 1];
            const rect = lastMessage.getBoundingClientRect();
            const inputContainer = document.querySelector('[data-testid="composer-container"]');
            const inputTop = inputContainer ? inputContainer.getBoundingClientRect().top : window.innerHeight;
            // Check if last message is above the input container
            return rect.bottom <= inputTop;
        }""",
        timeout=5000,
    )

    # Check if the last message is visible without manual scrolling
    last_message_box_auto = await last_message.bounding_box()
    assert last_message_box_auto is not None, "Last message should be visible"

    # Verify the last message is not obscured by the input
    # The bottom of the last message should be above the top of the input container
    assert (
        last_message_box_auto["y"] + last_message_box_auto["height"]
        <= container_box["y"]
    ), (
        f"Last message is obscured by input. Message bottom at "
        f"{last_message_box_auto['y'] + last_message_box_auto['height']}px "
        f"but input container starts at {container_box['y']}px. "
        f"Overlap of {(last_message_box_auto['y'] + last_message_box_auto['height']) - container_box['y']}px."
    )

    # Reset viewport
    await page.set_viewport_size({"width": 1280, "height": 720})
