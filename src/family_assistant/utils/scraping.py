"""
Utilities for scraping web URLs using Playwright's Async API and httpx,
rendering JavaScript, and returning content.
"""

import asyncio
import io
import logging
import os
from dataclasses import dataclass
from importlib.metadata import version
from urllib.parse import urlparse

import httpx

# --- Scraping Imports ---
try:
    # Use Async API imports
    from playwright.async_api import Error as PlaywrightError
    from playwright.async_api import TimeoutError as PlaywrightTimeoutError
    from playwright.async_api import async_playwright

    _playwright_installed = True
except ImportError:
    _playwright_installed = False
    PlaywrightError = Exception  # Define for except blocks
    PlaywrightTimeoutError = Exception  # Define for except blocks
    async_playwright = None  # Define for checks

try:
    from markitdown import MarkItDown
except ImportError:
    # This should ideally not happen if dependencies are managed correctly,
    # but good to have a fallback for the library itself.
    # The calling code (processor/tool) should handle this more gracefully.
    logging.getLogger(__name__).error(
        "markitdown library is not installed. Please install it."
    )
    # Allow the module to load, but scraping that needs MarkItDown will fail.
    MarkItDown = None


# --- Constants ---
try:
    # Attempt to get a version of a core local package, fallback if needed
    # Using a placeholder, as "mcp.server" might not be relevant here.
    # Consider using the main application's version if available.
    _lib_version = version("family_assistant")  # Placeholder, adjust if needed
except Exception:
    _lib_version = "0.0.0"  # Fallback version

APP_NAME = "FamilyAssistantScraper"  # Updated App Name
APP_VERSION = "1.0.0"
WEBSITE_URL = "https://github.com/your_repo/family-assistant"  # Replace if you have one
DEFAULT_USER_AGENT = (
    f"{APP_NAME}/{APP_VERSION} (FamilyAssistantInternalTool/{_lib_version}; +{WEBSITE_URL})"
)

IMAGE_MIME_TYPES = [
    "image/jpeg",
    "image/png",
    "image/gif",
    "image/webp",
    "image/svg+xml",
    "image/bmp",
]
VERBATIM_TEXT_MIME_TYPES = [
    "application/json",
    "text/plain",
    "text/csv",
    "application/xml",
    "text/xml",
    "application/javascript",
    "text/css",
]
HTML_MIME_TYPE = "text/html"

logger = logging.getLogger(__name__)


@dataclass
class ScrapeResult:
    """Structured result from the Scraper."""

    type: str  # "markdown", "text", "image", "error"
    final_url: str
    content: str | None = None  # For markdown or text
    content_bytes: bytes | None = None  # For images
    mime_type: str | None = None  # Detected MIME type
    encoding: str | None = None
    message: str | None = None  # Error message if type is "error"
    source_description: str = "unknown" # e.g. "Playwright-rendered HTML", "httpx-fetched PDF"


class Scraper:
    """Scrapes web content asynchronously and converts HTML to Markdown using MarkItDown."""

    def __init__(self, verify_ssl: bool = True, user_agent: str | None = None) -> None:
        """
        Initializes the scraper.

        Args:
            verify_ssl: Whether to verify SSL certificates. Defaults to True.
            user_agent: Custom user agent string. Defaults to DEFAULT_USER_AGENT.
        """
        self.verify_ssl = verify_ssl
        self.user_agent = user_agent or DEFAULT_USER_AGENT
        self.playwright_available = _playwright_installed
        if MarkItDown:
            self.md_converter = MarkItDown()
        else:
            self.md_converter = None
            logger.error(
                "MarkItDown library not available. HTML to Markdown conversion will be skipped."
            )

    async def _convert_bytes_to_markdown(
        self, content_bytes: bytes, filename: str | None
    ) -> str | None:
        """Helper to convert bytes to Markdown using MarkItDown in a thread."""
        if not content_bytes or not self.md_converter:
            if not self.md_converter:
                logger.warning(
                    "MarkItDown converter not initialized, cannot convert to markdown."
                )
            return None

        def convert_sync() -> str | None:
            stream = io.BytesIO(content_bytes)
            effective_filename = filename or "unknown_file"
            try:
                logger.debug(
                    f"MarkItDown: Attempting conversion for {effective_filename}"
                )
                result = self.md_converter.convert_stream(
                    stream, filename=effective_filename
                )
                if result and result.text_content:
                    logger.debug(
                        f"MarkItDown: Conversion successful for {effective_filename}"
                    )
                    return result.text_content
                else:
                    logger.warning(
                        f"MarkItDown: Conversion resulted in empty content for {effective_filename}"
                    )
                    return None
            except Exception as e_convert:
                logger.error(
                    f"MarkItDown: convert_stream failed for {effective_filename}: {e_convert}",
                    exc_info=True,
                )
                return None

        markdown_text = await asyncio.to_thread(convert_sync)
        return markdown_text

    async def scrape(self, url: str) -> ScrapeResult:
        """
        Asynchronously retrieves content from a URL and processes it based on type.
        Returns a ScrapeResult object.

        Args:
            url: The URL to scrape.
        """
        logger.info(f"Initiating scrape for URL: {url}")
        (
            raw_bytes,
            content_type_header,
            final_url,
            encoding,
        ) = await self._fetch_with_httpx(url)

        if raw_bytes is None:
            logger.warning(f"Failed to retrieve content using httpx from {url}")
            return ScrapeResult(
                type="error",
                final_url=url, # Original URL as final_url might not be known
                message=f"Failed to retrieve content from URL: {url} using basic HTTP GET.",
                source_description="httpx fetch failed",
            )

        mime_type = (
            content_type_header.split(";")[0].strip().lower()
            if content_type_header
            else ""
        )
        logger.info(
            f"httpx fetch successful. Final URL: {final_url}, MIME type: '{mime_type}', Encoding: {encoding}, Size: {len(raw_bytes)} bytes"
        )

        parsed_final_url = urlparse(final_url)
        filename_hint = os.path.basename(parsed_final_url.path) or "webresource"

        # 1. Image Handling
        if mime_type in IMAGE_MIME_TYPES:
            logger.info(
                f"Detected image MIME type: {mime_type}. Returning raw image bytes."
            )
            return ScrapeResult(
                type="image",
                final_url=final_url,
                content_bytes=raw_bytes,
                mime_type=mime_type,
                encoding=encoding,
                source_description=f"httpx-fetched image ({mime_type})",
            )

        # 2. Verbatim Text Handling (JSON, plain text, etc.)
        if mime_type in VERBATIM_TEXT_MIME_TYPES:
            logger.info(
                f"Detected verbatim text MIME type: {mime_type}. Returning decoded text."
            )
            try:
                decoded_text = raw_bytes.decode(encoding or "utf-8", errors="replace")
                return ScrapeResult(
                    type="text",
                    final_url=final_url,
                    content=decoded_text,
                    mime_type=mime_type,
                    encoding=encoding,
                    source_description=f"httpx-fetched text ({mime_type})",
                )
            except Exception as e:
                logger.error(f"Error decoding verbatim text for {url}: {e}")
                return ScrapeResult(
                    type="error",
                    final_url=final_url,
                    message=f"Error decoding content for {url}: {e}",
                    mime_type=mime_type,
                    encoding=encoding,
                    source_description=f"httpx-fetched text ({mime_type}), decoding error",
                )

        # 3. HTML Handling (potentially with Playwright, then MarkItDown)
        if mime_type == HTML_MIME_TYPE:
            logger.info(f"Detected HTML MIME type for {url}.")
            html_source_bytes: bytes | None = None
            source_description = ""

            if self.playwright_available:
                logger.info(
                    f"Attempting JS rendering with Playwright for {url}"
                )
                playwright_html_str, _ = await self._fetch_with_playwright(
                    url
                )
                if playwright_html_str:
                    logger.info(f"Playwright successfully rendered HTML for {url}")
                    html_source_bytes = playwright_html_str.encode(
                        "utf-8", errors="replace"
                    )
                    source_description = "Playwright-rendered HTML"
                else:
                    logger.warning(
                        f"Playwright rendering failed for {url}. Falling back to httpx-fetched HTML."
                    )
                    html_source_bytes = raw_bytes
                    source_description = "httpx-fetched HTML (Playwright failed)"
            else:
                logger.info(
                    f"Playwright not available. Using httpx-fetched HTML for {url}"
                )
                html_source_bytes = raw_bytes
                source_description = "httpx-fetched HTML (Playwright not available)"

            if html_source_bytes:
                logger.info(
                    f"Converting {source_description} to Markdown using MarkItDown for {url}"
                )
                markdown_content = await self._convert_bytes_to_markdown(
                    html_source_bytes, filename_hint + ".html"
                )
                if markdown_content:
                    return ScrapeResult(
                        type="markdown",
                        final_url=final_url, # Should be final_url from playwright if it succeeded, or httpx's
                        content=markdown_content,
                        mime_type="text/markdown", # Output is markdown
                        encoding="utf-8", # Markdown is text
                        source_description=source_description + " -> MarkItDown",
                    )
                else:
                    logger.warning(
                        f"Markdown conversion of HTML ({source_description}) failed. Returning raw HTML text for {url}."
                    )
                    decoded_html_fallback = html_source_bytes.decode(
                        encoding or "utf-8", errors="replace"
                    )
                    return ScrapeResult(
                        type="text", # Fallback to raw text
                        final_url=final_url,
                        content=decoded_html_fallback,
                        mime_type=HTML_MIME_TYPE, # Original HTML mime type
                        encoding=encoding,
                        source_description=source_description + " (MarkItDown failed, raw HTML)",
                    )

        # 4. General MarkItDown Conversion (PDF, DOCX, etc., or fallback for unknown types)
        logger.info(
            f"Attempting general MarkItDown conversion for {filename_hint} (MIME: {mime_type or 'unknown'}) from {url}"
        )
        markdown_content = await self._convert_bytes_to_markdown(
            raw_bytes, filename=filename_hint
        )
        if markdown_content:
            return ScrapeResult(
                type="markdown",
                final_url=final_url,
                content=markdown_content,
                mime_type="text/markdown", # Output is markdown
                encoding="utf-8",
                source_description=f"httpx-fetched ({mime_type or 'unknown'}) -> MarkItDown",
            )
        else:
            logger.warning(
                f"General MarkItDown conversion failed for {filename_hint} from {url}."
            )
            return ScrapeResult(
                type="error",
                final_url=final_url,
                message=f"Failed to convert content from {url} (type: {mime_type or 'unknown'}) to Markdown.",
                mime_type=mime_type,
                encoding=encoding,
                source_description=f"httpx-fetched ({mime_type or 'unknown'}), MarkItDown failed",
            )

    async def _fetch_with_playwright(
        self, url: str
    ) -> tuple[str | None, str | None]:
        """Internal: Scrapes using Playwright Async API. Returns (html_content_str, final_mime_type)."""
        if not async_playwright or not self.playwright_available:
            # Ensure playwright_available is False if async_playwright itself is None
            self.playwright_available = False
            logger.warning("Playwright library or browser not available for _fetch_with_playwright.")
            return None, None

        content_str: str | None = None
        final_mime_type: str | None = None
        browser = None
        context = None
        page = None

        try:
            async with async_playwright() as p:
                try:
                    # Use a more standard UA + custom part
                    effective_user_agent = f"Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36 {self.user_agent}"
                    browser = await p.chromium.launch()
                    context = await browser.new_context(
                        ignore_https_errors=not self.verify_ssl, user_agent=effective_user_agent
                    )
                    page = await context.new_page()

                    response = None
                    try:
                        logger.debug(f"Playwright navigating to {url}")
                        response = await page.goto(
                            url, wait_until="networkidle", timeout=20000
                        )
                        logger.debug(f"Playwright navigation to {url} completed.")
                    except PlaywrightTimeoutError:
                        logger.warning(
                            f"Playwright timed out waiting for network idle at {url}. Content might be incomplete."
                        )
                        # Proceed to get content anyway
                    except PlaywrightError as e:
                        if "net::ERR_" in str(e):
                            logger.error(
                                f"Playwright navigation network error for {url}: {e}"
                            )
                        else:
                            logger.error(
                                f"Playwright navigation error for {url}: {e}"
                            )
                        return None, None

                    try:
                        content_str = await page.content()
                        if response:
                            headers = await response.all_headers()
                            content_type_header = headers.get("content-type")
                            if content_type_header:
                                final_mime_type = content_type_header.split(";")[0].strip().lower()
                        logger.debug(f"Playwright successfully fetched content for {url}. Length: {len(content_str or '')}")
                    except PlaywrightError as e:
                        logger.error(
                            f"Playwright error getting content for {url}: {e}"
                        )
                        content_str = None # Ensure content is None on error

                except PlaywrightError as e:
                    logger.error(f"Playwright execution error: {e}", exc_info=True)
                    if "Executable doesn't exist" in str(e) or "Browser process exited" in str(e):
                        logger.error(
                            "Playwright browser not found or failed to launch. "
                            "Please run: python -m playwright install --with-deps chromium"
                        )
                        self.playwright_available = False # Mark as unavailable for future calls in this instance
                    return None, None
                except Exception as e:
                    logger.error(
                        f"Unexpected error during Playwright scraping context: {e}",
                        exc_info=True,
                    )
                    return None, None
                finally:
                    if page:
                        await page.close()
                    if context:
                        await context.close()
                    if browser:
                        await browser.close()
        except Exception as e:
            logger.error(f"Error setting up/tearing down Playwright: {e}", exc_info=True)
            if isinstance(e, PlaywrightError) and ("Executable doesn't exist" in str(e) or "Browser process exited" in str(e)):
                self.playwright_available = False
            return None, None

        return content_str, final_mime_type

    async def _fetch_with_httpx(
        self, url: str
    ) -> tuple[bytes | None, str | None, str, str | None]:
        """
        Internal: Fetches content using httpx AsyncClient.
        Returns (raw_bytes, content_type_header, final_url, encoding).
        """
        headers = {"User-Agent": self.user_agent}
        try:
            async with httpx.AsyncClient(
                headers=headers,
                verify=self.verify_ssl,
                follow_redirects=True,
                timeout=15.0,
            ) as client:
                logger.debug(f"httpx GET request to {url}")
                response = await client.get(url)
                response.raise_for_status()
                raw_bytes = response.content
                content_type_header = response.headers.get("content-type")
                final_url = str(response.url)
                encoding = response.encoding
                logger.debug(f"httpx GET successful for {url}. Status: {response.status_code}, Final URL: {final_url}")
                return raw_bytes, content_type_header, final_url, encoding
        except httpx.HTTPStatusError as http_err:
            logger.error(
                f"HTTP error occurred for {url}: {http_err.response.status_code} {http_err.response.reason_phrase}"
            )
        except httpx.RequestError as req_err:
            logger.error(f"HTTP request error occurred for {url}: {req_err}")
        except Exception as err:
            logger.error(
                f"An unexpected error occurred during async httpx request for {url}: {err}",
                exc_info=True,
            )
        return None, None, url, None # Return original URL on failure here


async def check_playwright_is_functional() -> bool:
    """Checks if Playwright async API and browser are functional."""
    if not _playwright_installed or not async_playwright:
        logger.warning(
            "Playwright library not installed, skipping browser check."
        )
        return False

    browser = None
    try:
        async with async_playwright() as p:
            logger.debug("Launching Playwright Chromium for check...")
            browser = await p.chromium.launch()
            logger.info("Playwright Chromium browser check successful.")
        return True
    except Exception as e:
        logger.warning("Playwright Chromium browser check failed.")
        if "Executable doesn't exist" in str(e) or "Browser process exited" in str(e):
            logger.warning(
                "Playwright browser executable not found or failed to launch. "
                "Run: python -m playwright install --with-deps chromium. "
                "Scraping will fall back to basic HTTP GET for HTML if Playwright is needed."
            )
        else:
            logger.warning(f"Playwright check error: {e}", exc_info=True)
        return False
    finally:
        if browser:
            await browser.close()
            logger.debug("Playwright browser closed after check.")
