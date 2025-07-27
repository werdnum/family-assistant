"""Chat Page Object Model for Playwright tests."""

from typing import Any

from .base_page import BasePage


class ChatPage(BasePage):
    """Page object for chat-related functionality."""

    # Selectors
    SIDEBAR_TOGGLE = ".sidebar-toggle"
    CHAT_INPUT = '[data-testid="chat-input"]'
    SEND_BUTTON = '[data-testid="send-button"]'
    CONVERSATION_ITEM = ".conversation-item"
    NEW_CHAT_BUTTON = '[data-testid="new-chat-button"]'
    MESSAGE_USER = '[data-testid="user-message"]'
    MESSAGE_ASSISTANT = '[data-testid="assistant-message"]'
    MESSAGE_USER_CONTENT = '[data-testid="user-message-content"]'
    MESSAGE_ASSISTANT_CONTENT = '[data-testid="assistant-message-content"]'
    MESSAGE_TOOL_CALL = '[data-ui="tool-call-content"], .tool-call-content'
    THREAD_MESSAGES = ".thread-messages"
    CONVERSATION_TITLE = ".conversation-title"
    CONVERSATION_PREVIEW = ".conversation-preview"
    SIDEBAR = ".conversation-sidebar"
    SIDEBAR_OVERLAY = ".sidebar-overlay"
    CHAT_CONTAINER = ".chat-container"
    LOADING_INDICATOR = ".thread-loading"

    async def navigate_to_chat(self, conversation_id: str | None = None) -> None:
        """Navigate to the chat page."""
        if conversation_id:
            await self.navigate_to(f"/chat?conversation_id={conversation_id}")
        else:
            await self.navigate_to("/chat")
        await self.wait_for_load()

    async def send_message(self, message: str) -> None:
        """Send a message in the chat.

        Args:
            message: The message to send
        """
        # Wait for the chat input to be available
        chat_input = await self.page.wait_for_selector(self.CHAT_INPUT, state="visible")
        if chat_input:
            await chat_input.fill(message)

        # Click the send button
        send_button = await self.page.wait_for_selector(
            self.SEND_BUTTON, state="visible"
        )
        if send_button:
            await send_button.click()

        # Wait for the user message to appear
        await self.page.wait_for_selector(
            self.MESSAGE_USER, state="visible", timeout=5000
        )

        # Wait for assistant response to start
        await self.page.wait_for_selector(
            self.MESSAGE_ASSISTANT, state="visible", timeout=30000
        )

    async def get_last_assistant_message(self) -> str:
        """Get the text content of the last assistant message."""
        await self.page.wait_for_selector(self.MESSAGE_ASSISTANT, state="visible")
        # Get all assistant messages
        assistant_messages = await self.page.query_selector_all(self.MESSAGE_ASSISTANT)
        if assistant_messages:
            # Get the last one
            last_message = assistant_messages[-1]
            # Find the content element within it
            content_elem = await last_message.query_selector(
                self.MESSAGE_ASSISTANT_CONTENT
            )
            if content_elem:
                return await content_elem.text_content() or ""
        return ""

    async def get_all_messages(self) -> list[dict[str, str]]:
        """Get all messages in the conversation.

        Returns:
            List of dictionaries with 'role' and 'content' keys
        """
        messages = []

        # Get all message elements in order
        all_messages = await self.page.query_selector_all(
            f"{self.MESSAGE_USER}, {self.MESSAGE_ASSISTANT}"
        )

        for msg in all_messages:
            # Check if it's a user or assistant message
            is_user = await msg.get_attribute("data-testid") == "user-message"
            role = "user" if is_user else "assistant"

            # Try multiple selectors to find content
            content = ""

            if is_user:
                # For user messages, try the specific content selector first
                content_elem = await msg.query_selector(self.MESSAGE_USER_CONTENT)
                if content_elem:
                    content = await content_elem.text_content() or ""
                else:
                    # Fallback: try to get text from the bubble div
                    bubble_elem = await msg.query_selector(".user-bubble")
                    if bubble_elem:
                        content = await bubble_elem.text_content() or ""
            else:
                # For assistant messages, try the specific content selector first
                content_elem = await msg.query_selector(self.MESSAGE_ASSISTANT_CONTENT)
                if content_elem:
                    content = await content_elem.text_content() or ""
                else:
                    # Fallback: try to get text from the bubble div
                    bubble_elem = await msg.query_selector(".assistant-bubble")
                    if bubble_elem:
                        content = await bubble_elem.text_content() or ""

            messages.append({"role": role, "content": content.strip()})

        return messages

    async def toggle_sidebar(self) -> None:
        """Toggle the conversation sidebar."""
        toggle_button = await self.page.wait_for_selector(self.SIDEBAR_TOGGLE)
        if toggle_button:
            await toggle_button.click()
        await self.page.wait_for_timeout(300)  # Wait for animation

    async def is_sidebar_open(self) -> bool:
        """Check if the sidebar is open."""
        sidebar = await self.page.query_selector(self.SIDEBAR)
        if sidebar:
            # Check if the parent has 'with-sidebar' class
            parent = await self.page.query_selector(".chat-app-wrapper.with-sidebar")
            return parent is not None
        return False

    async def create_new_chat(self) -> None:
        """Create a new chat conversation."""
        # Ensure sidebar is open
        if not await self.is_sidebar_open():
            await self.toggle_sidebar()

        new_chat_button = await self.page.wait_for_selector(self.NEW_CHAT_BUTTON)
        if new_chat_button:
            await new_chat_button.click()
        await self.wait_for_load()

    async def get_conversation_list(self) -> list[dict[str, Any]]:
        """Get the list of conversations from the sidebar.

        Returns:
            List of conversation info dicts with 'id', 'title', and 'preview' keys
        """
        # Ensure sidebar is open
        if not await self.is_sidebar_open():
            await self.toggle_sidebar()

        conversations = []
        items = await self.page.query_selector_all(self.CONVERSATION_ITEM)

        for item in items:
            conv_id = await item.get_attribute("data-conversation-id") or ""
            title_elem = await item.query_selector(self.CONVERSATION_TITLE)
            preview_elem = await item.query_selector(self.CONVERSATION_PREVIEW)

            title = (await title_elem.text_content() if title_elem else "") or ""
            preview = (await preview_elem.text_content() if preview_elem else "") or ""

            conversations.append({
                "id": conv_id,
                "title": title.strip() if title else "",
                "preview": preview.strip() if preview else "",
            })

        return conversations

    async def select_conversation(self, conversation_id: str) -> None:
        """Select a conversation from the sidebar.

        Args:
            conversation_id: The ID of the conversation to select
        """
        # Ensure sidebar is open
        if not await self.is_sidebar_open():
            await self.toggle_sidebar()

        # Click on the conversation item
        selector = f'{self.CONVERSATION_ITEM}[data-conversation-id="{conversation_id}"]'
        conv_item = await self.page.wait_for_selector(selector)
        if conv_item:
            await conv_item.click()

        # Wait for the conversation to load
        await self.wait_for_load()
        # Wait for URL to update with new conversation ID
        await self.page.wait_for_function(
            f"window.location.href.includes('conversation_id={conversation_id}')",
            timeout=5000
        )

    async def refresh_conversations(self) -> None:
        """Refresh the conversation list."""
        # The UI should auto-refresh, so just wait a bit
        await self.page.wait_for_timeout(1000)

    async def wait_for_tool_call_display(self) -> None:
        """Wait for tool call content to be displayed."""
        await self.page.wait_for_selector(
            self.MESSAGE_TOOL_CALL, state="visible", timeout=10000
        )

    async def get_tool_calls(self) -> list[dict[str, Any]]:
        """Get information about tool calls displayed in the conversation.

        Returns:
            List of tool call information
        """
        tool_calls = []
        tool_elements = await self.page.query_selector_all(self.MESSAGE_TOOL_CALL)

        for elem in tool_elements:
            # Extract tool name and result from the assistant-ui component
            text = await elem.text_content() or ""
            tool_calls.append({"display_text": text.strip()})

        return tool_calls

    async def is_chat_input_enabled(self) -> bool:
        """Check if the chat input is enabled and ready for input."""
        try:
            # Wait for chat input to be visible first
            chat_input = await self.page.wait_for_selector(
                self.CHAT_INPUT, state="visible", timeout=5000
            )
            if chat_input:
                return not await chat_input.is_disabled()
        except Exception:
            pass
        return False

    async def get_current_conversation_id(self) -> str | None:
        """Get the current conversation ID from the URL."""
        url = self.page.url
        if "conversation_id=" in url:
            return url.split("conversation_id=")[-1].split("&")[0]
        return None

    async def wait_for_assistant_response(self, timeout: int = 30000) -> None:
        """Wait for assistant to complete responding.

        Args:
            timeout: Maximum time to wait in milliseconds
        """
        # Wait for assistant message to appear
        await self.page.wait_for_selector(
            self.MESSAGE_ASSISTANT, state="visible", timeout=timeout
        )
        # Wait for any loading indicators to disappear
        await self.page.wait_for_selector(
            self.LOADING_INDICATOR, state="hidden", timeout=timeout
        )
        # Give a tiny buffer for DOM updates
        await self.page.wait_for_timeout(100)

    async def wait_for_conversation_saved(self, timeout: int = 5000) -> None:
        """Wait for conversation to be saved to backend.

        Args:
            timeout: Maximum time to wait in milliseconds
        """
        # Wait for network idle state indicating save completed
        await self.page.wait_for_load_state("networkidle", timeout=timeout)

    async def wait_for_message_count(
        self, expected_count: int, role: str | None = None, timeout: int = 10000
    ) -> None:
        """Wait for a specific number of messages to appear.

        Args:
            expected_count: Expected number of messages
            role: Optional role filter ('user' or 'assistant')
            timeout: Maximum time to wait in milliseconds
        """
        if role:
            selector = self.MESSAGE_USER if role == "user" else self.MESSAGE_ASSISTANT
        else:
            selector = f"{self.MESSAGE_USER}, {self.MESSAGE_ASSISTANT}"

        await self.page.wait_for_function(
            f"document.querySelectorAll('{selector}').length >= {expected_count}",
            timeout=timeout,
        )
