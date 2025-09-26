#!/usr/bin/env python3
"""
Simple test to check if tool calls appear in the UI with a basic tool.
"""

import asyncio
import json

import pytest

from family_assistant.llm import LLMOutput, ToolCallFunction, ToolCallItem
from tests.functional.web.conftest import WebTestFixture
from tests.functional.web.pages.chat_page import ChatPage
from tests.mocks.mock_llm import RuleBasedMockLLMClient


@pytest.mark.playwright
@pytest.mark.asyncio
async def test_simple_tool_call_ui(
    web_test_fixture: WebTestFixture, mock_llm_client: RuleBasedMockLLMClient
) -> None:
    """Test that ANY tool call UI appears - use a simple tool like add_or_update_note."""
    page = web_test_fixture.page
    chat_page = ChatPage(page, web_test_fixture.base_url)

    # Use a simple tool that shouldn't need special handling
    def should_trigger_note_tool(args: dict) -> bool:
        messages = args.get("messages", [])
        has_trigger = "add a note" in str(messages).lower()
        has_tool_results = any(msg.get("role") == "tool" for msg in messages)
        print(
            f"[SIMPLE DEBUG] Has trigger: {has_trigger}, Has tool results: {has_tool_results}"
        )
        return has_trigger and not has_tool_results

    mock_llm_client.rules = [
        (
            should_trigger_note_tool,
            LLMOutput(
                content="I'll add that note for you",
                tool_calls=[
                    ToolCallItem(
                        id="test_note_tool_call",
                        type="function",
                        function=ToolCallFunction(
                            name="add_or_update_note",
                            arguments=json.dumps({
                                "title": "Test Note",
                                "content": "This is a test note",
                            }),
                        ),
                    )
                ],
            ),
        ),
    ]

    await chat_page.navigate_to_chat()
    await chat_page.send_message("add a note")

    # Wait for any tool call elements to appear
    await asyncio.sleep(3)

    # Check for tool call elements
    tool_call_elements = await page.query_selector_all('[data-testid="tool-call"]')
    print(f"[SIMPLE DEBUG] Found {len(tool_call_elements)} tool call elements")

    # Also check for any elements containing tool text
    note_elements = await page.query_selector_all("text=add_or_update_note")
    print(
        f"[SIMPLE DEBUG] Found {len(note_elements)} elements with 'add_or_update_note' text"
    )

    # Check for assistant message elements
    assistant_elements = await page.query_selector_all(
        '[data-testid="assistant-message"]'
    )
    print(f"[SIMPLE DEBUG] Found {len(assistant_elements)} assistant message elements")

    # Get all testid elements
    all_testids = await page.evaluate("""
        () => {
            const elements = document.querySelectorAll('[data-testid]');
            return Array.from(elements).map(el => el.getAttribute('data-testid'));
        }
    """)
    print(f"[SIMPLE DEBUG] All data-testid values: {all_testids}")

    # This should pass if any tool UI is working
    assert len(tool_call_elements) > 0 or len(note_elements) > 0, (
        f"Should find tool call elements. Found testids: {all_testids}"
    )
