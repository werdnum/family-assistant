"""Base Page Object Model for Playwright tests."""

import logging
from typing import Any

from playwright.async_api import Page, Request, Response

logger = logging.getLogger(__name__)


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

    async def navigate_to(
        self, path: str = "", wait_for_app_ready: bool = True
    ) -> Response | None:
        """Navigate to a specific path relative to the base URL.

        Args:
            path: The path to navigate to (e.g., "/notes", "/documents")
            wait_for_app_ready: If True, waits for React app to be fully ready (default: True)

        Returns:
            The response object from the navigation, or None if no response
        """
        url = f"{self.base_url}{path}"
        response = await self.page.goto(url)
        await self.wait_for_load(wait_for_app_ready=wait_for_app_ready)
        return response

    async def wait_for_load(self, wait_for_app_ready: bool = True) -> None:
        """Wait for the page to load.

        Args:
            wait_for_app_ready: When True, waits for React to mount and initial loading to complete.
                              When False, only waits for DOM content loaded.

        Note: We cannot use "networkidle" because SSE connections prevent it from ever triggering.
        Instead, we wait for React to mount and loading indicators to disappear.
        """
        if wait_for_app_ready:
            # Wait for app to be ready - all pages now use data-app-ready attribute
            # Fail fast if not found to catch issues
            await self.page.wait_for_selector(
                '[data-app-ready="true"]',
                timeout=10000,
            )

            # Wait for any loading indicators to disappear
            # This ensures ProfileSelector and other components have loaded their data
            # data-app-ready is only set after runtime is ready AND all loading states are false
            loading_indicator = self.page.locator('[data-loading-indicator="true"]')
            if await loading_indicator.count() > 0:
                # Wait for all loading indicators to disappear
                await loading_indicator.first.wait_for(state="hidden", timeout=10000)
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
        self, selector: str, wait_for_app_ready: bool = False
    ) -> None:
        """Click an element and wait for navigation or app to be ready.

        Args:
            selector: CSS selector or text selector for the element to click
            wait_for_app_ready: If True, waits for app to be fully ready after click
        """
        await self.page.click(selector)
        await self.wait_for_load(wait_for_app_ready=wait_for_app_ready)

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

        def handle_console_message(msg: Any) -> None:  # noqa: ANN401  # playwright console message
            if msg.type == "error":
                errors.append(msg.text)

        self.page.on("console", handle_console_message)
        return errors

    def setup_request_logging(self) -> None:
        """Set up request logging to help debug network issues.

        Logs all requests and their responses at INFO level.
        """

        async def handle_request_finished(request: Request) -> None:
            try:
                response = await request.response()
                if response:
                    status = response.status
                    logger.info(
                        f"Request finished: {request.method} {request.url} -> {status}"
                    )
                else:
                    logger.info(
                        f"Request finished: {request.method} {request.url} -> No response"
                    )
            except Exception as e:
                logger.error(f"Error handling request finish: {e}")

        async def handle_request_failed(request: Request) -> None:
            try:
                failure = request.failure
                if failure:
                    logger.info(
                        f"Request failed: {request.method} {request.url} -> {failure}"
                    )
                else:
                    logger.info(
                        f"Request failed: {request.method} {request.url} -> Unknown failure"
                    )
            except Exception as e:
                logger.error(f"Error handling request failure: {e}")

        self.page.on("requestfinished", handle_request_finished)
        self.page.on("requestfailed", handle_request_failed)

    async def wait_for_network_idle(self, timeout: int = 30000) -> None:
        """Wait for network activity to settle.

        Args:
            timeout: Maximum time to wait in milliseconds
        """
        await self.wait_for_page_idle(timeout=timeout)

    async def reload(self) -> None:
        """Reload the current page."""
        await self.page.reload()
        await self.wait_for_load()
