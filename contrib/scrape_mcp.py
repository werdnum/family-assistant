#!/usr/bin/env python
# /// script
# requires-python = ">=3.8"
# dependencies = [
#   "httpx>=0.25.0", # Specify a recent version for better feature support
#   "playwright",
#   "markitdown[all]>=0.1.1", # Updated for broader file support and specific version
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
from typing import Sequence, Dict, Any, Tuple, Optional
from urllib.parse import urlparse
import os

# --- MCP Imports ---
from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.types import Tool, TextContent, ImageContent, EmbeddedResource # ImageContent is used
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
    print("Warning: Playwright library not found. HTML Scraping will rely on basic HTTP GET and markitdown conversion.", file=sys.stderr)
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
APP_VERSION = "1.2.0" # Incremented version for new features
WEBSITE_URL = "https://github.com/example/mcp-web-scraper" # Replace if you have one
mcp_scraper_user_agent = f"{APP_NAME}/{APP_VERSION} (MCPTool/{__version__}; +{WEBSITE_URL})"

IMAGE_MIME_TYPES = ["image/jpeg", "image/png", "image/gif", "image/webp", "image/svg+xml", "image/bmp"]
VERBATIM_TEXT_MIME_TYPES = ["application/json", "text/plain", "text/csv", "application/xml", "text/xml", "application/javascript", "text/css"]
HTML_MIME_TYPE = "text/html"
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

    async def _convert_bytes_to_markdown(self, content_bytes: bytes, filename: Optional[str]) -> Optional[str]:
        """Helper to convert bytes to Markdown using MarkItDown in a thread."""
        if not content_bytes:
            return None

        def convert_sync():
            stream = io.BytesIO(content_bytes)
            # Provide filename to markitdown for type detection if possible
            effective_filename = filename or "unknown_file"
            try:
                self.print_error(f"MarkItDown: Attempting conversion for {effective_filename}")
                result = self.md_converter.convert_stream(stream, filename=effective_filename)
                if result and result.text_content:
                    self.print_error(f"MarkItDown: Conversion successful for {effective_filename}")
                    return result.text_content
                else:
                    self.print_error(f"MarkItDown: Conversion resulted in empty content for {effective_filename}")
                    return None
            except Exception as e_convert:
                self.print_error(f"MarkItDown: convert_stream failed for {effective_filename}: {e_convert}")
                return None

        markdown_text = await asyncio.to_thread(convert_sync)
        return markdown_text

    async def scrape(self, url: str) -> Dict[str, Any]:
        """
        Asynchronously retrieves content from a URL and processes it based on type.
        Returns a dictionary with "type" and "content" (or "content_bytes", "mime_type").
        Possible types: "markdown", "text", "image", "error".

        Args:
            url (str): The URL to scrape.

        Returns:
            Dict[str, Any]: Processed content or error information.
        """
        self.print_error(f"Initiating scrape for URL: {url}")
        raw_bytes, content_type_header, final_url, encoding = await self._scrape_with_httpx(url)

        if raw_bytes is None:
            self.print_error(f"Failed to retrieve content using httpx from {url}")
            return {"type": "error", "message": f"Failed to retrieve content from URL: {url} using basic HTTP GET."}

        mime_type = (content_type_header.split(";")[0].strip().lower() if content_type_header else "")
        self.print_error(f"httpx fetch successful. Final URL: {final_url}, MIME type: '{mime_type}', Encoding: {encoding}, Size: {len(raw_bytes)} bytes")

        parsed_final_url = urlparse(final_url)
        filename_hint = os.path.basename(parsed_final_url.path) or "webresource"

        # 1. Image Handling
        if mime_type in IMAGE_MIME_TYPES:
            self.print_error(f"Detected image MIME type: {mime_type}. Returning raw image bytes.")
            return {"type": "image", "content_bytes": raw_bytes, "mime_type": mime_type}

        # 2. Verbatim Text Handling (JSON, plain text, etc.)
        if mime_type in VERBATIM_TEXT_MIME_TYPES:
            self.print_error(f"Detected verbatim text MIME type: {mime_type}. Returning decoded text.")
            try:
                decoded_text = raw_bytes.decode(encoding or 'utf-8', errors='replace')
                return {"type": "text", "content": decoded_text, "mime_type": mime_type}
            except Exception as e:
                self.print_error(f"Error decoding verbatim text for {url}: {e}")
                return {"type": "error", "message": f"Error decoding content for {url}: {e}"}

        # 3. HTML Handling (potentially with Playwright, then MarkItDown)
        if mime_type == HTML_MIME_TYPE:
            self.print_error(f"Detected HTML MIME type for {url}.")
            html_source_bytes: Optional[bytes] = None
            source_description = ""

            if self.playwright_available:
                self.print_error(f"Attempting JS rendering with Async Playwright for {url}")
                playwright_html_str, _ = await self._scrape_with_playwright(url) # Use original URL for Playwright
                if playwright_html_str:
                    self.print_error(f"Playwright successfully rendered HTML for {url}")
                    # Playwright page.content() returns str, assume it handled decoding.
                    html_source_bytes = playwright_html_str.encode('utf-8', errors='replace') # Encode to bytes for MarkItDown
                    source_description = "Playwright-rendered HTML"
                else:
                    self.print_error(f"Playwright rendering failed for {url}. Falling back to httpx-fetched HTML.")
                    html_source_bytes = raw_bytes
                    source_description = "httpx-fetched HTML (Playwright failed)"
            else: # Playwright not available
                self.print_error(f"Playwright not available. Using httpx-fetched HTML for {url}")
                html_source_bytes = raw_bytes
                source_description = "httpx-fetched HTML (Playwright not available)"

            if html_source_bytes:
                self.print_error(f"Converting {source_description} to Markdown using MarkItDown for {url}")
                markdown_content = await self._convert_bytes_to_markdown(html_source_bytes, filename_hint + ".html")
                if markdown_content:
                    return {"type": "markdown", "content": markdown_content}
                else:
                    # Fallback for HTML: if MarkItDown fails, return the raw HTML text.
                    self.print_error(f"Markdown conversion of HTML ({source_description}) failed. Returning raw HTML text for {url}.")
                    decoded_html_fallback = html_source_bytes.decode(encoding or 'utf-8', errors='replace')
                    return {"type": "text", "content": decoded_html_fallback, "mime_type": HTML_MIME_TYPE}

        # 4. General MarkItDown Conversion (PDF, DOCX, etc., or fallback)
        self.print_error(f"Attempting general MarkItDown conversion for {filename_hint} (MIME: {mime_type or 'unknown'}) from {url}")
        markdown_content = await self._convert_bytes_to_markdown(raw_bytes, filename=filename_hint)
        if markdown_content:
            return {"type": "markdown", "content": markdown_content}
        else:
            self.print_error(f"General MarkItDown conversion failed for {filename_hint} from {url}.")
            # If MarkItDown fails for non-HTML, and it's not a known verbatim type, it's an error.
            # Could also consider returning raw bytes as EmbeddedResource if MCP spec allows/client handles.
            # For now, let's error if not already handled and conversion fails.
            return {"type": "error", "message": f"Failed to convert content from {url} (type: {mime_type or 'unknown'}) to Markdown."}


    async def _scrape_with_playwright(self, url: str) -> Tuple[Optional[str], Optional[str]]:
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

    async def _scrape_with_httpx(self, url: str) -> Tuple[Optional[bytes], Optional[str], str, Optional[str]]:
        """
        Internal: Fetches content using httpx AsyncClient.
        Returns (raw_bytes, content_type_header, final_url, encoding).
        """
        headers = {"User-Agent": f"Mozilla/5.0 ({mcp_scraper_user_agent})"}
        try:
            async with httpx.AsyncClient(
                headers=headers, verify=self.verify_ssl, follow_redirects=True, timeout=15.0
            ) as client:
                response = await client.get(url)
                response.raise_for_status() # Raise exception for 4xx/5xx status codes
                raw_bytes = response.content
                content_type_header = response.headers.get("content-type")
                final_url = str(response.url)
                encoding = response.encoding # httpx's detected encoding
                return raw_bytes, content_type_header, final_url, encoding
        except httpx.HTTPStatusError as http_err:
            self.print_error(f"HTTP error occurred for {url}: {http_err.response.status_code} {http_err.response.reason_phrase}")
        except httpx.RequestError as req_err:
             self.print_error(f"HTTP request error occurred for {url}: {req_err}")
        except Exception as err:
            self.print_error(f"An unexpected error occurred during async httpx request for {url}: {err}")
        return None, None, url, None # Return original URL on failure here
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
                    "converts various document types (HTML, PDF, Office docs) to Markdown, "
                    "returns images directly, and passes through text formats like JSON. "
                    "Useful for fetching and processing diverse web content."
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
    ) -> Sequence[TextContent | ImageContent]: # Removed EmbeddedResource for now
        """Handles the 'scrape_url' tool call using the AsyncScraper."""
        if name != "scrape_url":
            raise McpError(f"Unknown tool: {name}")

        url = arguments.get("url")
        if not url or not isinstance(url, str):
            raise McpError("Missing or invalid required argument: url (must be a string)")

        print(f"MCP: Received request to scrape URL: {url}", file=sys.stderr)

        try:
            result = await scraper.scrape(url)
            result_type = result.get("type")

            if result_type == "markdown":
                print(f"MCP: Successfully converted content to Markdown for: {url}", file=sys.stderr)
                return [TextContent(type="text", text=result["content"])]
            elif result_type == "text":
                print(f"MCP: Successfully retrieved text content ({result.get('mime_type')}) for: {url}", file=sys.stderr)
                return [TextContent(type="text", text=result["content"])]
            elif result_type == "image":
                print(f"MCP: Successfully retrieved image content ({result.get('mime_type')}) for: {url}", file=sys.stderr)
                return [ImageContent(type="image", content=result["content_bytes"], media_type=result["mime_type"])]
            elif result_type == "error":
                error_message = result.get("message", "Unknown error during scraping.")
                print(f"MCP: Scraping/processing error for {url}: {error_message}", file=sys.stderr)
                raise McpError(error_message)
            else:
                unknown_error = f"Unknown result type '{result_type}' from scraper for {url}."
                print(f"MCP: {unknown_error}", file=sys.stderr)
                raise McpError(unknown_error)

        except Exception as e:
            print(f"MCP: Error during scraping/conversion for {url}: {e}", file=sys.stderr)
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
        description="Run an MCP server that provides an async web scraping tool using Playwright, or scrape a single URL to stdout.",
        epilog="Connect this server to an MCP client (like Claude Desktop)."
    )
    parser.add_argument(
        "url",
        nargs="?",
        type=str,
        help="Optional URL to scrape directly to stdout. If not provided, runs as an MCP server.",
    )
    parser.add_argument(
        "--no-verify-ssl",
        action="store_false",
        dest="verify_ssl",
        help="Disable SSL certificate verification during scraping.",
    )
    parser.add_argument(
        "--output-file",
        type=str,
        help="Optional: In standalone mode, save image output to this file instead of stdout. Other content types are still printed to stdout.",
    )
    args = parser.parse_args()

    try:
        if args.url:
            # Standalone mode
            async def standalone_scrape():
                print(f"STANDALONE: Scraping URL: {args.url}", file=sys.stderr)
                scraper = AsyncScraper(verify_ssl=args.verify_ssl)
                # Perform playwright check for standalone mode as well for informative messages
                await check_playwright_async()
                result = await scraper.scrape(args.url)
                result_type = result.get("type")

                if result_type == "markdown" or result_type == "text":
                    print(result["content"], end="") # Print directly to stdout
                elif result_type == "image":
                    if args.output_file:
                        try:
                            with open(args.output_file, "wb") as f:
                                f.write(result["content_bytes"])
                            print(f"STANDALONE: Image content ({result.get('mime_type')}) saved to {args.output_file}", file=sys.stderr)
                        except IOError as e:
                            print(f"STANDALONE: Error writing image to file {args.output_file}: {e}", file=sys.stderr)
                            # Fallback to stdout if file write fails? Or just error out?
                            # For now, just error out for the file operation.
                            sys.exit(1)
                    else:
                        # Write raw bytes to stdout. User can redirect if needed.
                        sys.stdout.buffer.write(result["content_bytes"])
                        sys.stdout.flush()
                        print(f"\nSTANDALONE: Image content ({result.get('mime_type')}) written to stdout.", file=sys.stderr)
                elif result_type == "error":
                    print(f"STANDALONE: Error: {result.get('message', 'Unknown scraping error.')}", file=sys.stderr)
                    sys.exit(1)
                else:
                    print(f"STANDALONE: Error: Unknown result type '{result_type}' from scraper.", file=sys.stderr)
                    sys.exit(1)
            asyncio.run(standalone_scrape())
        else:
            # MCP server mode
            asyncio.run(serve(verify_ssl=args.verify_ssl))
    except KeyboardInterrupt:
        print("\nProcess interrupted by user. Exiting.", file=sys.stderr)
        sys.exit(0)
    except Exception as e:
        print(f"\nProcess encountered a fatal error: {e}", file=sys.stderr)
        # Print traceback for debugging if possible
        import traceback
        traceback.print_exc(file=sys.stderr)

        sys.exit(1)
