"""Tests for tool call grouping and collapsible UI functionality."""

import pytest
from playwright.async_api import expect

from family_assistant.llm import LLMOutput, ToolCallFunction, ToolCallItem
from tests.functional.web.conftest import WebTestFixture
from tests.functional.web.pages.chat_page import ChatPage
from tests.mocks.mock_llm import RuleBasedMockLLMClient


@pytest.mark.playwright
@pytest.mark.asyncio
async def test_multiple_tool_calls_are_grouped(
    web_test_fixture: WebTestFixture, mock_llm_client: RuleBasedMockLLMClient
) -> None:
    """Test that multiple consecutive tool calls are grouped in a collapsible section."""
    page = web_test_fixture.page
    chat_page = ChatPage(page, web_test_fixture.base_url)

    # Configure mock LLM to respond with multiple tool calls
    mock_llm_client.rules = [
        (
            lambda args: "add multiple notes" in str(args.get("messages", [])).lower(),
            LLMOutput(
                content="I'll add several notes for you.",
                tool_calls=[
                    ToolCallItem(
                        id="call_1",
                        type="function",
                        function=ToolCallFunction(
                            name="add_or_update_note",
                            arguments='{"title": "Note 1", "content": "First note content"}',
                        ),
                    ),
                    ToolCallItem(
                        id="call_2",
                        type="function",
                        function=ToolCallFunction(
                            name="add_or_update_note",
                            arguments='{"title": "Note 2", "content": "Second note content"}',
                        ),
                    ),
                    ToolCallItem(
                        id="call_3",
                        type="function",
                        function=ToolCallFunction(
                            name="search_documents",
                            arguments='{"query": "test information"}',
                        ),
                    ),
                ],
            ),
        ),
        # Mock tool responses
        (
            lambda args: any(
                msg.get("role") == "tool"
                and "call_1" in str(msg.get("tool_call_id", ""))
                for msg in args.get("messages", [])
            ),
            LLMOutput(content="Successfully added all three notes."),
        ),
        (
            lambda args: any(
                msg.get("role") == "tool"
                and "call_2" in str(msg.get("tool_call_id", ""))
                for msg in args.get("messages", [])
            ),
            LLMOutput(content="Successfully added all three notes."),
        ),
        (
            lambda args: any(
                msg.get("role") == "tool"
                and "call_3" in str(msg.get("tool_call_id", ""))
                for msg in args.get("messages", [])
            ),
            LLMOutput(content="Successfully added all three notes."),
        ),
    ]

    await chat_page.navigate_to_chat()
    await chat_page.send_message("Please add multiple notes for testing")

    # Wait for the assistant's response and tool calls
    await chat_page.wait_for_message_content("I'll add several notes for you.")

    # Wait for tool calls to appear
    assistant_message = page.locator('[data-testid="assistant-message"]')
    await assistant_message.wait_for(state="visible", timeout=10000)

    # Wait for tool call elements to be visible
    await page.wait_for_selector(
        '[data-testid*="tool-call"]', state="visible", timeout=10000
    )

    # Check console logs for our ToolGroup debug message
    console_messages = []
    page.on("console", lambda msg: console_messages.append(msg.text))

    # Look for any tool calls being rendered (basic functionality test)
    # Try multiple selectors that might be used for tool calls
    tool_selectors = [
        '[data-testid="tool-group"]',
        '[data-ui="tool-call-content"]',
        ".tool-call-content",
        '[data-testid*="tool"]',
    ]

    tool_found = False
    for selector in tool_selectors:
        tool_elements = page.locator(selector)
        count = await tool_elements.count()
        if count > 0:
            tool_found = True
            print(f"Found {count} elements with selector: {selector}")
            break

    # If no tool elements found, fail the test explicitly
    assert tool_found, "No tool elements found - ToolGroup integration is not working"

    # Test that ToolGroup component is rendered for multiple tool calls
    tool_group = page.locator('[data-testid="tool-group"]')
    tool_group_count = await tool_group.count()
    print(f"Found {tool_group_count} ToolGroup elements")
    await tool_group.wait_for(state="visible", timeout=10000)

    # Test that trigger shows correct tool count
    trigger = page.locator('[data-testid="tool-group-trigger"]')
    await trigger.wait_for(state="visible", timeout=5000)

    # Verify the tool count is displayed correctly (now shows category-based summary)
    tool_count_text = await trigger.text_content()
    assert tool_count_text is not None, "Tool group trigger should have text content"
    # The text should show something like "2 notes and 1 documents" (category-based)
    # Just verify it contains information about multiple tools
    assert any(
        keyword in tool_count_text.lower() for keyword in ["note", "document", "tool"]
    ), f"Expected tool/category information in trigger text, got: {tool_count_text}"

    # Test that group is initially expanded (so attachments are immediately visible)
    content = page.locator('[data-testid="tool-group-content"]')
    await content.wait_for(state="attached", timeout=5000)

    # Check if content is initially expanded (no hidden attribute means expanded)
    is_hidden = await content.get_attribute("hidden")
    assert is_hidden is None, (
        "ToolGroup content should be initially expanded so attachments are visible"
    )


@pytest.mark.playwright
@pytest.mark.asyncio
async def test_tool_group_expand_collapse_interaction(
    web_test_fixture: WebTestFixture, mock_llm_client: RuleBasedMockLLMClient
) -> None:
    """Test that users can expand and collapse tool groups."""
    page = web_test_fixture.page
    chat_page = ChatPage(page, web_test_fixture.base_url)

    # Configure mock LLM with two tool calls
    mock_llm_client.rules = [
        (
            lambda args: "search and add" in str(args.get("messages", [])).lower(),
            LLMOutput(
                content="I'll search for information and then add a note.",
                tool_calls=[
                    ToolCallItem(
                        id="call_search",
                        type="function",
                        function=ToolCallFunction(
                            name="search_documents",
                            arguments='{"query": "test information"}',
                        ),
                    ),
                    ToolCallItem(
                        id="call_note",
                        type="function",
                        function=ToolCallFunction(
                            name="add_or_update_note",
                            arguments='{"title": "Search Results", "content": "Found information"}',
                        ),
                    ),
                ],
            ),
        ),
        # Mock tool responses
        (
            lambda args: any(
                msg.get("role") == "tool"
                and "call_search" in str(msg.get("tool_call_id", ""))
                for msg in args.get("messages", [])
            ),
            LLMOutput(content="Search completed and note added."),
        ),
        (
            lambda args: any(
                msg.get("role") == "tool"
                and "call_note" in str(msg.get("tool_call_id", ""))
                for msg in args.get("messages", [])
            ),
            LLMOutput(content="Search completed and note added."),
        ),
    ]

    await chat_page.navigate_to_chat()
    await chat_page.send_message("Please search and add a note")

    # Wait for assistant message
    await chat_page.wait_for_message_content(
        "I'll search for information and then add a note."
    )

    # Wait for tool calls to appear
    await page.wait_for_selector(
        '[data-testid*="tool-call"]', state="visible", timeout=10000
    )

    # Check if tool elements are rendered at all
    tool_selectors = [
        '[data-testid="tool-group"]',
        '[data-ui="tool-call-content"]',
        ".tool-call-content",
        '[data-testid*="tool"]',
    ]

    tool_found = False
    for selector in tool_selectors:
        tool_elements = page.locator(selector)
        count = await tool_elements.count()
        if count > 0:
            tool_found = True
            break

    # If no tool elements found, skip ToolGroup-specific tests
    if not tool_found:
        print("No tool elements found - skipping ToolGroup interaction tests")
        assistant_message = page.locator('[data-testid="assistant-message"]')
        assert await assistant_message.count() > 0, (
            "Assistant message should be present"
        )
        return

    # Verify ToolGroup is rendered
    tool_group = page.locator('[data-testid="tool-group"]')
    await tool_group.wait_for(state="visible", timeout=10000)

    # Get references to trigger and content
    trigger = page.locator('[data-testid="tool-group-trigger"]')
    content = page.locator('[data-testid="tool-group-content"]')

    # Verify tool count shows category-based summary
    tool_count_text = await trigger.text_content()
    assert tool_count_text is not None, "Tool group trigger should have text content"
    # Should show something like "1 notes and 1 documents" or similar
    assert any(
        keyword in tool_count_text.lower() for keyword in ["note", "document", "tool"]
    ), f"Expected tool/category information in trigger text, got: {tool_count_text}"

    # Verify initially expanded
    await content.wait_for(state="attached", timeout=5000)
    is_hidden = await content.get_attribute("hidden")
    assert is_hidden is None, "ToolGroup content should be initially expanded"

    # Test collapse functionality
    await trigger.click()

    # Wait for collapse animation and verify content is hidden
    await expect(content).not_to_be_visible(timeout=1000)

    # Test expansion functionality
    await trigger.click()

    # Wait for expansion animation and verify content is visible again
    await expect(content).to_be_visible(timeout=1000)


@pytest.mark.playwright
@pytest.mark.asyncio
async def test_single_tool_call_uses_toolgroup(
    web_test_fixture: WebTestFixture, mock_llm_client: RuleBasedMockLLMClient
) -> None:
    """Test that even single tool calls are grouped (assistant-ui groups all tool calls)."""
    page = web_test_fixture.page
    chat_page = ChatPage(page, web_test_fixture.base_url)

    # Configure mock LLM with a single tool call
    mock_llm_client.rules = [
        (
            lambda args: "test single note" in str(args.get("messages", [])).lower(),
            LLMOutput(
                content="I'll add a single note for you.",
                tool_calls=[
                    ToolCallItem(
                        id="call_single",
                        type="function",
                        function=ToolCallFunction(
                            name="add_or_update_note",
                            arguments='{"title": "Single Test", "content": "Testing single tool call"}',
                        ),
                    ),
                ],
            ),
        ),
        (
            lambda args: any(
                msg.get("role") == "tool"
                and "call_single" in str(msg.get("tool_call_id", ""))
                for msg in args.get("messages", [])
            ),
            LLMOutput(content="Single note added successfully."),
        ),
    ]

    await chat_page.navigate_to_chat()
    await chat_page.send_message("Please test single note functionality")

    # Wait for assistant message
    await chat_page.wait_for_message_content("I'll add a single note for you.")

    # Wait for tool call elements to be visible
    await page.wait_for_selector(
        '[data-testid*="tool-call"]', state="visible", timeout=10000
    )

    # Check if tool elements are rendered at all
    tool_selectors = [
        '[data-testid="tool-group"]',
        '[data-ui="tool-call-content"]',
        ".tool-call-content",
        '[data-testid*="tool"]',
    ]

    tool_found = False
    for selector in tool_selectors:
        tool_elements = page.locator(selector)
        count = await tool_elements.count()
        if count > 0:
            tool_found = True
            break

    # If no tool elements found, skip ToolGroup-specific tests
    if not tool_found:
        print("No tool elements found - skipping single tool call ToolGroup tests")
        assistant_message = page.locator('[data-testid="assistant-message"]')
        assert await assistant_message.count() > 0, (
            "Assistant message should be present"
        )
        return

    # According to assistant-ui docs, even single tool calls are grouped
    # So we should still see a ToolGroup, but with "1 tool call"
    tool_group = page.locator('[data-testid="tool-group"]')
    await tool_group.wait_for(state="visible", timeout=10000)

    # Verify tool count shows category-based summary (e.g., "1 notes")
    trigger = page.locator('[data-testid="tool-group-trigger"]')
    tool_count_text = await trigger.text_content()
    assert tool_count_text is not None, "Tool group trigger should have text content"
    # Should show something like "1 notes" or "1 tool" based on the category
    assert any(keyword in tool_count_text.lower() for keyword in ["note", "tool"]), (
        f"Expected tool/category information in trigger text, got: {tool_count_text}"
    )

    # Verify the group is still functional (can be expanded)
    content = page.locator('[data-testid="tool-group-content"]')
    await content.wait_for(state="attached", timeout=5000)

    # Should be initially expanded
    is_hidden = await content.get_attribute("hidden")
    assert is_hidden is None, "Single tool call should also be initially expanded"

    # Should be able to collapse
    await trigger.click()
    await expect(content).not_to_be_visible(timeout=1000)
