"""Chat Page Object Model for Playwright tests."""

import contextlib
import time
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

        # Wait for the chat app to fully load
        # The default wait_for_load only waits for DOM, not JavaScript modules
        await self.wait_for_load(wait_for_network=True)

        # Also wait for the chat interface to be ready
        await self.page.wait_for_selector(
            self.CHAT_INPUT, state="visible", timeout=10000
        )

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

    async def get_last_assistant_message(self, timeout: int = 15000) -> str:
        """Get the text content of the last assistant message, waiting for it to stabilize."""
        await self.page.wait_for_selector(
            self.MESSAGE_ASSISTANT, state="visible", timeout=timeout
        )

        # This JS function polls the last assistant message until its content
        # has not changed for a certain period (e.g., 500ms), indicating stability.
        js_function = f"""
        () => {{
            return new Promise((resolve, reject) => {{
                const assistantMessages = document.querySelectorAll('{self.MESSAGE_ASSISTANT_CONTENT}');
                if (assistantMessages.length === 0) {{
                    return resolve(null);
                }}

                const lastMessage = assistantMessages[assistantMessages.length - 1];
                let lastText = lastMessage.innerText;
                let stableTime = 0;
                const stabilityThreshold = 1000; // ms - increased for better stability
                const pollInterval = 100; // ms
                const timeoutLimit = {timeout}; // ms

                const poller = setInterval(() => {{
                    const newText = lastMessage.innerText || lastMessage.textContent || '';
                    if (newText === lastText) {{
                        stableTime += pollInterval;
                        if (stableTime >= stabilityThreshold) {{
                            clearInterval(poller);
                            // Use textContent as fallback if innerText seems incomplete
                            const finalText = newText.length > 10 ? newText : (lastMessage.textContent || newText);
                            resolve(finalText);
                        }}
                    }} else {{
                        lastText = newText;
                        stableTime = 0;
                    }}
                }}, pollInterval);

                setTimeout(() => {{
                    clearInterval(poller);
                    reject(new Error(`Timeout: Assistant message did not stabilize within ${{timeout}}ms. Last content: "${{lastText}}"`));
                }}, timeoutLimit);
            }});
        }}"""

        try:
            handle = await self.page.evaluate_handle(js_function)
            content = await handle.json_value()
            return content or ""
        except Exception as e:
            print(f"DEBUG: Error waiting for stable assistant message: {e}")
            # Fallback for debugging
            assistant_messages = await self.page.query_selector_all(
                self.MESSAGE_ASSISTANT_CONTENT
            )
            if assistant_messages:
                return await assistant_messages[-1].inner_text()
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
            timeout=5000,
        )

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
        # TODO: There's a known issue where the chat input remains disabled after streaming completes.
        # This appears to be related to the assistant-ui library's runtime state management.
        # For now, we'll just wait for messages to appear and give a delay for streaming to complete.

        # Wait for at least one assistant message to appear
        await self.page.wait_for_selector(
            self.MESSAGE_ASSISTANT, state="visible", timeout=timeout
        )

        # Give time for streaming to complete and UI to stabilize
        await self.page.wait_for_timeout(2000)

        # Also, wait for any loading indicators to disappear
        with contextlib.suppress(Exception):
            await self.page.wait_for_selector(
                self.LOADING_INDICATOR, state="hidden", timeout=1000
            )

    async def wait_for_conversation_saved(self, timeout: int = 20000) -> None:
        """Wait for conversation to be saved to backend by checking conversation list."""
        start_time = time.time()
        current_conv_id = await self.get_current_conversation_id()

        while time.time() - start_time < timeout / 1000:
            try:
                # Force a reload to ensure the conversation list is up-to-date
                await self.page.reload(wait_until="networkidle")

                conv_list = await self.get_conversation_list()
                if any(c["id"] == current_conv_id for c in conv_list):
                    return  # Conversation found
            except Exception as e:
                print(f"DEBUG: Error checking conversation list, will retry: {e}")

            await self.page.wait_for_timeout(2000)  # Wait longer between reloads

        raise TimeoutError(
            f"Conversation {current_conv_id} not saved within {timeout}ms"
        )

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
