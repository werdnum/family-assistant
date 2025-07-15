"""Base Page Object Model for Playwright tests."""

from typing import Any

from playwright.async_api import Page, Response


class BasePage:
    """Base class for all page objects."""

    def __init__(self, page: Page, base_url: str | None = None) -> None:
        """Initialize the base page with a Playwright page instance.

        Args:
            page: The Playwright page instance
            base_url: Optional base URL for the application
        """
        self.page = page
        self.base_url = base_url or "http://localhost:5173"

    async def navigate_to(self, path: str = "") -> Response | None:
        """Navigate to a specific path relative to the base URL.

        Args:
            path: The path to navigate to (e.g., "/notes", "/documents")

        Returns:
            The response object from the navigation, or None if no response
        """
        url = f"{self.base_url}{path}"
        response = await self.page.goto(url)
        await self.wait_for_load()
        return response

    async def wait_for_load(self, wait_for_network: bool = False) -> None:
        """Wait for the page to load.

        Args:
            wait_for_network: If True, waits for network idle (slower but more thorough).
                            If False, only waits for DOM content loaded (faster).
        """
        if wait_for_network:
            await self.page.wait_for_load_state("networkidle")
        else:
            await self.page.wait_for_load_state("domcontentloaded")

    async def wait_for_element(self, selector: str, timeout: int = 30000) -> None:
        """Wait for an element to be present on the page.

        Args:
            selector: CSS selector or text selector for the element
            timeout: Maximum time to wait in milliseconds
        """
        await self.page.wait_for_selector(selector, timeout=timeout)

    async def click_and_wait(
        self, selector: str, wait_for_network: bool = False
    ) -> None:
        """Click an element and wait for navigation or network activity to complete.

        Args:
            selector: CSS selector or text selector for the element to click
            wait_for_network: If True, waits for network idle after click
        """
        await self.page.click(selector)
        await self.wait_for_load(wait_for_network=wait_for_network)

    async def fill_form_field(self, selector: str, value: str) -> None:
        """Fill a form field with the specified value.

        Args:
            selector: CSS selector for the form field
            value: Value to fill in the field
        """
        await self.page.fill(selector, value)

    async def get_text(self, selector: str) -> str:
        """Get the text content of an element.

        Args:
            selector: CSS selector for the element

        Returns:
            The text content of the element
        """
        element = await self.page.wait_for_selector(selector)
        if element:
            text = await element.text_content()
            return text or ""
        return ""

    async def is_element_visible(self, selector: str) -> bool:
        """Check if an element is visible on the page.

        Args:
            selector: CSS selector for the element

        Returns:
            True if the element is visible, False otherwise
        """
        try:
            await self.page.wait_for_selector(selector, timeout=5000, state="visible")
            return True
        except Exception:
            return False

    async def wait_for_success_message(self, message: str | None = None) -> None:
        """Wait for a success message to appear.

        Args:
            message: Optional specific message to wait for
        """
        if message:
            await self.page.wait_for_selector(f"text={message}")
        else:
            # Common success message patterns
            await self.page.wait_for_selector(
                "[role=alert], .alert-success, .toast-success, text=/success/i"
            )

    async def wait_for_error_message(self, message: str | None = None) -> None:
        """Wait for an error message to appear.

        Args:
            message: Optional specific error message to wait for
        """
        if message:
            await self.page.wait_for_selector(f"text={message}")
        else:
            # Common error message patterns
            await self.page.wait_for_selector(
                "[role=alert].error, .alert-error, .toast-error, text=/error/i"
            )

    async def take_screenshot(self, name: str) -> None:
        """Take a screenshot of the current page state.

        Args:
            name: Name for the screenshot file
        """
        await self.page.screenshot(path=f"screenshots/{name}.png", full_page=True)

    def setup_console_error_collection(self) -> list[str]:
        """Set up console error collection for the page.

        Call this before navigating to start collecting errors.

        Returns:
            List that will be populated with console error messages
        """
        errors: list[str] = []

        def handle_console_message(msg: Any) -> None:
            if msg.type == "error":
                errors.append(msg.text)

        self.page.on("console", handle_console_message)
        return errors

    async def wait_for_network_idle(self, timeout: int = 30000) -> None:
        """Wait for network activity to settle.

        Args:
            timeout: Maximum time to wait in milliseconds
        """
        await self.page.wait_for_load_state("networkidle", timeout=timeout)

    async def reload(self) -> None:
        """Reload the current page."""
        await self.page.reload()
        await self.wait_for_load()
