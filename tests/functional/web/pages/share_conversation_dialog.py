"""Page object for the conversation sharing dialog."""

from playwright.async_api import Locator, Page, expect


class ShareConversationDialog:
    """Expose sharing actions without coupling tests to dialog markup."""

    def __init__(self, page: Page) -> None:
        self.page = page
        self.trigger = page.get_by_role("button", name="Share conversation", exact=True)
        self.dialog = page.get_by_role("dialog", name="Share conversation")
        self.url = self.dialog.get_by_role("textbox", name="Share link")
        self.destination = self.dialog.get_by_role("link", name="Open read-only view")

    def action(self, name: str) -> Locator:
        """Find a named action inside the dialog."""
        return self.dialog.get_by_role("button", name=name, exact=True)

    async def open(self) -> None:
        """Open sharing controls without mutating the link."""
        await self.trigger.click()
        await expect(self.dialog).to_be_visible()

    async def close(self) -> None:
        """Dismiss via keyboard and verify focus restoration."""
        await self.page.keyboard.press("Escape")
        await expect(self.dialog).not_to_be_visible()
        await expect(self.trigger).to_be_focused()
