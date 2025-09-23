"""Events Page Object Model for Playwright tests."""

import re
from typing import Any

from .base_page import BasePage


class EventsPage(BasePage):
    """Page object for events-related functionality."""

    # Selectors - Updated for shadcn/ui components
    # Time filter uses shadcn Select with trigger button
    # The button is inside a div with the id, not the button itself
    TIME_FILTER_TRIGGER = "#hours"
    TIME_FILTER_OPTION = "div[role='option']"

    # Source filter uses shadcn Select
    SOURCE_FILTER_TRIGGER = "#source_id"
    SOURCE_FILTER_OPTION = "div[role='option']"

    # Checkbox for triggered events
    ONLY_TRIGGERED_CHECKBOX = "#only_triggered"

    # Event cards and pagination
    EVENT_CARD = ".event-card"
    PAGINATION_PREV = "button:has-text('Previous')"
    PAGINATION_NEXT = "button:has-text('Next')"
    PAGINATION_INFO = ".pagination-info"

    # Filters section
    FILTERS_DETAILS = "details:has(summary:has-text('Filters'))"
    CLEAR_FILTERS_BUTTON = "button:has-text('Clear Filters')"

    async def navigate_to_events(self) -> None:
        """Navigate to the events list page."""
        await self.navigate_to("/events")
        await self.wait_for_load()

    async def set_hours_filter(self, hours: str) -> None:
        """Set the hours filter using shadcn Select.

        Args:
            hours: The hours value to select (e.g., "1", "6", "24", "48")
        """
        # Click the select trigger to open dropdown
        trigger = await self.page.wait_for_selector(
            self.TIME_FILTER_TRIGGER, timeout=5000
        )
        if not trigger:
            raise ValueError(
                f"Could not find time filter trigger: {self.TIME_FILTER_TRIGGER}"
            )
        await trigger.click()

        # Wait for dropdown to open and click the option
        option_text = f"Last {hours} hour" if hours == "1" else f"Last {hours} hours"
        option = await self.page.wait_for_selector(
            f"div[role='option']:has-text('{option_text}')", timeout=5000
        )
        if not option:
            raise ValueError(f"Could not find option: {option_text}")
        await option.click()

        # Wait for the dropdown to close
        await self.page.wait_for_timeout(500)

    async def get_hours_filter_value(self) -> str:
        """Get the current hours filter value from the shadcn Select.

        Returns:
            The current hours value
        """
        trigger = await self.page.wait_for_selector(
            self.TIME_FILTER_TRIGGER, state="visible", timeout=10000
        )
        if not trigger:
            return "24"  # Default value if trigger not found
        text = await trigger.text_content()

        # Parse the text to get the hours value
        if text and "Last 1 hour" in text:
            return "1"
        elif text and "Last 6 hours" in text:
            return "6"
        elif text and "Last 24 hours" in text:
            return "24"
        elif text and "Last 48 hours" in text:
            return "48"
        return "24"  # Default

    async def set_source_filter(self, source: str) -> None:
        """Set the source filter using shadcn Select.

        Args:
            source: The source ID to select (e.g., "_all", "home_assistant", "indexing")
        """
        # Click the select trigger to open dropdown - it's a button with the ID
        trigger = await self.page.wait_for_selector(
            self.SOURCE_FILTER_TRIGGER, state="visible", timeout=10000
        )
        if not trigger:
            raise ValueError(
                f"Could not find source filter trigger: {self.SOURCE_FILTER_TRIGGER}"
            )
        await trigger.click()

        # Wait for dropdown to open and click the option
        # Map source IDs to display text
        source_text_map = {
            "_all": "All Sources",
            "all": "All Sources",
            "home_assistant": "Home Assistant",
            "indexing": "Indexing",
            "test_source": "Test Source",
        }
        option_text = source_text_map.get(source, source)
        option = await self.page.wait_for_selector(
            f"div[role='option']:has-text('{option_text}')", timeout=5000
        )
        if not option:
            raise ValueError(f"Could not find option: {option_text}")
        await option.click()

        # Wait for the dropdown to close
        await self.page.wait_for_timeout(500)

    async def get_source_filter_value(self) -> str:
        """Get the current source filter value from the shadcn Select.

        Returns:
            The current source ID
        """
        trigger = await self.page.wait_for_selector(
            self.SOURCE_FILTER_TRIGGER, state="visible", timeout=10000
        )
        if not trigger:
            return "_all"  # Default value if trigger not found
        text = await trigger.text_content()

        # Parse the text to get the source ID
        text_source_map = {
            "All Sources": "_all",
            "Home Assistant": "home_assistant",
            "Indexing": "indexing",
            "Test Source": "test_source",
        }
        return text_source_map.get(text or "", "_all")

    async def set_only_triggered_filter(self, checked: bool) -> None:
        """Set the only triggered filter checkbox.

        Args:
            checked: Whether to check or uncheck the checkbox
        """
        checkbox = await self.page.wait_for_selector(
            self.ONLY_TRIGGERED_CHECKBOX, state="attached", timeout=5000
        )
        if not checkbox:
            raise ValueError(
                f"Could not find only triggered checkbox: {self.ONLY_TRIGGERED_CHECKBOX}"
            )
        is_checked = await checkbox.is_checked()
        if is_checked != checked:
            # Click the label for better reliability with shadcn Checkbox
            label = await self.page.query_selector("label[for='only_triggered']")
            if label:
                await label.click()
            else:
                await checkbox.click()
            # Wait for state to update
            await self.page.wait_for_timeout(500)

    async def get_only_triggered_filter_value(self) -> bool:
        """Get the current only triggered filter checkbox state.

        Returns:
            True if checked, False otherwise
        """
        checkbox = await self.page.wait_for_selector(
            self.ONLY_TRIGGERED_CHECKBOX, state="attached", timeout=5000
        )
        if not checkbox:
            return False  # Default value if checkbox not found
        return await checkbox.is_checked()

    async def open_filters(self) -> None:
        """Open the filters section if it's closed."""
        # Wait for the page to fully load first
        await self.page.wait_for_load_state("networkidle")
        await self.page.wait_for_timeout(1000)  # Give React time to render

        details = await self.page.wait_for_selector(self.FILTERS_DETAILS, timeout=10000)
        if not details:
            raise ValueError("Could not find filters details element")

        # Always click the summary to toggle the filters open
        # The 'open' attribute might not reflect the actual state due to React hydration
        summary = await details.query_selector("summary")
        if not summary:
            raise ValueError("Could not find filters summary element")

        # Click to open (or close and re-open if already open)
        await summary.click()
        await self.page.wait_for_timeout(500)

        # Check if we can see the source filter now
        try:
            await self.page.wait_for_selector(
                self.SOURCE_FILTER_TRIGGER, state="visible", timeout=2000
            )
        except Exception:
            # If not visible, click again to toggle
            await summary.click()
            await self.page.wait_for_timeout(500)

        # Final wait for filters to be visible
        await self.page.wait_for_selector(
            self.SOURCE_FILTER_TRIGGER, state="visible", timeout=10000
        )

    async def clear_filters(self) -> None:
        """Click the clear filters button."""
        await self.open_filters()
        clear_button = await self.page.wait_for_selector(
            self.CLEAR_FILTERS_BUTTON, timeout=5000
        )
        if not clear_button:
            raise ValueError(
                f"Could not find clear filters button: {self.CLEAR_FILTERS_BUTTON}"
            )
        await clear_button.click()

    async def get_event_count(self) -> int:
        """Get the count of events displayed on the page.

        Returns:
            The number of events visible
        """
        event_cards = await self.page.query_selector_all(self.EVENT_CARD)
        return len(event_cards)

    async def get_pagination_info(self) -> dict[str, Any]:
        """Get pagination information.

        Returns:
            Dictionary with pagination details
        """
        info_element = await self.page.wait_for_selector(
            self.PAGINATION_INFO, timeout=5000
        )
        if not info_element:
            return {"start": 0, "end": 0, "total": 0}
        text = await info_element.text_content()
        # Parse text like "Showing 1-20 of 45 events"

        match = re.search(r"Showing (\d+)-(\d+) of (\d+) events", text or "")
        if match:
            return {
                "start": int(match.group(1)),
                "end": int(match.group(2)),
                "total": int(match.group(3)),
            }
        return {"start": 0, "end": 0, "total": 0}

    async def navigate_to_next_page(self) -> None:
        """Navigate to the next page of events."""
        next_button = await self.page.wait_for_selector(
            self.PAGINATION_NEXT, timeout=5000
        )
        if not next_button:
            raise ValueError(f"Could not find next button: {self.PAGINATION_NEXT}")
        await next_button.click()
        await self.wait_for_load()

    async def navigate_to_previous_page(self) -> None:
        """Navigate to the previous page of events."""
        prev_button = await self.page.wait_for_selector(
            self.PAGINATION_PREV, timeout=5000
        )
        if not prev_button:
            raise ValueError(f"Could not find previous button: {self.PAGINATION_PREV}")
        await prev_button.click()
        await self.wait_for_load()
