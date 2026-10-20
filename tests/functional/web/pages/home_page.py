"""Page object for Home and its threads in the chat."""

from playwright.async_api import Locator, expect

from .chat_page import ChatPage


class HomePage(ChatPage):
    """The chat with threads enabled: Home, thread cards and the grouped sidebar."""

    CONVERSATION_HEADING = '[data-testid="conversation-title"]'
    CONVERSATION_PANE = '[data-testid="conversation-pane"]'
    HOME_ITEM = '[data-testid="home-item"]'
    THREAD_BANNER = '[data-testid="thread-banner"]'
    THREAD_BREADCRUMB_HOME = '[data-testid="thread-breadcrumb-home"]'
    THREAD_ORIGIN = '[data-testid="thread-origin"]'
    THREAD_WORKING_INDICATOR = '[data-testid="thread-working-indicator"]'
    THREAD_UNREAD_BADGE = '[data-testid="thread-unread-badge"]'
    THREAD_CARD_STATE = '[data-testid="thread-card-state"]'
    THREAD_MARK_DONE = '[data-testid="thread-mark-done"]'
    THREAD_BANNER_STATE = '[data-testid="thread-banner-state"]'
    THREAD_ANCHOR_QUOTE = '[data-testid="thread-anchor-quote"]'
    MOVED_TURN_MARKER = '[data-testid="moved-turn-marker"]'
    START_THREAD_DIALOG = '[data-testid="start-thread-dialog"]'
    MOVE_TO_THREAD_DIALOG = '[data-testid="move-to-thread-dialog"]'

    def echo_of(self, thread_conversation_id: str) -> Locator:
        """The Home message echoing a thread's outcome."""
        return self.page.locator(
            '[data-testid="assistant-message"]'
            f'[data-echoed-from-thread="{thread_conversation_id}"]'
        )

    def heading(self) -> Locator:
        """The heading of the conversation in the main column."""
        return self.page.locator(
            f'{self.CONVERSATION_PANE}[data-presentation="main"] {self.CONVERSATION_HEADING}'
        )

    def pane(self, conversation_id: str) -> Locator:
        """The pane showing a conversation: the main column or the side panel."""
        return self.page.locator(
            f'{self.CONVERSATION_PANE}[data-pane-conversation-id="{conversation_id}"]'
        )

    async def send_in(self, conversation_id: str, message: str) -> None:
        """Send a message from the composer of the pane showing a conversation."""
        pane = self.pane(conversation_id)
        chat_input = pane.locator(self.CHAT_INPUT)
        await expect(chat_input).to_be_enabled(timeout=10000)
        await chat_input.fill(message)
        await chat_input.press("Enter")
        await expect(
            pane.locator(self.MESSAGE_USER).filter(has_text=message)
        ).to_be_visible(timeout=10000)

    async def close_panel(self) -> None:
        await self.page.get_by_test_id("panel-close").click()

    async def expand_panel(self) -> None:
        await self.page.get_by_test_id("panel-expand").click()

    async def collapse_panel(self) -> None:
        await self.page.get_by_test_id("panel-collapse").click()

    def thread_card(self, thread_conversation_id: str) -> Locator:
        return self.page.locator(
            f'[data-testid="thread-card"][data-conversation-id="{thread_conversation_id}"]'
        )

    def sidebar_thread(self, thread_conversation_id: str) -> Locator:
        return self.page.get_by_test_id(f"thread-item-{thread_conversation_id}")

    def sidebar_group(self, state: str) -> Locator:
        return self.page.get_by_test_id(f"thread-group-{state}")

    def thread_banner(self) -> Locator:
        return self.page.locator(self.THREAD_BANNER)

    async def wait_for_home(self, home_conversation_id: str) -> None:
        """Wait until Home is the open conversation, with no thread open."""
        await expect(
            self.pane(home_conversation_id).locator(self.CONVERSATION_HEADING)
        ).to_have_text("Home", timeout=15000)
        await self.page.wait_for_url(
            f"**/chat?conversation_id={home_conversation_id}", timeout=15000
        )

    async def wait_for_card_state(
        self, thread_conversation_id: str, state: str, timeout: int = 15000
    ) -> None:
        await expect(self.thread_card(thread_conversation_id)).to_have_attribute(
            "data-thread-state", state, timeout=timeout
        )

    async def open_thread_from_card(self, thread_conversation_id: str) -> None:
        """Open a thread from its card, beside Home or in its place."""
        await self.thread_card(thread_conversation_id).click()
        await self.page.wait_for_url(
            lambda url: f"thread={thread_conversation_id}" in url, timeout=15000
        )
        await (
            self
            .pane(thread_conversation_id)
            .locator(self.THREAD_BANNER)
            .wait_for(state="visible", timeout=15000)
        )

    def side_pane(self) -> Locator:
        """The pane in the right-hand panel, whichever thread it shows."""
        return self.page.locator(f'{self.CONVERSATION_PANE}[data-presentation="side"]')

    def reply_in(self, conversation_id: str, text: str) -> Locator:
        """An assistant message in the pane showing a conversation."""
        return (
            self
            .pane(conversation_id)
            .locator('[data-testid="assistant-message"]')
            .filter(has_text=text)
        )

    async def message_action(self, message: Locator, action_test_id: str) -> None:
        """Click one of a message's actions, which show while it is hovered."""
        await message.hover()
        await message.get_by_test_id(action_test_id).click()

    async def start_thread(
        self, profile_id: str, prompt: str | None, *, with_context: bool | None = None
    ) -> None:
        """Fill and submit the open start-thread dialog, and wait for it to close."""
        dialog = self.page.locator(self.START_THREAD_DIALOG)
        await dialog.get_by_test_id("start-thread-profile").select_option(profile_id)
        if with_context is not None:
            await dialog.get_by_test_id("start-thread-with-context").set_checked(
                with_context
            )
        if prompt is not None:
            await dialog.get_by_test_id("start-thread-prompt").fill(prompt)
        await dialog.get_by_test_id("start-thread-submit").click()
        await expect(dialog).to_have_count(0, timeout=15000)

    def move_option(self, thread_conversation_id: str) -> Locator:
        return self.page.locator(self.MOVE_TO_THREAD_DIALOG).get_by_test_id(
            f"move-option-{thread_conversation_id}"
        )

    async def back_to_home(self, home_conversation_id: str) -> None:
        await self.page.locator(self.THREAD_BREADCRUMB_HOME).click()
        await self.wait_for_home(home_conversation_id)
