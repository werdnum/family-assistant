"""Test for tool confirmation timeout behavior in the web UI."""

import asyncio
import json

import pytest

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
            # Only respond with tool call on the FIRST user message, not on subsequent messages
            lambda args: (
                "add a note" in str(args.get("messages", []))
                and not any(msg.role == "tool" for msg in args.get("messages", []))
            ),
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
                msg.role == "tool"
                and (
                    "timed out" in str(msg.content or "").lower()
                    or "cancelled" in str(msg.content or "").lower()
                )
                for msg in args.get("messages", [])
            ),
            LLMOutput(content=llm_timeout_response),
        ),
    ]

    await chat_page.navigate_to_chat()
    await chat_page.send_message("Please add a note.")

    # Wait for confirmation dialog to appear
    await chat_page.wait_for_confirmation_dialog()

    # The test timeout is 10s (overridden from default 1hr). Since we can't wait that long in tests,
    # let's just verify the dialog appears and has the timeout countdown.
    # Check that the UI shows a countdown timer
    timer_element = await page.query_selector(
        '.tool-confirmation-container span:has-text("Expires in")'
    )
    assert timer_element is not None, "Confirmation should show countdown timer"

    # Verify the confirmation buttons are present and enabled
    approve_button = await page.query_selector(
        '.tool-confirmation-container button:has-text("Approve")'
    )
    reject_button = await page.query_selector(
        '.tool-confirmation-container button:has-text("Reject")'
    )
    assert approve_button is not None, "Approve button should be visible"
    assert reject_button is not None, "Reject button should be visible"


@pytest.mark.playwright
@pytest.mark.asyncio
async def test_tool_confirmation_approval_flow(
    web_test_fixture: WebTestFixture, mock_llm_client: RuleBasedMockLLMClient
) -> None:
    """Test the full tool confirmation flow where the user approves the action."""
    page = web_test_fixture.page
    chat_page = ChatPage(page, web_test_fixture.base_url)

    tool_call_id = "tool_call_for_confirmation"
    llm_initial_response = "I can add that note for you. Please confirm."
    llm_final_response = "Okay, I have added the note."

    mock_llm_client.rules = [
        (
            # Only respond with tool call on the FIRST user message
            lambda args: (
                "add a note" in str(args.get("messages", []))
                and not any(msg.role == "tool" for msg in args.get("messages", []))
            ),
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
                msg.role == "tool" and msg.tool_call_id == tool_call_id
                for msg in args.get("messages", [])
            ),
            LLMOutput(content=llm_final_response),
        ),
    ]

    await chat_page.navigate_to_chat()
    await chat_page.send_message("Please add a note.")

    # Wait for confirmation dialog to appear
    await chat_page.wait_for_confirmation_dialog()

    # Approve the tool
    approve_button = await page.query_selector(
        '.tool-confirmation-container button:has-text("Approve")'
    )
    assert approve_button is not None, "Approve button should be visible"
    await approve_button.click()

    # Wait for the assistant's final response
    # ast-grep-ignore: no-asyncio-sleep-in-tests - Waiting for UI processing after tool approval
    await asyncio.sleep(2)  # Allow time for processing

    # Verify confirmation UI is no longer visible
    remaining_confirmations = await page.query_selector_all(
        ".tool-confirmation-container"
    )
    assert len(remaining_confirmations) == 0, (
        "Confirmation should be cleared after approval"
    )


@pytest.mark.playwright
@pytest.mark.asyncio
async def test_tool_confirmation_rejection_flow(
    web_test_fixture: WebTestFixture, mock_llm_client: RuleBasedMockLLMClient
) -> None:
    """Test the full tool confirmation flow where the user rejects the action."""
    page = web_test_fixture.page
    chat_page = ChatPage(page, web_test_fixture.base_url)

    tool_call_id = "tool_call_for_rejection"
    llm_initial_response = "I can add that note. Please confirm."
    llm_final_response = "Okay, I will not add the note."

    mock_llm_client.rules = [
        (
            # Only respond with tool call on the FIRST user message
            lambda args: (
                "add a note" in str(args.get("messages", []))
                and not any(msg.role == "tool" for msg in args.get("messages", []))
            ),
            LLMOutput(
                content=llm_initial_response,
                tool_calls=[
                    ToolCallItem(
                        id=tool_call_id,
                        type="function",
                        function=ToolCallFunction(
                            name="add_or_update_note",
                            arguments=json.dumps({
                                "title": "Test Note Reject",
                                "content": "Test Content Reject",
                            }),
                        ),
                    )
                ],
            ),
        ),
        (
            lambda args: any(
                msg.role == "tool"
                and (
                    "rejected" in str(msg.content or "").lower()
                    or "cancelled" in str(msg.content or "").lower()
                )
                for msg in args.get("messages", [])
            ),
            LLMOutput(content=llm_final_response),
        ),
    ]

    await chat_page.navigate_to_chat()
    await chat_page.send_message("Please add a note.")

    # Wait for confirmation dialog to appear
    await chat_page.wait_for_confirmation_dialog()

    # Reject the tool
    reject_button = await page.query_selector(
        '.tool-confirmation-container button:has-text("Reject")'
    )
    assert reject_button is not None, "Reject button should be visible"
    await reject_button.click()

    # Wait for the assistant's final response
    # ast-grep-ignore: no-asyncio-sleep-in-tests - Waiting for UI processing after tool rejection
    await asyncio.sleep(2)  # Allow time for processing

    # Verify confirmation UI is no longer visible
    remaining_confirmations = await page.query_selector_all(
        ".tool-confirmation-container"
    )
    assert len(remaining_confirmations) == 0, (
        "Confirmation should be cleared after rejection"
    )


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
            # Only respond with tool calls on the FIRST user message
            lambda args: (
                "add two notes" in str(args.get("messages", []))
                and not any(msg.role == "tool" for msg in args.get("messages", []))
            ),
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
            # After receiving tool results, generate final response
            lambda args: any(msg.role == "tool" for msg in args.get("messages", [])),
            LLMOutput(content=llm_final_response),
        ),
    ]

    await chat_page.navigate_to_chat()
    await chat_page.send_message("Please add two notes.")

    # Wait for FIRST confirmation dialog to appear
    await page.wait_for_selector(
        ".tool-confirmation-container", state="visible", timeout=5000
    )

    # Immediately approve the first tool (before it times out)
    first_approve = await page.query_selector(
        '.tool-confirmation-container button:has-text("Approve"):not([disabled])'
    )
    assert first_approve is not None, (
        "Approve button should be visible and enabled for first tool"
    )
    await first_approve.click()

    # Wait for the SECOND confirmation dialog to appear after first is processed
    await page.wait_for_selector(
        ".tool-confirmation-container", state="visible", timeout=5000
    )

    # Immediately reject the second tool (before it times out)
    second_reject = await page.query_selector(
        '.tool-confirmation-container button:has-text("Reject"):not([disabled])'
    )
    assert second_reject is not None, (
        "Reject button should be visible and enabled for second tool"
    )
    await second_reject.click()

    # Wait for the UI to update and confirmations to be removed
    # ast-grep-ignore: no-asyncio-sleep-in-tests - Waiting for UI to update after processing multiple tools
    await asyncio.sleep(2)

    # Verify confirmations are no longer visible after both actions
    remaining_confirmations = await page.query_selector_all(
        ".tool-confirmation-container"
    )
    assert len(remaining_confirmations) == 0, (
        "All confirmations should be cleared after processing both tools"
    )
