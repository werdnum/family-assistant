# ruff: noqa
# pyright: reportMissingImports=false, reportUndefinedVariable=false
"""Positive test cases for no-strict-assistant-message-wait.

These examples SHOULD trigger the conformance rule.
"""


async def test_direct_locator_wait(page):
    await page.locator('[data-testid="assistant-message"]').wait_for(
        state="visible", timeout=10000
    )


async def test_assigned_locator_wait(page):
    assistant_message = page.locator('[data-testid="assistant-message"]')
    await assistant_message.wait_for(state="visible", timeout=10000)


async def test_direct_get_by_test_id_wait(page):
    await page.get_by_test_id("assistant-message").wait_for(state="visible")


async def test_assigned_get_by_test_id_wait(page):
    assistant_message = page.get_by_test_id("assistant-message")
    await assistant_message.wait_for(state="visible")
