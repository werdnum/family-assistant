#!/usr/bin/env python
# /// script
# requires-python = ">=3.8"
# dependencies = [
#   "httpx",
#   "playwright",
#   "markitdown[html]>=0.1.0",
#   "mcp.server>=0.1.0", # MCP server library
# ]
# ///

"""
MCP Server providing a tool to scrape web URLs using Playwright's Async API,
render JavaScript, and return the content as Markdown using MarkItDown.
"""

import asyncio
import io
import sys
import argparse
import json
from typing import Sequence

# --- MCP Imports ---
from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.types import Tool, TextContent, ImageContent, EmbeddedResource
from mcp.shared.exceptions import McpError

# --- Scraping Imports ---
import httpx
from importlib.metadata import version
try:
    # Use Async API imports
    from playwright.async_api import async_playwright
    from playwright.async_api import Error as PlaywrightError
    from playwright.async_api import TimeoutError as PlaywrightTimeoutError
    _playwright_installed = True
except ImportError:
    _playwright_installed = False
    PlaywrightError = Exception # Define for except blocks
    PlaywrightTimeoutError = Exception # Define for except blocks
    async_playwright = None # Define for checks
    print("Warning: Playwright library not found. Scraping will rely on basic HTTP GET.", file=sys.stderr)
    print("Install using a PEP 723 runner or 'pip install \"markitdown[html]>=0.1.0\" playwright'", file=sys.stderr)
    print("Then run: python -m playwright install --with-deps chromium", file=sys.stderr)

try:
    from markitdown import MarkItDown
except ImportError:
    print("Error: markitdown is not installed. Run with a PEP 723 runner (e.g., pipx run).", file=sys.stderr)
    sys.exit(1)
# --- End Imports ---

# --- Constants ---
try:
    # Attempt to get a version, fallback if needed
    __version__ = version("mcp.server")
except Exception:
    __version__ = "0.0.0" # Fallback version

APP_NAME = "MCPWebScraperAsync"
APP_VERSION = "1.1.0" # Incremented version
WEBSITE_URL = "https://github.com/example/mcp-web-scraper" # Replace if you have one
mcp_scraper_user_agent = f"{APP_NAME}/{APP_VERSION} (MCPTool/{__version__}; +{WEBSITE_URL})"
# --- End Constants ---


# --- Async Scraper Class ---
class AsyncScraper:
    """Scrapes web content asynchronously and converts HTML to Markdown using MarkItDown."""

    def __init__(self, print_error=None, verify_ssl=True):
        """
        Initializes the scraper.

        Args:
            print_error (callable, optional): Function to print errors. Defaults to print to stderr.
            verify_ssl (bool, optional): Whether to verify SSL certificates. Defaults to True.
        """
        self.print_error = print_error or (lambda msg: print(f"SCRAPER: {msg}", file=sys.stderr))
        self.verify_ssl = verify_ssl
        self.playwright_available = _playwright_installed
        # Initialize MarkItDown converter (sync, will run in thread)
        self.md_converter = MarkItDown()

    async def scrape(self, url: str) -> str | None:
        """
        Asynchronously scrapes a URL, attempts conversion to Markdown if HTML,
        otherwise returns raw content.

        Args:
            url (str): The URL to scrape.

        Returns:
            str | None: The Markdown content if conversion is successful,
                        raw content if not HTML, or None on failure.
        """
        content: str | None = None
        mime_type: str | None = None

        if self.playwright_available:
            self.print_error(f"Attempting scrape with Async Playwright: {url}")
            content, mime_type = await self._scrape_with_playwright(url)
            if content is None and not self.playwright_available:
                 # Playwright might have failed permanently (e.g., browser launch)
                 self.print_error("Async Playwright failed, falling back to async HTTP GET.")
                 content, mime_type = await self._scrape_with_httpx(url)
            elif content is None:
                 # Playwright failed for this specific URL
                 self.print_error("Async Playwright scraping returned no content, trying async HTTP GET.")
                 content, mime_type = await self._scrape_with_httpx(url)

        else:
            self.print_error(f"Attempting scrape with async httpx: {url}")
            content, mime_type = await self._scrape_with_httpx(url)

        if content is None:
            self.print_error(f"Failed to retrieve content from {url}")
            return None

        # Check if the content is likely HTML
        if mime_type and "html" in mime_type.lower():
            self.print_error(f"Detected HTML content-type ({mime_type}), attempting Markdown conversion.")
            try:
                # MarkItDown conversion is synchronous, run it in a thread
                def convert_sync():
                    html_bytes = content.encode('utf-8', errors='replace')
                    html_stream = io.BytesIO(html_bytes)
                    result = self.md_converter.convert_stream(html_stream, filename="webpage.html")
                    return result.text_content if result and result.text_content else None

                markdown_result = await asyncio.to_thread(convert_sync)

                if markdown_result:
                    self.print_error("Markdown conversion successful.")
                    return markdown_result
                else:
                    self.print_error("Markdown conversion resulted in empty content. Returning original HTML.")
                    return content # Return original HTML if conversion yields nothing

            except Exception as e:
                self.print_error(f"MarkItDown conversion failed: {e}")
                self.print_error("Returning raw HTML content instead.")
                return content # Return raw HTML on conversion error
        else:
            self.print_error(f"Content-type is '{mime_type}' or unknown, returning raw content.")
            return content # Return raw content if not HTML

    async def _scrape_with_playwright(self, url: str) -> tuple[str | None, str | None]:
        """Internal: Scrapes using Playwright Async API."""
        if not async_playwright:
            self.playwright_available = False
            return None, None

        content: str | None = None
        mime_type: str | None = None
        browser = None
        context = None
        page = None

        try:
            async with async_playwright() as p:
                try:
                    # Use a more standard UA + custom part
                    user_agent = f"Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36 {mcp_scraper_user_agent}"
                    browser = await p.chromium.launch()
                    context = await browser.new_context(
                        ignore_https_errors=not self.verify_ssl,
                        user_agent=user_agent
                    )
                    page = await context.new_page()

                    response = None
                    try:
                        # Use networkidle, but handle timeout gracefully
                        response = await page.goto(url, wait_until="networkidle", timeout=20000) # Slightly increased timeout
                    except PlaywrightTimeoutError:
                        self.print_error(f"Warning: Async Playwright timed out waiting for network idle at {url}. Content might be incomplete.")
                        # Proceed to get content anyway
                    except PlaywrightError as e:
                        # Handle navigation errors (e.g., DNS resolution, connection refused)
                        if "net::ERR_" in str(e):
                             self.print_error(f"Playwright navigation network error for {url}: {e}")
                        else:
                            self.print_error(f"Playwright navigation error for {url}: {e}")
                        return None, None # Critical error during navigation

                    try:
                        content = await page.content()
                        if response:
                            headers = await response.all_headers()
                            content_type = headers.get("content-type")
                            if content_type:
                                mime_type = content_type.split(";")[0].strip().lower()
                    except PlaywrightError as e:
                        self.print_error(f"Playwright error getting content for {url}: {e}")
                        content = None # Ensure content is None on error

                except PlaywrightError as e:
                    # Catch errors during browser/context/page operations
                    self.print_error(f"Playwright execution error: {e}")
                    # If the error is about browser launch, mark playwright as unavailable
                    if "Executable doesn't exist" in str(e):
                        self.print_error("Playwright browser not found. Please run: python -m playwright install --with-deps chromium")
                        self.playwright_available = False
                    return None, None
                except Exception as e:
                    # Catch unexpected errors during Playwright context
                    self.print_error(f"Unexpected error during Playwright scraping context: {e}")
                    return None, None
                finally:
                    if page: await page.close()
                    if context: await context.close()
                    if browser: await browser.close()

        except Exception as e:
             # Catch errors during async_playwright() startup/shutdown
             self.print_error(f"Error setting up/tearing down Playwright: {e}")
             # If it failed to start, mark as unavailable
             if isinstance(e, PlaywrightError) and "Executable doesn't exist" in str(e):
                 self.playwright_available = False
             return None, None

        return content, mime_type

    async def _scrape_with_httpx(self, url: str) -> tuple[str | None, str | None]:
        """Internal: Scrapes using httpx AsyncClient."""
        headers = {"User-Agent": f"Mozilla/5.0 ({mcp_scraper_user_agent})"}
        try:
            # Use async client
            async with httpx.AsyncClient(
                headers=headers, verify=self.verify_ssl, follow_redirects=True, timeout=15.0
            ) as client:
                response = await client.get(url)
                response.raise_for_status() # Raise exception for 4xx/5xx status codes
                # Decode explicitly, httpx might guess wrong sometimes
                content = response.content.decode(response.encoding or 'utf-8', errors='replace')
                content_type = response.headers.get("content-type", "")
                mime_type = content_type.split(";")[0].strip().lower()
                return content, mime_type
        except httpx.HTTPStatusError as http_err:
            self.print_error(f"HTTP error occurred for {url}: {http_err.response.status_code} {http_err.response.reason_phrase}")
        except httpx.RequestError as req_err:
             self.print_error(f"HTTP request error occurred for {url}: {req_err}")
        except Exception as err:
            self.print_error(f"An unexpected error occurred during async httpx request for {url}: {err}")
        return None, None
# --- End Async Scraper Class ---


# --- Async Playwright Check ---
async def check_playwright_async():
    """Checks if Playwright async API and browser are functional."""
    if not _playwright_installed or not async_playwright:
        print("MCP: Playwright library not installed, skipping browser check.", file=sys.stderr)
        return False
    browser = None
    try:
        async with async_playwright() as p:
            browser = await p.chromium.launch()
        print("MCP: Async Playwright Chromium browser check successful.", file=sys.stderr)
        return True
    except Exception as e:
        print("MCP Warning: Async Playwright Chromium browser check failed.", file=sys.stderr)
        if "Executable doesn't exist" in str(e):
            print("MCP Warning: Run: python -m playwright install --with-deps chromium", file=sys.stderr)
            print("MCP Warning: Scraping will fall back to basic HTTP GET.", file=sys.stderr)
        else:
            print(f"MCP Warning: Playwright check error: {e}", file=sys.stderr)
        return False
    finally:
         if browser: await browser.close()


# --- MCP Server Implementation ---
async def serve(verify_ssl: bool = True) -> None:
    """Sets up and runs the MCP server for web scraping using Async Scraper."""
    server = Server("mcp-web-scraper-async")
    # Use the AsyncScraper
    scraper = AsyncScraper(verify_ssl=verify_ssl)

    @server.list_tools()
    async def list_tools() -> list[Tool]:
        """Lists the available web scraping tool."""
        # Tool definition remains the same
        return [
            Tool(
                name="scrape_url",
                description=(
                    "Scrapes a web URL using Playwright's async API to render JavaScript, "
                    "and returns the content as Markdown. Useful for reading web pages, "
                    "especially those requiring JS execution."
                ),
                inputSchema={
                    "type": "object",
                    "properties": {
                        "url": {
                            "type": "string",
                            "description": "The fully qualified URL to scrape (e.g., 'https://example.com').",
                        }
                    },
                    "required": ["url"],
                },
            )
        ]

    @server.call_tool()
    async def call_tool(
        name: str, arguments: dict
    ) -> Sequence[TextContent | ImageContent | EmbeddedResource]:
        """Handles the 'scrape_url' tool call using the AsyncScraper."""
        if name != "scrape_url":
            raise McpError(f"Unknown tool: {name}")

        url = arguments.get("url")
        if not url or not isinstance(url, str):
            raise McpError("Missing or invalid required argument: url (must be a string)")

        print(f"MCP: Received request to scrape URL: {url}", file=sys.stderr)

        try:
            # Directly await the async scrape method
            markdown_content = await scraper.scrape(url)

            if markdown_content is not None:
                print(f"MCP: Scraping successful for: {url}", file=sys.stderr)
                # Limit content size slightly for safety? Maybe later.
                # if len(markdown_content) > 50000:
                #     markdown_content = markdown_content[:50000] + "\n... (content truncated)"
                return [TextContent(type="text", text=markdown_content)]
            else:
                print(f"MCP: Scraping failed for: {url}", file=sys.stderr)
                # Raise an error that the client can interpret
                raise McpError(f"Failed to scrape or convert content from URL: {url}")

        except Exception as e:
            print(f"MCP: Error during scraping/conversion for {url}: {e}", file=sys.stderr)
            # Propagate error to the client
            raise McpError(f"Error processing scrape request for {url}: {str(e)}")

    # --- Run the server ---
    options = server.create_initialization_options()
    print("MCP Async Web Scraper Server starting. Checking Playwright...", file=sys.stderr)

    # Perform the async check before starting the server loop
    await check_playwright_async()

    print("MCP: Waiting for connection...", file=sys.stderr)
    async with stdio_server() as (read_stream, write_stream):
        print("MCP: Connection established. Running server loop.", file=sys.stderr)
        await server.run(read_stream, write_stream, options)
    print("MCP: Server shut down.", file=sys.stderr)

# --- Main Execution ---
if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Run an MCP server that provides an async web scraping tool using Playwright.",
        epilog="Connect this server to an MCP client (like Claude Desktop)."
    )
    parser.add_argument(
        "--no-verify-ssl",
        action="store_false",
        dest="verify_ssl",
        help="Disable SSL certificate verification during scraping.",
    )
    args = parser.parse_args()

    try:
        # Pass the command-line argument to the serve function
        asyncio.run(serve(verify_ssl=args.verify_ssl))
    except KeyboardInterrupt:
        print("\nMCP Server interrupted by user. Exiting.", file=sys.stderr)
        sys.exit(0)
    except Exception as e:
        # Catch potential top-level errors during asyncio.run()
        print(f"\nMCP Server encountered a fatal error during startup/runtime: {e}", file=sys.stderr)
        # Print traceback for debugging if possible
        import traceback
        traceback.print_exc(file=sys.stderr)
        sys.exit(1)
