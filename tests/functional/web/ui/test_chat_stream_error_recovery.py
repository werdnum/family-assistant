"""Playwright regressions for chat stream recovery after tool activation."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
from playwright.async_api import expect

from family_assistant.llm import LLMStreamEvent, ToolCallFunction, ToolCallItem
from family_assistant.processing import ProcessingService
from family_assistant.tools import OnDemandToolsView
from tests.functional.web.pages.chat_page import ChatPage

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    from tests.functional.web.conftest import WebTestFixture
    from tests.mocks.mock_llm import RuleBasedMockLLMClient


def _enable_search_documents_on_demand(service: ProcessingService) -> None:
    """Expose activate_tools in this test without changing global test config."""
    service.on_demand_view = OnDemandToolsView(
        wrapped_provider=service.tools_provider,
        on_demand_tool_names={"search_documents"},
    )


@pytest.mark.playwright
@pytest.mark.asyncio
async def test_refresh_recovers_after_activate_tools_stream_error(
    web_test_fixture: WebTestFixture,
    mock_llm_client: RuleBasedMockLLMClient,
) -> None:
    """A post-activate_tools stream error must not leave the chat stuck after refresh."""
    service = web_test_fixture.assistant.default_processing_service
    assert isinstance(service, ProcessingService)
    _enable_search_documents_on_demand(service)

    tool_call_id = "call_activate_tools_recovery"
    user_prompt = "Activate tools then hit a stream failure"

    def generate_response_stream(
        *,
        messages: list[object],
        tools: list[dict[str, object]] | None = None,
        tool_choice: str | None = "auto",
    ) -> AsyncIterator[LLMStreamEvent]:
        async def _stream() -> AsyncIterator[LLMStreamEvent]:
            has_tool_result = any(
                getattr(message, "role", None) == "tool"
                and getattr(message, "tool_call_id", None) == tool_call_id
                for message in messages
            )
            if has_tool_result:
                raise RuntimeError("simulated provider failure after activate_tools")

            tool_names: set[str] = set()
            for definition in tools or []:
                function_definition = definition.get("function")
                if not isinstance(function_definition, dict):
                    continue
                name = function_definition.get("name")
                if isinstance(name, str):
                    tool_names.add(name)
            assert "activate_tools" in tool_names
            yield LLMStreamEvent(
                type="tool_call",
                tool_call=ToolCallItem(
                    id=tool_call_id,
                    type="function",
                    function=ToolCallFunction(
                        name="activate_tools",
                        arguments='{"tool_names":["search_documents"]}',
                    ),
                ),
            )
            yield LLMStreamEvent(type="done", metadata={})

        return _stream()

    mock_llm_client.generate_response_stream = generate_response_stream  # type: ignore[method-assign]

    page = web_test_fixture.page
    chat_page = ChatPage(page, web_test_fixture.base_url)

    async with page.expect_response(
        lambda response: "/stream" in response.url and response.ok,
        timeout=30000,
    ):
        await chat_page.navigate_to_chat()

    await chat_page.send_message(user_prompt)

    await expect(
        page.get_by_text("Sorry, I encountered an error after running tools.")
    ).to_be_visible(timeout=20000)
    await expect(page.get_by_test_id("send-button")).to_be_visible(timeout=5000)
    await expect(page.get_by_test_id("send-button")).to_be_enabled(timeout=5000)
    await expect(page.get_by_text("Stop generating")).to_have_count(0, timeout=5000)

    await page.reload()
    await page.wait_for_selector('[data-app-ready="true"]', timeout=10000)
    await page.wait_for_selector(ChatPage.CHAT_INPUT, state="visible", timeout=10000)

    await expect(
        page.get_by_text("I encountered an error while processing your message.")
    ).to_be_visible(timeout=10000)
    await expect(page.get_by_test_id("send-button")).to_be_enabled(timeout=5000)
    await expect(page.get_by_text("Stop generating")).to_have_count(0, timeout=5000)
