"""Chat Page Object Model for Playwright tests."""

import contextlib
import time
from typing import Any

from .base_page import BasePage


class ChatPage(BasePage):
    """Page object for chat-related functionality."""

    # Constants
    ASSISTANT_LOADING_PLACEHOLDER = "..."

    # Selectors - Updated for shadcn/ui components
    SIDEBAR_TOGGLE = "button[aria-label='Toggle sidebar']"
    CHAT_INPUT = '[data-testid="chat-input"]'
    SEND_BUTTON = '[data-testid="send-button"]'
    # Updated: Card components are used for conversation items with data-conversation-id
    CONVERSATION_ITEM = "[data-conversation-id]"
    NEW_CHAT_BUTTON = '[data-testid="new-chat-button"]'
    MESSAGE_USER = '[data-testid="user-message"]'
    MESSAGE_ASSISTANT = '[data-testid="assistant-message"]'
    MESSAGE_USER_CONTENT = '[data-testid="user-message-content"]'
    MESSAGE_ASSISTANT_CONTENT = '[data-testid="assistant-message-content"]'
    MESSAGE_TOOL_CALL = '[data-ui="tool-call-content"], .tool-call-content'
    THREAD_MESSAGES = ".thread-messages"
    CONVERSATION_TITLE = ".conversation-title"
    CONVERSATION_PREVIEW = ".conversation-preview"
    # Updated: Sidebar is now a div with specific classes
    SIDEBAR = "div.w-80.flex-shrink-0.border-r, div.h-full.w-80.flex-shrink-0.border-r"  # Desktop sidebar
    SIDEBAR_SHEET = '[role="dialog"][data-state]'  # Mobile sheet
    SIDEBAR_OVERLAY = ".fixed.inset-0.z-40"  # Mobile overlay
    CHAT_CONTAINER = ".flex.min-w-0.flex-1"  # Main content container
    LOADING_INDICATOR = ".animate-bounce"  # Loading dots animation
    # Updated for inline confirmation UI
    CONFIRMATION_CONTAINER = ".tool-confirmation-container"
    CONFIRMATION_PROMPT = ".tool-confirmation-container .prose"
    CONFIRMATION_APPROVE_BUTTON = (
        '.tool-confirmation-container button:has-text("Approve")'
    )
    CONFIRMATION_REJECT_BUTTON = (
        '.tool-confirmation-container button:has-text("Reject")'
    )

    async def navigate_to_chat(self, conversation_id: str | None = None) -> None:
        """Navigate to the chat page."""
        if conversation_id:
            await self.navigate_to(f"/chat?conversation_id={conversation_id}")
        else:
            await self.navigate_to("/chat")

        # Wait for the chat app to fully load
        # The default wait_for_load only waits for DOM, not JavaScript modules
        await self.wait_for_load(wait_for_network=True)

        # Wait for React app to initialize
        await self.page.wait_for_timeout(1000)

        # Also wait for the chat interface to be ready
        await self.page.wait_for_selector(
            self.CHAT_INPUT, state="visible", timeout=10000
        )

    async def send_message(self, message: str) -> None:
        """Send a message in the chat.

        Args:
            message: The message to send
        """
        # Wait for the chat input to be available and enabled
        chat_input = await self.page.wait_for_selector(self.CHAT_INPUT, state="visible")
        if not chat_input:
            raise RuntimeError("Chat input not found")

        # Focus the input
        await chat_input.click()

        # Type the message character by character
        await chat_input.type(message)

        # Small delay to ensure input is processed
        await self.page.wait_for_timeout(500)

        # Press Enter to send (this is more reliable than clicking the button)
        await chat_input.press("Enter")

        # Give time for the message to be processed and appear
        await self.page.wait_for_timeout(3000)

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
                const stabilityThreshold = 3000; // ms - increased for better stability with streaming responses
                const pollInterval = 100; // ms
                const timeoutLimit = {timeout}; // ms

                let waitingForContent = !lastText;
                
                const poller = setInterval(() => {{
                    // Check if it's showing the typing indicator
                    const typingIndicator = lastMessage.querySelector('.typing-indicator');
                    if (typingIndicator) {{
                        // Skip if still showing loading state
                        return;
                    }}
                    
                    // Try multiple ways to get the text content
                    const markdownEl = lastMessage.querySelector('.markdown-text');
                    let newText = '';
                    
                    // First try the markdown element
                    if (markdownEl) {{
                        newText = markdownEl.innerText || markdownEl.textContent || '';
                    }}
                    
                    // If no markdown element or empty, try the parent
                    if (!newText || newText.trim() === '') {{
                        newText = lastMessage.innerText || lastMessage.textContent || '';
                    }}
                    
                    // If we're waiting for initial content, keep polling
                    if (waitingForContent && newText && newText.trim()) {{
                        waitingForContent = false;
                        lastText = newText;
                        stableTime = 0;
                    }}
                    // Otherwise check for stability
                    else if (newText === lastText && newText.length > 0) {{
                        stableTime += pollInterval;
                        if (stableTime >= stabilityThreshold) {{
                            clearInterval(poller);
                            resolve(newText.trim());
                        }}
                    }} else if (newText && newText !== lastText) {{
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
                    # Check if it's showing the typing indicator
                    typing_indicator = await content_elem.query_selector(
                        ".typing-indicator"
                    )
                    if typing_indicator:
                        content = "..."  # Represent loading state as dots
                    else:
                        # Check if there's a markdown element inside
                        markdown_elem = await content_elem.query_selector(
                            ".markdown-text"
                        )
                        if markdown_elem:
                            # For markdown elements, we need to get all text including from nested elements
                            # ReactMarkdown can create multiple paragraph/element children

                            # Try multiple methods to get complete text
                            # Method 1: Get all text nodes directly
                            all_text = await markdown_elem.evaluate(
                                "el => el.textContent || el.innerText || ''"
                            )

                            if all_text:
                                content = all_text
                            else:
                                # Method 2: Try getting paragraph text
                                paragraphs = await markdown_elem.query_selector_all("p")
                                if paragraphs:
                                    content_parts = []
                                    for p in paragraphs:
                                        p_text = await p.text_content() or ""
                                        if p_text:
                                            content_parts.append(p_text)
                                    content = " ".join(content_parts)
                                else:
                                    # Method 3: Fallback to text_content
                                    content = await markdown_elem.text_content() or ""
                        else:
                            content = await content_elem.text_content() or ""

                        # Don't return LOADING_MARKER as content
                        if content == "___LOADING___":
                            content = "..."
                else:
                    # Fallback: try to get text from the bubble div
                    bubble_elem = await msg.query_selector(".assistant-bubble")
                    if bubble_elem:
                        content = await bubble_elem.text_content() or ""

            messages.append({"role": role, "content": content.strip()})

        return messages

    async def toggle_sidebar(self) -> None:
        """Toggle the conversation sidebar."""
        # The toggle button is now always visible (removed lg:hidden)
        # Try the aria-label selector first
        toggle_button = await self.page.query_selector(self.SIDEBAR_TOGGLE)
        if not toggle_button:
            # Fallback to the hamburger text
            toggle_button = await self.page.query_selector("button:has-text('☰')")

        if toggle_button:
            await toggle_button.click()
        else:
            raise RuntimeError("Could not find sidebar toggle button")

        # Wait longer for Sheet animation on mobile
        viewport_size = self.page.viewport_size
        if viewport_size and viewport_size["width"] <= 768:
            await self.page.wait_for_timeout(
                1500
            )  # Longer wait for mobile Sheet animation
        else:
            await self.page.wait_for_timeout(300)  # Standard wait for desktop

    async def is_sidebar_open(self) -> bool:
        """Check if the sidebar is open."""
        # Check viewport width to determine if we're in mobile or desktop mode
        viewport_size = self.page.viewport_size
        if viewport_size and viewport_size["width"] <= 768:
            # Mobile: Check for Sheet dialog state
            # The Sheet component from shadcn/ui uses data-state="open" or data-state="closed"
            sheet = await self.page.query_selector('[data-state="open"][role="dialog"]')
            if sheet:
                # If we found a sheet with data-state="open", check if it contains sidebar content
                sidebar_content = await sheet.query_selector(
                    '[data-testid="new-chat-button"]'
                )
                return sidebar_content is not None
            return False
        else:
            # Desktop: Check if sidebar is visible by looking for its presence and checking the margin class
            # Try multiple selectors for the sidebar
            sidebar = await self.page.query_selector("div.w-80.flex-shrink-0.border-r")
            if not sidebar:
                sidebar = await self.page.query_selector(
                    "div.h-full.w-80.flex-shrink-0.border-r"
                )

            if sidebar:
                # Check the classes to see if it's visible (ml-0) or hidden (-ml-80)
                classes = await sidebar.get_attribute("class") or ""
                # If ml-0 is present, it's open. If -ml-80 is present, it's closed.
                # If neither is present, assume it's open (default state)
                return "-ml-80" not in classes
            return False

    async def create_new_chat(self) -> None:
        """Create a new chat conversation."""
        # Ensure sidebar is open
        if not await self.is_sidebar_open():
            await self.toggle_sidebar()

        # Get current conversation ID before creating new chat
        current_url = self.page.url

        new_chat_button = await self.page.wait_for_selector(self.NEW_CHAT_BUTTON)
        if new_chat_button:
            await new_chat_button.click()

        # Wait for URL to change to new conversation
        await self.page.wait_for_function(
            f"window.location.href !== '{current_url}'", timeout=5000
        )

        await self.wait_for_load()
        # Additional small wait to ensure React state is fully updated
        await self.page.wait_for_timeout(200)

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
            # In the new UI, the preview text is directly in the card, not in separate elements
            # The first text element is the message preview
            text_content = await item.text_content() or ""
            lines = text_content.strip().split("\n")
            # First line is the message preview, rest is metadata
            preview = lines[0] if lines else ""

            conversations.append({
                "id": conv_id,
                "title": "",  # No separate title in new UI
                "preview": preview.strip(),
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

        # Click on the conversation item - use the data-conversation-id attribute
        selector = f'[data-conversation-id="{conversation_id}"]'
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
            conv_id = url.split("conversation_id=")[-1].split("&")[0]
            return conv_id if conv_id else None
        # Try to get it from localStorage as a fallback
        try:
            conv_id = await self.page.evaluate(
                "localStorage.getItem('lastConversationId')"
            )
            return conv_id
        except Exception:
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
        try:
            await self.page.wait_for_selector(
                self.MESSAGE_ASSISTANT, state="visible", timeout=timeout
            )
        except Exception:
            # Fallback: wait for any assistant message element
            await self.page.wait_for_selector(
                "div[data-testid='assistant-message'], div.flex.items-start.gap-3",
                state="visible",
                timeout=timeout,
            )

        # Give time for streaming to complete and UI to stabilize
        await self.page.wait_for_timeout(2000)

        # Also, wait for any loading indicators to disappear
        with contextlib.suppress(Exception):
            await self.page.wait_for_selector(
                self.LOADING_INDICATOR, state="hidden", timeout=1000
            )

    async def wait_for_streaming_complete(self, timeout: int = 10000) -> None:
        """Wait for any active streaming response to complete.

        This checks for the absence of loading indicators and ensures
        the assistant message is no longer updating.
        """
        # Wait for no typing indicator
        await self.page.wait_for_function(
            """() => {
                const typingIndicator = document.querySelector('.typing-indicator');
                return !typingIndicator;
            }""",
            timeout=timeout,
        )

        # Get the last assistant message content and wait for it to stabilize
        start_time = time.time()
        last_content = ""
        stable_count = 0

        while stable_count < 3:  # Need 3 consecutive checks with same content
            # Check for timeout
            if (time.time() - start_time) * 1000 > timeout:
                raise TimeoutError(
                    f"Timeout waiting for streaming to complete after {timeout}ms. "
                    f"Last content: {last_content}"
                )

            await self.page.wait_for_timeout(200)
            messages = await self.get_all_messages()
            if messages and messages[-1]["role"] == "assistant":
                current_content = messages[-1].get("content", "")
                if (
                    current_content == last_content
                    and current_content != self.ASSISTANT_LOADING_PLACEHOLDER
                ):
                    stable_count += 1
                else:
                    stable_count = 0
                    last_content = current_content
            else:
                break

    async def wait_for_conversation_saved(self, timeout: int = 20000) -> None:
        """Wait for conversation to be saved to backend by checking conversation list."""
        start_time = time.time()
        current_conv_id = await self.get_current_conversation_id()

        # First ensure streaming is complete before checking if saved
        # This prevents the reload from interrupting message processing
        await self.wait_for_streaming_complete(timeout=10000)

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

    async def wait_for_message_content(
        self, expected_text: str, role: str = "assistant", timeout: int = 10000
    ) -> None:
        """Wait for a message with specific text content to appear and stabilize.

        Args:
            expected_text: Text to look for in the message
            role: Message role to check ('user' or 'assistant')
            timeout: Maximum time to wait in milliseconds
        """
        selector = self.MESSAGE_USER if role == "user" else self.MESSAGE_ASSISTANT
        content_selector = (
            self.MESSAGE_USER_CONTENT
            if role == "user"
            else self.MESSAGE_ASSISTANT_CONTENT
        )

        # JavaScript function that accepts parameters safely
        js_function = """
        ([expectedText, selector, contentSelector, timeout]) => {
            return new Promise((resolve, reject) => {
                const startTime = Date.now();
                
                const checkForContent = () => {
                    const messages = document.querySelectorAll(selector);
                    
                    for (const msg of messages) {
                        const contentElem = msg.querySelector(contentSelector);
                        if (!contentElem) continue;
                        
                        // Skip if showing typing indicator
                        const typingIndicator = contentElem.querySelector('.typing-indicator');
                        if (typingIndicator) continue;
                        
                        // Get text content
                        let text = '';
                        const markdownElem = contentElem.querySelector('.markdown-text');
                        if (markdownElem) {
                            text = markdownElem.innerText || markdownElem.textContent || '';
                        } else {
                            text = contentElem.innerText || contentElem.textContent || '';
                        }
                        
                        // Check if text contains expected content
                        if (text && text.includes(expectedText)) {
                            return resolve(true);
                        }
                    }
                    
                    // Check timeout
                    if (Date.now() - startTime > timeout) {
                        return reject(new Error('Timeout waiting for message content: ' + expectedText));
                    }
                    
                    // Check again
                    setTimeout(checkForContent, 100);
                };
                
                checkForContent();
            });
        }"""

        # Pass parameters safely to avoid injection
        await self.page.evaluate(
            js_function, [expected_text, selector, content_selector, timeout]
        )

    async def wait_for_messages_with_content(
        self, expected_contents: dict[str, str], timeout: int = 30000
    ) -> None:
        """Wait for messages to contain expected content, polling until satisfied.

        Args:
            expected_contents: Dict mapping role to expected text content
            timeout: Maximum time to wait in milliseconds (default increased to 30s)
        """
        start_time = time.time()

        while (time.time() - start_time) * 1000 < timeout:
            messages = await self.get_all_messages()

            # Check if all expected content is present
            all_found = True
            for role, expected_text in expected_contents.items():
                role_messages = [m for m in messages if m["role"] == role]
                if not role_messages:
                    all_found = False
                    break

                # Check if any message of this role contains the expected text
                found = any(
                    expected_text in msg.get("content", "") for msg in role_messages
                )
                if not found:
                    all_found = False
                    break

            if all_found:
                return  # Success!

            # Wait a bit before next check
            await self.page.wait_for_timeout(100)

        # If we get here, timeout occurred
        messages = await self.get_all_messages()
        raise TimeoutError(
            f"Timeout waiting for expected content. Got messages: {messages}"
        )

    async def wait_for_confirmation_dialog(self, timeout: int = 10000) -> None:
        """Wait for the inline tool confirmation UI to appear."""
        await self.page.wait_for_selector(
            self.CONFIRMATION_CONTAINER, state="visible", timeout=timeout
        )

    async def get_confirmation_prompt(self) -> str:
        """Get the text from the tool confirmation prompt."""
        prompt_element = await self.page.query_selector(self.CONFIRMATION_PROMPT)
        if not prompt_element:
            raise RuntimeError("Tool confirmation prompt not found")
        return await prompt_element.text_content() or ""

    async def approve_tool_confirmation(self) -> None:
        """Click the approve button on the tool confirmation dialog."""
        approve_button = await self.page.query_selector(
            self.CONFIRMATION_APPROVE_BUTTON
        )
        if not approve_button:
            raise RuntimeError("Tool confirmation approve button not found")
        await approve_button.click()

    async def reject_tool_confirmation(self) -> None:
        """Click the reject button on the tool confirmation dialog."""
        reject_button = await self.page.query_selector(self.CONFIRMATION_REJECT_BUTTON)
        if not reject_button:
            raise RuntimeError("Tool confirmation reject button not found")
        await reject_button.click()
