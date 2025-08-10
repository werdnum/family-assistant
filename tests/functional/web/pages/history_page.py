"""Page Object Model for History UI testing."""

from playwright.async_api import Page


class HistoryPage:
    """Page Object Model for History page interactions."""

    def __init__(self, page: Page, base_url: str) -> None:
        """Initialize with a Playwright page and base URL."""
        self.page = page
        self.base_url = base_url

    # Selectors using data-testid or specific component patterns
    INTERFACE_TYPE_TRIGGER = "[role='combobox']"
    INTERFACE_TYPE_TRIGGER_ALT = "button[role='combobox']"
    CONVERSATION_ID_INPUT = "input[name='conversation_id']"
    START_DATE_INPUT = "input[name='start_date']"
    END_DATE_INPUT = "input[name='end_date']"
    SEARCH_INPUT = "input[name='search']"
    APPLY_FILTERS_BUTTON = "button:has-text('Apply Filters')"
    CLEAR_FILTERS_BUTTON = "button:has-text('Clear Filters')"
    CONVERSATION_LIST = ".conversation-list"
    CONVERSATION_ITEM = ".conversation-item"

    async def navigate_to(self) -> None:
        """Navigate to the history page."""
        await self.page.goto(f"{self.base_url}/history")
        await self.wait_for_load()

    async def wait_for_load(self) -> None:
        """Wait for the history page to load."""
        await self.page.wait_for_selector(
            "h1:has-text('Conversation History')", timeout=10000
        )

    async def set_interface_type_filter(self, interface_type: str) -> None:
        """Set the interface type filter using shadcn Select.

        Args:
            interface_type: The interface type to select (e.g., "web", "telegram", "api", "email")
        """
        # Wait for the filters to be fully rendered
        await self.page.wait_for_timeout(1000)

        # Try multiple selectors to find the interface type combobox
        # First try data-testid (if frontend was rebuilt)
        trigger = self.page.locator("[data-testid='interface-type-select']")
        count = await trigger.count()

        # If not found, use a more general selector for the first combobox
        if count == 0:
            # Find combobox that contains "All Interfaces" text
            trigger = self.page.locator(
                "button[role='combobox']:has-text('All Interfaces')"
            )
            count = await trigger.count()

        # If still not found, get the first combobox in the filters section
        if count == 0:
            trigger = self.page.locator("details button[role='combobox']").first

        # Click to open dropdown
        await trigger.click(force=True)

        # Wait for dropdown to open and click the option
        if interface_type == "_all" or interface_type == "":
            option_text = "All Interfaces"
        elif interface_type == "web":
            option_text = "Web"
        elif interface_type == "telegram":
            option_text = "Telegram"
        elif interface_type == "api":
            option_text = "API"
        elif interface_type == "email":
            option_text = "Email"
        else:
            option_text = interface_type

        option = await self.page.wait_for_selector(
            f"div[role='option']:has-text('{option_text}')", timeout=5000
        )
        if option:
            await option.click()

        # Wait for the dropdown to close
        await self.page.wait_for_timeout(500)

    async def get_interface_type_options(self) -> list[str]:
        """Get all available interface type options.

        Returns:
            List of available interface type options
        """
        # Open the dropdown to get options
        trigger = self.page.locator("[data-testid='interface-type-select']")
        count = await trigger.count()

        if count == 0:
            trigger = self.page.locator("details button[role='combobox']").first

        # Click to open dropdown
        await trigger.click()

        # Wait for options to appear
        await self.page.wait_for_selector("div[role='option']", timeout=5000)

        # Get all option texts
        options = await self.page.locator("div[role='option']").all_text_contents()

        # Close dropdown by pressing Escape
        await self.page.keyboard.press("Escape")

        return options

    async def get_interface_type_filter_value(self) -> str:
        """Get the current interface type filter value.

        Returns:
            The current interface type
        """
        # Try multiple selectors to find the interface type combobox
        trigger = self.page.locator("[data-testid='interface-type-select']")
        count = await trigger.count()

        if count == 0:
            # Find combobox that's the first one in filters
            trigger = self.page.locator("details button[role='combobox']").first

        try:
            # Get text
            text = await trigger.text_content()

            # Map display text back to value
            text_value_map = {
                "All Interfaces": "_all",
                "Web": "web",
                "Telegram": "telegram",
                "API": "api",
                "Email": "email",
            }
            # Clean the text (remove any extra whitespace)
            if text:
                text = text.strip()
                return text_value_map.get(text, "_all")
            return "_all"
        except Exception:
            # Default to _all if element not found
            return "_all"

    async def set_conversation_id_filter(self, conversation_id: str) -> None:
        """Set the conversation ID filter.

        Args:
            conversation_id: The conversation ID to filter by
        """
        input_element = await self.page.wait_for_selector(
            self.CONVERSATION_ID_INPUT, timeout=5000
        )
        if input_element:
            await input_element.fill(conversation_id)

    async def set_date_range_filter(self, start_date: str, end_date: str) -> None:
        """Set the date range filter.

        Args:
            start_date: Start date in YYYY-MM-DD format
            end_date: End date in YYYY-MM-DD format
        """
        start_input = await self.page.wait_for_selector(
            self.START_DATE_INPUT, timeout=5000
        )
        end_input = await self.page.wait_for_selector(self.END_DATE_INPUT, timeout=5000)

        if start_input:
            await start_input.fill(start_date)
        if end_input:
            await end_input.fill(end_date)

    async def set_search_filter(self, search_text: str) -> None:
        """Set the search text filter.

        Args:
            search_text: Text to search for in conversations
        """
        search_input = await self.page.wait_for_selector(
            self.SEARCH_INPUT, timeout=5000
        )
        if search_input:
            await search_input.fill(search_text)

    async def apply_filters(self) -> None:
        """Click the Apply Filters button."""
        button = await self.page.wait_for_selector(
            self.APPLY_FILTERS_BUTTON, timeout=5000
        )
        if button:
            await button.click()
            # Wait for the filter to be applied
            await self.page.wait_for_timeout(1000)

    async def clear_filters(self) -> None:
        """Click the Clear Filters button."""
        button = await self.page.wait_for_selector(
            self.CLEAR_FILTERS_BUTTON, timeout=5000
        )
        if button:
            await button.click()
            # Wait for filters to clear
            await self.page.wait_for_timeout(500)

    async def get_conversation_count(self) -> int:
        """Get the number of conversations displayed.

        Returns:
            Number of conversations
        """
        conversations = await self.page.query_selector_all(self.CONVERSATION_ITEM)
        return len(conversations)

    async def click_conversation(self, index: int) -> None:
        """Click on a specific conversation by index.

        Args:
            index: Zero-based index of the conversation to click
        """
        conversations = await self.page.query_selector_all(self.CONVERSATION_ITEM)
        if index < len(conversations):
            await conversations[index].click()
            # Wait for navigation
            await self.page.wait_for_timeout(1000)
