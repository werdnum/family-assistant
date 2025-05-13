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

import argparse
import asyncio
import logging  # Changed
import sys
from collections.abc import Sequence  # Removed Dict, Any, Tuple, Optional

# from urllib.parse import urlparse # No longer needed here
# import os # No longer needed here
# --- MCP Imports ---
from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.shared.exceptions import McpError
from mcp.types import ImageContent, TextContent, Tool

# --- Local Project Imports ---
# Assuming contrib is at the same level as src, or PYTHONPATH is set up
try:
    from family_assistant.utils.scraping import (
        DEFAULT_USER_AGENT,  # For standalone mode user agent
        Scraper,
        ScrapeResult,
        check_playwright_is_functional,
    )
except ImportError:
    # This allows the script to provide a more helpful error if it's run
    # without the main package being available in PYTHONPATH.
    print(
        "Error: Could not import Scraper from family_assistant.utils.scraping. "
        "Ensure the script is run in an environment where 'family_assistant' package is accessible.",
        file=sys.stderr,
    )
    sys.exit(1)


# --- Constants ---
# Most constants (IMAGE_MIME_TYPES, etc.) are now in family_assistant.utils.scraping
# We might only need a specific user agent for the MCP server itself if it differs
# from the one used by the Scraper utility. For now, Scraper's DEFAULT_USER_AGENT
# will be used internally by the Scraper instance.
# If the MCP server needs to identify itself *additionally*, that could be done here.
# For simplicity, we'll rely on the Scraper's UA.

# --- End Constants ---

# --- Logging Setup ---
# Configure logging for the MCP server script
logger = logging.getLogger(__name__)
# Basic configuration, can be enhanced (e.g., to match main app's logging)
logging.basicConfig(level=logging.INFO, format="MCP_SCRAPER: %(levelname)s: %(message)s")


# --- Async Playwright Check (using imported function) ---
async def check_playwright_async_wrapper(): # Renamed to avoid conflict if any
    """Wraps the imported Playwright check for MCP server context."""
    # The check_playwright_is_functional from utils uses its own logger.
    # We can add MCP-specific logging here if needed.
    logger.info("Performing Playwright functionality check...")
    functional = await check_playwright_is_functional()
    if functional:
        logger.info("Playwright check successful.")
    else:
        logger.warning(
            "Playwright check failed. Scraping quality for dynamic sites may be reduced."
        )
    return functional


# --- MCP Server Implementation ---
async def serve(verify_ssl: bool = True) -> None:
    """Sets up and runs the MCP server for web scraping using the Scraper utility."""
    server = Server("mcp-web-scraper-async") # Keep a distinct name for the MCP server
    # Use the Scraper from utils
    # The Scraper's __init__ uses its own logger for internal messages.
    # The user_agent for the scraper will be its default or one passed here.
    # For an MCP tool, it's often good to let the utility handle its default UA.
    scraper_instance = Scraper(verify_ssl=verify_ssl)

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
    ) -> Sequence[TextContent | ImageContent]:  # Removed EmbeddedResource for now
        """Handles the 'scrape_url' tool call using the AsyncScraper."""
        if name != "scrape_url":
            raise McpError(f"Unknown tool: {name}")

        url = arguments.get("url")
        if not url or not isinstance(url, str):
            raise McpError(
                "Missing or invalid required argument: url (must be a string)"
            )

        logger.info(f"Received request to scrape URL: {url}")

        try:
            # Use the imported Scraper instance
            result: ScrapeResult = await scraper_instance.scrape(url)

            if result.type == "markdown":
                logger.info(f"Successfully converted content to Markdown for: {url} (Source: {result.source_description})")
                if result.content is None: # Should not happen if type is markdown
                    raise McpError(f"Scraper returned type markdown but content is None for {url}")
                return [TextContent(type="text", text=result.content)]
            elif result.type == "text":
                logger.info(f"Successfully retrieved text content (MIME: {result.mime_type}, Source: {result.source_description}) for: {url}")
                if result.content is None: # Should not happen if type is text
                    raise McpError(f"Scraper returned type text but content is None for {url}")
                return [TextContent(type="text", text=result.content)]
            elif result.type == "image":
                logger.info(f"Successfully retrieved image content (MIME: {result.mime_type}, Source: {result.source_description}) for: {url}")
                if result.content_bytes is None or result.mime_type is None: # Should not happen
                    raise McpError(f"Scraper returned type image but content_bytes or mime_type is None for {url}")
                return [
                    ImageContent(
                        type="image",
                        content=result.content_bytes,
                        media_type=result.mime_type,
                    )
                ]
            elif result.type == "error":
                error_message = result.message or "Unknown error during scraping."
                logger.error(f"Scraping/processing error for {url} (Source: {result.source_description}): {error_message}")
                raise McpError(error_message)
            else:
                unknown_error = (
                    f"Unknown result type '{result.type}' from scraper for {url}."
                )
                logger.error(unknown_error)
                raise McpError(unknown_error)

        except Exception as e:
            logger.error(f"Error during scraping/conversion for {url}: {e}", exc_info=True)
            raise McpError(f"Error processing scrape request for {url}: {str(e)}")

    # --- Run the server ---
    options = server.create_initialization_options()
    logger.info("MCP Async Web Scraper Server starting. Checking Playwright...")

    # Perform the async check before starting the server loop
    await check_playwright_async_wrapper() # Use the wrapper

    logger.info("Waiting for connection...")
    async with stdio_server() as (read_stream, write_stream):
        logger.info("Connection established. Running server loop.")
        await server.run(read_stream, write_stream, options)
    print("MCP: Server shut down.", file=sys.stderr)


# --- Main Execution ---
if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Run an MCP server that provides an async web scraping tool using Playwright, or scrape a single URL to stdout.",
        epilog="Connect this server to an MCP client (like Claude Desktop).",
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
                # Use basicConfig for standalone mode as well, or a more specific logger
                logging.basicConfig(level=logging.INFO, format="STANDALONE_SCRAPER: %(levelname)s: %(message)s")
                logger.info(f"Scraping URL: {args.url}")

                # Use the imported Scraper, providing its own default user agent
                scraper_instance = Scraper(
                    verify_ssl=args.verify_ssl,
                    user_agent=DEFAULT_USER_AGENT # Or a specific one for standalone
                )
                # Perform playwright check for standalone mode as well
                await check_playwright_async_wrapper()
                result: ScrapeResult = await scraper_instance.scrape(args.url)

                if result.type == "markdown" or result.type == "text":
                    if result.content is None:
                         logger.error(f"Error: Scraper returned type {result.type} but content is None.")
                         sys.exit(1)
                    print(result.content, end="")  # Print directly to stdout
                elif result.type == "image":
                    if result.content_bytes is None or result.mime_type is None:
                        logger.error("Error: Scraper returned type image but content_bytes or mime_type is None.")
                        sys.exit(1)
                    if args.output_file:
                        try:
                            with open(args.output_file, "wb") as f:
                                f.write(result.content_bytes)
                            logger.info(
                                f"Image content (MIME: {result.mime_type}, Source: {result.source_description}) saved to {args.output_file}"
                            )
                        except OSError as e:
                            logger.error(
                                f"Error writing image to file {args.output_file}: {e}"
                            )
                            sys.exit(1)
                    else:
                        sys.stdout.buffer.write(result.content_bytes)
                        sys.stdout.flush()
                        logger.info(
                            f"Image content (MIME: {result.mime_type}, Source: {result.source_description}) written to stdout."
                        )
                elif result.type == "error":
                    logger.error(
                        f"Error (Source: {result.source_description}): {result.message or 'Unknown scraping error.'}"
                    )
                    sys.exit(1)
                else:
                    logger.error(
                        f"Error: Unknown result type '{result.type}' from scraper."
                    )
                    sys.exit(1)

            asyncio.run(standalone_scrape())
        else:
            # MCP server mode
            # Logging for server mode is configured at the top of the script
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
