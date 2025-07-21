"""Page object for the message history UI."""

from .base_page import BasePage


class HistoryPage(BasePage):
    """Page object for interacting with the message history UI."""

    async def navigate(self) -> None:
        """Navigate to the history page."""
        await self.navigate_to("/history")
        await self.wait_for_load()

    async def get_conversation_count(self) -> int:
        """Get the number of conversations displayed."""
        # Wait for conversations to load - either we see conversations or empty state
        await self.page.wait_for_selector(
            ".conversation-group, text=No conversations found", timeout=5000
        )
        conversations = await self.page.locator(".conversation-group").all()
        return len(conversations)

    async def get_conversation_header(self, conversation_index: int) -> str:
        """Get the header text for a conversation by index."""
        conversations = self.page.locator(".conversation-group")
        conversation = conversations.nth(conversation_index)
        # Look for both h3 and the conversation ID display
        header = conversation.locator("h3").first
        header_text = await header.text_content()

        # Also get the conversation ID info
        conv_id_elem = conversation.locator(".text-gray-600").first
        if await conv_id_elem.count() > 0:
            conv_id_text = await conv_id_elem.text_content()
            return f"{header_text} {conv_id_text}"

        return header_text or ""

    async def get_turn_count(self, conversation_index: int = 0) -> int:
        """Get the number of turns in a conversation."""
        conversations = await self.page.locator(".conversation-group").all()
        if conversation_index >= len(conversations):
            raise IndexError(f"No conversation at index {conversation_index}")
        turns = await conversations[conversation_index].locator(".turn-group").all()
        return len(turns)

    async def get_message_text(
        self,
        conversation_index: int = 0,
        turn_index: int = 0,
        message_role: str = "user",
    ) -> str | None:
        """Get the content text of a specific message."""
        conversations = await self.page.locator(".chat-group").all()
        if conversation_index >= len(conversations):
            return None

        turns = await conversations[conversation_index].locator(".turn-group").all()
        if turn_index >= len(turns):
            return None

        message = (
            turns[turn_index]
            .locator(f".{message_role}-message .message-content-text")
            .first
        )
        if await message.count() == 0:
            return None

        return await message.text_content()

    async def expand_tool_calls(
        self, conversation_index: int = 0, turn_index: int = 0
    ) -> None:
        """Expand the tool calls details for a specific message."""
        conversations = await self.page.locator(".chat-group").all()
        if conversation_index >= len(conversations):
            return

        turns = await conversations[conversation_index].locator(".turn-group").all()
        if turn_index >= len(turns):
            return

        tool_details = turns[turn_index].locator(".tool-info-details summary").first
        if await tool_details.count() > 0:
            await tool_details.click()

    async def get_tool_call_count(
        self, conversation_index: int = 0, turn_index: int = 0
    ) -> int:
        """Get the number of tool calls in a message."""
        conversations = await self.page.locator(".chat-group").all()
        if conversation_index >= len(conversations):
            return 0

        turns = await conversations[conversation_index].locator(".turn-group").all()
        if turn_index >= len(turns):
            return 0

        tool_calls = await turns[turn_index].locator(".tool-call-item").all()
        return len(tool_calls)

    async def get_tool_call_function_name(
        self, conversation_index: int = 0, turn_index: int = 0, tool_index: int = 0
    ) -> str | None:
        """Get the function name of a specific tool call."""
        conversations = await self.page.locator(".chat-group").all()
        if conversation_index >= len(conversations):
            return None

        turns = await conversations[conversation_index].locator(".turn-group").all()
        if turn_index >= len(turns):
            return None

        tool_calls = await turns[turn_index].locator(".tool-call-item").all()
        if tool_index >= len(tool_calls):
            return None

        # Look for "Function: <name>" text
        text = await tool_calls[tool_index].text_content() or ""
        if "Function:" in text:
            return text.split("Function:")[1].split("\n")[0].strip()
        return None

    async def expand_trace(
        self, conversation_index: int = 0, turn_index: int = 0
    ) -> None:
        """Expand the full trace for a turn."""
        conversations = await self.page.locator(".chat-group").all()
        if conversation_index >= len(conversations):
            return

        turns = await conversations[conversation_index].locator(".turn-group").all()
        if turn_index >= len(turns):
            return

        trace_summary = turns[turn_index].locator(".turn-trace summary").first
        if await trace_summary.count() > 0:
            await trace_summary.click()

    async def get_trace_message_count(
        self, conversation_index: int = 0, turn_index: int = 0
    ) -> int:
        """Get the number of messages in the expanded trace."""
        conversations = await self.page.locator(".chat-group").all()
        if conversation_index >= len(conversations):
            return 0

        turns = await conversations[conversation_index].locator(".turn-group").all()
        if turn_index >= len(turns):
            return 0

        trace_messages = (
            await turns[turn_index].locator(".turn-trace-content .message").all()
        )
        return len(trace_messages)

    # Filter methods
    async def set_interface_type_filter(self, interface_type: str) -> None:
        """Set the interface type filter."""
        await self.page.select_option("#interface-type-filter", interface_type)

    async def set_conversation_filter(self, conversation_id: str) -> None:
        """Set the conversation ID filter."""
        await self.page.select_option("#conversation-filter", conversation_id)

    async def set_date_from_filter(self, date_string: str) -> None:
        """Set the date from filter. Format: YYYY-MM-DDTHH:MM"""
        await self.page.fill("#date-from-filter", date_string)

    async def set_date_to_filter(self, date_string: str) -> None:
        """Set the date to filter. Format: YYYY-MM-DDTHH:MM"""
        await self.page.fill("#date-to-filter", date_string)

    async def apply_filters(self) -> None:
        """Apply the current filters."""
        await self.page.click("button:has-text('Apply Filters')")
        await self.wait_for_load()

    async def clear_filters(self) -> None:
        """Clear all filters."""
        await self.page.click("a:has-text('Clear Filters')")
        await self.wait_for_load()

    async def get_filter_summary(self) -> str:
        """Get the filter summary text."""
        summary = self.page.locator(".tasks-summary p").first
        return await summary.text_content() or ""

    # Pagination methods
    async def has_pagination(self) -> bool:
        """Check if pagination is visible."""
        return await self.page.locator(".pagination").count() > 0

    async def get_current_page(self) -> int:
        """Get the current page number."""
        pagination = self.page.locator(".pagination span").nth(1)
        if await pagination.count() == 0:
            return 1
        text = await pagination.text_content() or ""
        # Extract current page from "Page X of Y"
        if "Page" in text:
            parts = text.split()
            if len(parts) >= 2:
                try:
                    return int(parts[1])
                except ValueError:
                    pass
        return 1

    async def go_to_next_page(self) -> None:
        """Navigate to the next page."""
        next_link = self.page.locator(".pagination a:has-text('Next')")
        if await next_link.count() > 0:
            await next_link.click()
            await self.wait_for_load()

    async def go_to_previous_page(self) -> None:
        """Navigate to the previous page."""
        prev_link = self.page.locator(".pagination a:has-text('Previous')")
        if await prev_link.count() > 0:
            await prev_link.click()
            await self.wait_for_load()

    async def check_empty_state(self) -> bool:
        """Check if the empty state message is displayed."""
        empty_message = self.page.locator("p:has-text('No message history found')")
        return await empty_message.count() > 0
