# ruff: noqa
# pyright: reportMissingImports=false, reportUndefinedVariable=false
"""Negative test cases for no-strict-assistant-message-wait.

These examples SHOULD NOT trigger the conformance rule.
"""


async def test_wait_for_specific_message_content(chat_page):
    await chat_page.wait_for_message_content("I'll add several notes for you.")


async def test_wait_for_concrete_tool_ui(page):
    await page.wait_for_selector('[data-testid*="tool-call"]', state="visible")
    await page.locator('[data-testid="tool-group"]').wait_for(state="visible")


async def test_count_assistant_messages(page):
    assistant_message = page.locator('[data-testid="assistant-message"]')
    assert await assistant_message.count() > 0


async def test_deliberately_scoped_assistant_message_wait(page):
    await page.locator('[data-testid="assistant-message"]').first.wait_for(
        state="visible"
    )

    assistant_message = page.get_by_test_id("assistant-message").last
    await assistant_message.wait_for(state="visible")
