"""Tests for tool call grouping and collapsible UI functionality."""

from collections.abc import Awaitable, Callable, Iterable, Mapping
from typing import Any

import pytest
from playwright.async_api import expect

from family_assistant.llm import LLMOutput, ToolCallFunction, ToolCallItem
from tests.functional.web.conftest import WebTestFixture
from tests.functional.web.pages.chat_page import ChatPage
from tests.mocks.mock_llm import RuleBasedMockLLMClient


def _has_tool_message(args: object) -> bool:
    if not isinstance(args, Mapping):
        return False

    messages = args.get("messages")
    if not isinstance(messages, Iterable) or isinstance(messages, str | bytes):
        return False

    return any(getattr(msg, "role", None) == "tool" for msg in messages)


@pytest.mark.playwright
@pytest.mark.asyncio
async def test_multiple_tool_calls_are_grouped(
    web_test_fixture: WebTestFixture,
    mock_llm_client: RuleBasedMockLLMClient,
    take_screenshot: Callable[[Any, str, str], Awaitable[None]],
) -> None:
    """Test that multiple consecutive tool calls are grouped in a collapsible section."""
    page = web_test_fixture.page
    chat_page = ChatPage(page, web_test_fixture.base_url)

    # Configure mock LLM to respond with multiple tool calls
    mock_llm_client.rules = [
        (
            lambda args: (
                "add multiple notes" in str(args.get("messages", [])).lower()
                and not _has_tool_message(args)
            ),
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
                msg.role == "tool" and "call_1" in str(msg.tool_call_id or "")
                for msg in args.get("messages", [])
            ),
            LLMOutput(content="Successfully added all three notes."),
        ),
        (
            lambda args: any(
                msg.role == "tool" and "call_2" in str(msg.tool_call_id or "")
                for msg in args.get("messages", [])
            ),
            LLMOutput(content="Successfully added all three notes."),
        ),
        (
            lambda args: any(
                msg.role == "tool" and "call_3" in str(msg.tool_call_id or "")
                for msg in args.get("messages", [])
            ),
            LLMOutput(content="Successfully added all three notes."),
        ),
    ]

    await chat_page.navigate_to_chat()
    await chat_page.send_message("Please add multiple notes for testing")

    # Wait for the assistant's response and tool calls
    await chat_page.wait_for_message_content("I'll add several notes for you.")
    await chat_page.wait_for_message_content(
        "Successfully added all three notes.", timeout=30000
    )

    # Wait for tool call summary or details to appear
    await chat_page.wait_for_tool_call_display(timeout=10000)

    tool_group = page.locator('[data-testid="tool-group"]')
    await tool_group.wait_for(state="visible", timeout=10000)

    trigger = page.locator('[data-testid="tool-group-trigger"]')
    await expect(trigger).to_contain_text("2 notes")
    await expect(trigger).to_contain_text("1 document")

    # Test that completed groups are initially collapsed so intermediate work stays compact
    content = page.locator('[data-testid="tool-group-content"]')
    await content.wait_for(state="attached", timeout=5000)

    await expect(content).not_to_be_visible(timeout=5000)

    for viewport in ["desktop", "mobile"]:
        await take_screenshot(page, "chat-tool-calls-collapsed", viewport)


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
            lambda args: (
                "search and add" in str(args.get("messages", [])).lower()
                and not _has_tool_message(args)
            ),
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
                msg.role == "tool" and "call_search" in str(msg.tool_call_id or "")
                for msg in args.get("messages", [])
            ),
            LLMOutput(content="Search completed and note added."),
        ),
        (
            lambda args: any(
                msg.role == "tool" and "call_note" in str(msg.tool_call_id or "")
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
    await chat_page.wait_for_message_content(
        "Search completed and note added.", timeout=30000
    )

    # Wait for tool calls to appear
    await chat_page.wait_for_tool_call_display(timeout=10000)

    tool_group = page.locator('[data-testid="tool-group"]')
    await tool_group.wait_for(state="visible", timeout=10000)

    trigger = page.locator('[data-testid="tool-group-trigger"]')
    content = page.locator('[data-testid="tool-group-content"]')

    await expect(trigger).to_contain_text("1 document")
    await expect(trigger).to_contain_text("1 note")

    # Verify initially collapsed
    await content.wait_for(state="attached", timeout=5000)
    await expect(content).not_to_be_visible(timeout=5000)

    # Test expansion functionality
    await trigger.click()

    await expect(content).to_be_visible()

    # Test collapse functionality
    await trigger.click()

    await expect(content).not_to_be_visible()


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
            lambda args: (
                "test single note" in str(args.get("messages", [])).lower()
                and not _has_tool_message(args)
            ),
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
                msg.role == "tool" and "call_single" in str(msg.tool_call_id or "")
                for msg in args.get("messages", [])
            ),
            LLMOutput(content="Single note added successfully."),
        ),
    ]

    await chat_page.navigate_to_chat()
    await chat_page.send_message("Please test single note functionality")

    # Wait for assistant message
    await chat_page.wait_for_message_content("I'll add a single note for you.")
    await chat_page.wait_for_message_content(
        "Single note added successfully.", timeout=30000
    )

    # Wait for tool call elements to be visible
    await chat_page.wait_for_tool_call_display(timeout=10000)

    tool_group = page.locator('[data-testid="tool-group"]')
    await tool_group.wait_for(state="visible", timeout=10000)

    trigger = page.locator('[data-testid="tool-group-trigger"]')
    # add_or_update_note asks for confirmation, which no one answers here, so
    # the call ends not run and the collapsed header says so.
    await expect(trigger).to_have_text("1 note · 1 didn't finish")

    # Verify the group is still functional (can be expanded)
    content = page.locator('[data-testid="tool-group-content"]')
    await content.wait_for(state="attached", timeout=5000)

    # Should be initially collapsed
    await expect(content).not_to_be_visible(timeout=5000)

    # Should be able to expand
    await trigger.click()
    await expect(content).to_be_visible()
