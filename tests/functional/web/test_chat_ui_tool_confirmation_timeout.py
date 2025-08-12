"""Test for tool confirmation timeout behavior in the web UI."""

import asyncio
import json

import pytest
from playwright.async_api import TimeoutError

from family_assistant.llm import LLMOutput, ToolCallFunction, ToolCallItem
from tests.functional.web.conftest import WebTestFixture
from tests.functional.web.pages.chat_page import ChatPage
from tests.mocks.mock_llm import RuleBasedMockLLMClient


@pytest.mark.playwright
@pytest.mark.asyncio
async def test_tool_confirmation_timeout_flow(
    web_test_fixture: WebTestFixture, mock_llm_client: RuleBasedMockLLMClient
) -> None:
    """Test that tool confirmation properly times out and doesn't execute the tool."""
    page = web_test_fixture.page
    chat_page = ChatPage(page, web_test_fixture.base_url)

    tool_call_id = "tool_call_for_timeout"
    llm_initial_response = "I can add that note for you. Please confirm."
    llm_timeout_response = "The confirmation request timed out. The note was not added."

    mock_llm_client.rules = [
        (
            lambda args: "add a note" in str(args.get("messages", [])),
            LLMOutput(
                content=llm_initial_response,
                tool_calls=[
                    ToolCallItem(
                        id=tool_call_id,
                        type="function",
                        function=ToolCallFunction(
                            name="add_or_update_note",
                            arguments=json.dumps({
                                "title": "Test Note",
                                "content": "Test Content",
                            }),
                        ),
                    )
                ],
            ),
        ),
        (
            lambda args: any(
                msg.get("role") == "tool"
                and "timed out" in str(msg.get("content", "")).lower()
                for msg in args.get("messages", [])
            ),
            LLMOutput(content=llm_timeout_response),
        ),
    ]

    await chat_page.navigate_to_chat()
    await chat_page.send_message("Please add a note.")

    # Wait for confirmation dialog to appear
    await chat_page.wait_for_confirmation_dialog()

    # Check that timer is displayed
    timer_element = await page.query_selector(
        '.tool-confirmation-container span:has-text("Expires in")'
    )
    assert timer_element is not None, "Timer should be displayed"

    # Wait for a short timeout (in test, we can override timeout to be shorter)
    # The test fixture should be configured with a short timeout for testing
    await asyncio.sleep(5)  # Wait for timeout

    # Check that the UI shows expired state
    expired_element = await page.query_selector(
        '.tool-confirmation-container span:has-text("Expired")'
    )
    if expired_element:
        # Confirmation has expired in UI
        pass
    else:
        # Confirmation might have been cleaned up - verify it's gone
        with pytest.raises(TimeoutError):
            await page.wait_for_selector(
                chat_page.CONFIRMATION_CONTAINER, state="visible", timeout=1000
            )

    # Verify that the tool was NOT executed
    # The LLM should receive a timeout message
    await chat_page.wait_for_assistant_response()
    final_message = await chat_page.get_last_assistant_message()
    assert "timed out" in final_message.lower()


@pytest.mark.playwright
@pytest.mark.asyncio
async def test_multiple_tool_confirmations(
    web_test_fixture: WebTestFixture, mock_llm_client: RuleBasedMockLLMClient
) -> None:
    """Test that multiple tools can have pending confirmations simultaneously."""
    page = web_test_fixture.page
    chat_page = ChatPage(page, web_test_fixture.base_url)

    tool_call_id_1 = "tool_call_1"
    tool_call_id_2 = "tool_call_2"
    llm_initial_response = "I'll add both notes for you. Please confirm each action."
    llm_final_response = "I've added the first note. The second note was rejected."

    mock_llm_client.rules = [
        (
            lambda args: "add two notes" in str(args.get("messages", [])),
            LLMOutput(
                content=llm_initial_response,
                tool_calls=[
                    ToolCallItem(
                        id=tool_call_id_1,
                        type="function",
                        function=ToolCallFunction(
                            name="add_or_update_note",
                            arguments=json.dumps({
                                "title": "First Note",
                                "content": "First Content",
                            }),
                        ),
                    ),
                    ToolCallItem(
                        id=tool_call_id_2,
                        type="function",
                        function=ToolCallFunction(
                            name="add_or_update_note",
                            arguments=json.dumps({
                                "title": "Second Note",
                                "content": "Second Content",
                            }),
                        ),
                    ),
                ],
            ),
        ),
        (
            lambda args: any(
                msg.get("role") == "tool" for msg in args.get("messages", [])
            ),
            LLMOutput(content=llm_final_response),
        ),
    ]

    await chat_page.navigate_to_chat()
    await chat_page.send_message("Please add two notes.")

    # Wait for confirmation dialogs to appear
    await page.wait_for_selector(
        ".tool-confirmation-container", state="visible", timeout=10000
    )

    # Check that we have two confirmation containers
    confirmation_containers = await page.query_selector_all(
        ".tool-confirmation-container"
    )
    assert len(confirmation_containers) == 2, (
        f"Expected 2 confirmation containers, got {len(confirmation_containers)}"
    )

    # Approve the first one
    first_approve = await confirmation_containers[0].query_selector(
        'button:has-text("Approve")'
    )
    if first_approve:
        await first_approve.click()

    # Reject the second one
    second_reject = await confirmation_containers[1].query_selector(
        'button:has-text("Reject")'
    )
    if second_reject:
        await second_reject.click()

    # Wait for final response
    await chat_page.wait_for_assistant_response()
    final_message = await chat_page.get_last_assistant_message()

    # Verify the message indicates mixed results
    assert "first note" in final_message.lower() or "rejected" in final_message.lower()
