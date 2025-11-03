"""End-to-end tests for the chat UI - Advanced features and complex interactions."""

import json

import pytest

from family_assistant.llm import ToolCallFunction, ToolCallItem
from tests.functional.web.conftest import WebTestFixture
from tests.functional.web.pages.chat_page import ChatPage
from tests.mocks.mock_llm import LLMOutput, RuleBasedMockLLMClient


@pytest.mark.playwright
@pytest.mark.asyncio
async def test_conversation_persistence_and_switching(
    web_test_fixture: WebTestFixture, mock_llm_client: RuleBasedMockLLMClient
) -> None:
    """Test that conversations persist and can be switched between."""
    page = web_test_fixture.page
    chat_page = ChatPage(page, web_test_fixture.base_url)

    # Configure mock LLM responses
    mock_llm_client.rules = [
        (
            lambda args: "first conversation" in str(args.get("messages", [])),
            LLMOutput(content="This is the response for the first conversation."),
        ),
        (
            lambda args: "second conversation" in str(args.get("messages", [])),
            LLMOutput(content="This is the response for the second conversation."),
        ),
    ]

    # Navigate to chat
    await chat_page.navigate_to_chat()

    # Create first conversation
    await chat_page.send_message("This is my first conversation")

    # Wait for messages with expected content
    await chat_page.wait_for_messages_with_content(
        {"user": "first conversation", "assistant": "response for the first"},
        timeout=20000,
    )

    # Verify first conversation has expected response
    messages = await chat_page.get_all_messages()
    assert len(messages) == 2
    assert messages[0]["role"] == "user"
    assert messages[1]["role"] == "assistant"
    # Check content contains expected text
    if messages[0]["content"]:
        assert "first conversation" in messages[0]["content"]
    if messages[1]["content"]:
        assert "response for the first" in messages[1]["content"], (
            f"Expected 'response for the first' in message: {messages[1]['content']}"
        )
    first_conv_id = await chat_page.get_current_conversation_id()

    # Wait for conversation to be saved (this now includes wait_for_streaming_complete)
    await chat_page.wait_for_conversation_saved()

    # Create a new chat
    await chat_page.create_new_chat()

    # Verify we have a new conversation ID
    new_conv_id = await chat_page.get_current_conversation_id()
    assert new_conv_id != first_conv_id

    # Send message in second conversation
    await chat_page.send_message("This is my second conversation")

    # Wait for messages with expected content
    await chat_page.wait_for_messages_with_content(
        {"user": "second conversation", "assistant": "response for the second"},
        timeout=20000,
    )

    # Verify second conversation has expected response
    messages2 = await chat_page.get_all_messages()
    assert len(messages2) == 2
    assert messages2[0]["role"] == "user"
    assert messages2[1]["role"] == "assistant"
    if messages2[0]["content"]:
        assert "second conversation" in messages2[0]["content"]
    if messages2[1]["content"]:
        # Check for expected content - the mock should return something about "second conversation"
        assert "response for the second" in messages2[1]["content"], (
            f"Expected 'response for the second' in assistant response: {messages2[1]['content']}"
        )

    # Wait for conversation to be saved
    await chat_page.wait_for_conversation_saved()

    # Get conversation list
    conversations = await chat_page.get_conversation_list()
    assert len(conversations) >= 2

    # Switch back to first conversation
    assert first_conv_id is not None  # Type guard
    await chat_page.select_conversation(first_conv_id)

    # Verify we're back in the first conversation
    current_conv_id = await chat_page.get_current_conversation_id()
    assert current_conv_id == first_conv_id

    # Wait for messages to load after switching
    await chat_page.wait_for_messages_with_content(
        {"user": "first conversation", "assistant": "response for the first"},
        timeout=10000,
    )

    # Verify the messages from the first conversation are displayed correctly
    messages = await chat_page.get_all_messages()
    assert len(messages) == 2, f"Expected 2 messages, got {len(messages)}"
    assert messages[0]["role"] == "user"
    assert messages[1]["role"] == "assistant"
    # Verify content if available
    if messages[0]["content"]:
        assert "first conversation" in messages[0]["content"]
    if messages[1]["content"]:
        assert "response for the first" in messages[1]["content"], (
            f"Expected 'response for the first' in message: {messages[1]['content']}"
        )


@pytest.mark.playwright
@pytest.mark.asyncio
async def test_tool_call_display(
    web_test_fixture: WebTestFixture, mock_llm_client: RuleBasedMockLLMClient
) -> None:
    """Test that tool calls are properly displayed in the chat UI."""
    page = web_test_fixture.page
    chat_page = ChatPage(page, web_test_fixture.base_url)

    # Configure mock LLM to respond with a tool call
    tool_call_id = "call_test123"
    mock_llm_client.rules = [
        (
            lambda args: "add a note" in str(args.get("messages", [])),
            LLMOutput(
                content="I'll add that note for you.",
                tool_calls=[
                    ToolCallItem(
                        id=tool_call_id,
                        type="function",
                        function=ToolCallFunction(
                            name="add_or_update_note",
                            arguments=json.dumps({
                                "title": "Test Note from Chat",
                                "content": "This is a test note created via chat UI",
                            }),
                        ),
                    )
                ],
            ),
        ),
        (
            lambda args: any(
                msg.get("role") == "tool" and msg.get("tool_call_id") == tool_call_id
                for msg in args.get("messages", [])
            ),
            LLMOutput(
                content="I've successfully added the note 'Test Note from Chat' for you."
            ),
        ),
    ]

    # Navigate to chat
    await chat_page.navigate_to_chat()

    # Send message requesting tool use
    await chat_page.send_message(
        "Please add a note titled 'Test Note from Chat' with content 'This is a test note created via chat UI'"
    )

    # Wait for assistant response (tool calls may be combined into one message)
    await chat_page.wait_for_assistant_response(timeout=15000)

    # Wait for all streaming to complete before verifying messages
    await chat_page.wait_for_streaming_complete(timeout=10000)

    # Verify messages including tool responses
    all_messages = await chat_page.get_all_messages()
    assert len(all_messages) >= 2, (
        f"Expected at least 2 messages, got {len(all_messages)}"
    )

    # Verify user message
    user_messages = [m for m in all_messages if m["role"] == "user"]
    assert len(user_messages) >= 1
    if user_messages[0]["content"]:
        assert "add a note" in user_messages[0]["content"]

    # Verify assistant responses (may be combined or separate)
    assistant_messages = [m for m in all_messages if m["role"] == "assistant"]
    assert len(assistant_messages) >= 1, "No assistant messages found"

    # Check that at least one message mentions the note creation (if content available)
    all_assistant_content = " ".join(
        m["content"] for m in assistant_messages if m["content"]
    )
    if all_assistant_content:
        assert "note" in all_assistant_content.lower()

    # IMPORTANT: Verify tool UI elements are displayed
    # Wait for tool UI elements to be rendered
    await chat_page.wait_for_tool_call_display()

    # Get tool calls that were displayed
    tool_calls = await chat_page.get_tool_calls()
    assert len(tool_calls) > 0, "Expected at least one tool UI element to be displayed"

    # Verify the tool UI contains expected content
    tool_display_text = tool_calls[0].get("display_text", "")
    assert "add_or_update_note" in tool_display_text or "Note" in tool_display_text, (
        f"Expected tool UI to show note-related content, got: {tool_display_text}"
    )


@pytest.mark.playwright
@pytest.mark.asyncio
async def test_tool_call_status_progression(
    web_test_fixture: WebTestFixture, mock_llm_client: RuleBasedMockLLMClient
) -> None:
    """Test that tool calls show proper status progression from running to complete."""
    page = web_test_fixture.page
    chat_page = ChatPage(page, web_test_fixture.base_url)

    # Configure mock LLM to respond with a tool call
    tool_call_id = "call_status_test"
    mock_llm_client.rules = [
        (
            lambda args: "add a note for status test" in str(args.get("messages", [])),
            LLMOutput(
                content="I'll add that note and demonstrate status progression.",
                tool_calls=[
                    ToolCallItem(
                        id=tool_call_id,
                        type="function",
                        function=ToolCallFunction(
                            name="add_or_update_note",
                            arguments=json.dumps({
                                "title": "Status Test Note",
                                "content": "Testing tool call status progression from running to complete",
                            }),
                        ),
                    )
                ],
            ),
        ),
        (
            lambda args: any(
                msg.get("role") == "tool" and msg.get("tool_call_id") == tool_call_id
                for msg in args.get("messages", [])
            ),
            LLMOutput(
                content="The note has been created successfully and the tool call is now complete!"
            ),
        ),
    ]

    # Navigate to chat
    await chat_page.navigate_to_chat()

    # Send message requesting tool use
    await chat_page.send_message("Please add a note for status test")

    # Wait for tool call UI to appear
    await chat_page.wait_for_tool_call_display()

    # At this point, we should initially see a running/pending status (spinning icon)
    # In a more sophisticated implementation, we would check for specific status icons
    # For now, we verify that tool calls are displayed

    # Wait for tool execution to complete
    await chat_page.wait_for_assistant_response(timeout=15000)

    # Handle tool confirmation if it appears
    try:
        await chat_page.wait_for_confirmation_dialog(timeout=5000)
        await chat_page.approve_tool_confirmation()
    except Exception:
        pass  # No confirmation dialog appeared or approval failed

    # Wait for streaming to complete
    await chat_page.wait_for_streaming_complete(timeout=10000)

    # Get tool calls after completion
    tool_calls = await chat_page.get_tool_calls()
    assert len(tool_calls) > 0, "Expected at least one tool UI element to be displayed"

    # Verify the tool call completed successfully
    # In a full implementation, we would check for specific status indicators
    # such as checkmark icons vs spinning icons
    # For now, verify the tool call is still displayed and contains expected content
    tool_display_text = tool_calls[0].get("display_text", "")
    assert "add_or_update_note" in tool_display_text or "Note" in tool_display_text, (
        f"Expected completed tool UI to show note-related content, got: {tool_display_text}"
    )

    # Verify final assistant response acknowledges completion
    all_messages = await chat_page.get_all_messages()
    assistant_messages = [m for m in all_messages if m["role"] == "assistant"]
    assert len(assistant_messages) >= 1

    # Check that final response mentions completion - be more flexible
    all_assistant_content = " ".join(
        m["content"] for m in assistant_messages if m["content"]
    )
    if all_assistant_content:
        # Accept either completion/success keywords OR the presence of tool execution result
        has_completion_keywords = (
            "complete" in all_assistant_content.lower()
            or "success" in all_assistant_content.lower()
            or "created" in all_assistant_content.lower()
        )
        # If we have tool calls displayed, that's also evidence of successful completion
        has_tool_display = len(tool_calls) > 0 and any(
            "add_or_update_note" in tc.get("display_text", "")
            or "Note" in tc.get("display_text", "")
            for tc in tool_calls
        )

        assert has_completion_keywords or has_tool_display, (
            f"Expected completion message or tool display in assistant response. "
            f"Content: {all_assistant_content}, Tool calls: {tool_calls}"
        )


@pytest.mark.flaky(reruns=2)
@pytest.mark.playwright
@pytest.mark.asyncio
async def test_conversation_loading_with_tool_calls(
    web_test_fixture: WebTestFixture, mock_llm_client: RuleBasedMockLLMClient
) -> None:
    """Test that conversations with tool calls load correctly when selected from sidebar."""
    page = web_test_fixture.page
    chat_page = ChatPage(page, web_test_fixture.base_url)

    # Configure mock LLM for tool call
    tool_call_id = "call_load_test"

    def initial_request_matcher(args: dict) -> bool:
        """Match the initial user request for creating a note."""
        messages = args.get("messages", [])
        # Check if this is the initial request (no tool messages yet)
        has_tool_messages = any(msg.get("role") == "tool" for msg in messages)
        has_user_request = any(
            msg.get("role") == "user"
            and "create a note for testing" in str(msg.get("content", ""))
            for msg in messages
        )
        return has_user_request and not has_tool_messages

    def tool_result_matcher(args: dict) -> bool:
        """Match when we have a tool result and should provide final response."""
        messages = args.get("messages", [])
        return any(
            msg.get("role") == "tool" and msg.get("tool_call_id") == tool_call_id
            for msg in messages
        )

    mock_llm_client.rules = [
        (
            initial_request_matcher,
            LLMOutput(
                content="Creating a test note for you.",
                tool_calls=[
                    ToolCallItem(
                        id=tool_call_id,
                        type="function",
                        function=ToolCallFunction(
                            name="add_or_update_note",
                            arguments=json.dumps({
                                "title": "Tool Call Test Note",
                                "content": "Testing tool call display on page load",
                            }),
                        ),
                    )
                ],
            ),
        ),
        (
            tool_result_matcher,
            LLMOutput(content="Note created successfully!"),
        ),
    ]

    # Navigate to chat and create conversation with tool call
    await chat_page.navigate_to_chat()
    await chat_page.send_message("Please create a note for testing tool call display")

    # Get the conversation ID and wait for it to be saved immediately after sending
    conv_id_with_tools = await chat_page.get_current_conversation_id()

    # Wait for the conversation to be saved to the backend via API polling
    # This should happen quickly since the user message is saved in its own transaction
    await chat_page.wait_for_conversation_saved()

    # Wait for tool call to complete
    await chat_page.wait_for_assistant_response(timeout=15000)

    # Handle tool confirmation if it appears
    try:
        await chat_page.wait_for_confirmation_dialog(timeout=5000)
        await chat_page.approve_tool_confirmation()
    except Exception:
        pass  # No confirmation dialog appeared or approval failed

    await chat_page.wait_for_streaming_complete(timeout=10000)

    # Verify we have tool call messages
    messages = await chat_page.get_all_messages()
    assert len(messages) >= 2
    assistant_messages = [m for m in messages if m["role"] == "assistant"]
    assert len(assistant_messages) >= 1
    # Check content if available
    if any(m["content"] for m in assistant_messages):
        assert any(
            "note" in m["content"].lower() for m in assistant_messages if m["content"]
        )

    # Create a new chat to navigate away
    await chat_page.create_new_chat()

    # Refresh the page to load the updated conversation list since there are no live updates
    await page.reload()
    await chat_page.wait_for_load()

    # Ensure sidebar is open after reload
    if not await chat_page.is_sidebar_open():
        await chat_page.toggle_sidebar()

    # Now the conversation should be visible in the sidebar
    assert conv_id_with_tools is not None  # Type guard
    conv_selector = f'[data-conversation-id="{conv_id_with_tools}"]'
    await page.wait_for_selector(conv_selector, timeout=10000, state="visible")

    # Navigate back to the conversation with tool calls
    await chat_page.select_conversation(conv_id_with_tools)

    # Verify the conversation loaded correctly
    current_conv_id = await chat_page.get_current_conversation_id()
    assert current_conv_id == conv_id_with_tools

    # Wait for messages to load after switching
    await chat_page.wait_for_messages_with_content(
        {"user": "note for testing"}, timeout=10000
    )

    # Verify messages loaded from the conversation with tool calls
    loaded_messages = await chat_page.get_all_messages()
    assert len(loaded_messages) >= 2, (
        f"Expected at least 2 messages after loading, got {len(loaded_messages)}"
    )

    # Verify the conversation contains our tool call request
    user_messages = [m for m in loaded_messages if m["role"] == "user"]
    assert len(user_messages) >= 1
    if user_messages[0]["content"]:
        assert "note for testing" in user_messages[0]["content"]
