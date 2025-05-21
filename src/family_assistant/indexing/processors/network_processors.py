"""
Content processors that interact with the network, e.g., fetching web content.
"""

import logging
import os
import tempfile
from dataclasses import dataclass, field
from typing import TYPE_CHECKING
from urllib.parse import urlparse

from family_assistant.utils.scraping import Scraper, ScrapeResult

if TYPE_CHECKING:
    from family_assistant.indexing.pipeline import IndexableContent
    from family_assistant.storage.vector import Document  # Protocol for Document
    from family_assistant.tools.types import ToolExecutionContext


logger = logging.getLogger(__name__)


@dataclass
class WebFetcherProcessorConfig:
    """Configuration for WebFetcherProcessor."""

    process_embedding_types: list[str] = field(
        default_factory=lambda: ["extracted_link", "raw_url"]
    )
    # Add other configs like timeouts if needed.


class WebFetcherProcessor:
    """
    A content processor that fetches content from URLs found in IndexableContent items.
    It uses a Scraper instance to perform the fetching and initial processing.
    """

    def __init__(
        self, scraper: Scraper, config: WebFetcherProcessorConfig | None = None
    ) -> None:
        """
        Initializes the WebFetcherProcessor.

        Args:
            scraper: A Scraper instance (e.g., PlaywrightScraper or MockScraper).
            config: Configuration for the processor.
        """
        self.scraper = scraper
        self.config = config or WebFetcherProcessorConfig()
        self._temp_files: list[str] = []  # To keep track of created temp files

    @property
    def name(self) -> str:
        """Unique identifier for the processor."""
        return "web_fetcher_processor"

    async def process(
        self,
        current_items: list["IndexableContent"],
        original_document: "Document",  # Passed for context, not directly used yet
        initial_content_ref: "IndexableContent",  # Passed for context
        context: "ToolExecutionContext",  # Passed for context
    ) -> list["IndexableContent"]:
        """
        Processes IndexableContent items, fetches URLs, and creates new items.
        """
        # Import IndexableContent here for clarity, though TYPE_CHECKING handles it
        # for static analysis. Using string literals for type hints below.
        from family_assistant.indexing.pipeline import IndexableContent

        output_items: list[IndexableContent] = []
        items_to_pass_through: list[IndexableContent] = []

        for item in current_items:
            if (
                item.embedding_type in self.config.process_embedding_types
                and isinstance(item.content, str)
                and (
                    item.content.startswith("http://")
                    or item.content.startswith("https://")
                )
            ):
                url_to_fetch = item.content
                logger.info(
                    f"{self.name}: Attempting to fetch URL '{url_to_fetch}' (original item type: {item.embedding_type})."
                )
                try:
                    scrape_result: ScrapeResult = await self.scraper.scrape(
                        url_to_fetch
                    )

                    # Start with metadata from the original document
                    base_metadata_from_doc = {}
                    # Safely access original_document.metadata using getattr
                    doc_meta = getattr(original_document, "metadata", None)
                    if original_document and doc_meta:
                        if "original_url" in doc_meta:
                            base_metadata_from_doc["original_url"] = doc_meta[
                                "original_url"
                            ]
                        if "original_filename" in doc_meta:
                            base_metadata_from_doc["original_filename"] = doc_meta[
                                "original_filename"
                            ]

                    common_metadata = {
                        **base_metadata_from_doc,  # Inherit original_url/filename from original_document
                        "fetched_url": (
                            url_to_fetch
                        ),  # URL specifically fetched by this processor
                        "final_url": scrape_result.final_url,
                        "source_scraper_description": scrape_result.source_description,
                        "original_item_metadata": (
                            item.metadata or {}
                        ),  # Metadata from the input item (e.g., the 'extracted_link' item)
                        "fetched_title": (
                            scrape_result.title
                        ),  # Add title from scrape result
                        "mime_type": (
                            scrape_result.mime_type
                        ),  # Add mime_type from scrape result
                    }

                    # If original_url was not in original_document.metadata (and thus not in base_metadata_from_doc),
                    # set it to url_to_fetch as a fallback for the primary 'original_url' key.
                    if "original_url" not in common_metadata:
                        common_metadata["original_url"] = url_to_fetch

                    processed_successfully = False

                    # Safely get content attributes from ScrapeResult
                    actual_content_str = getattr(scrape_result, "content", None)
                    actual_content_bytes = getattr(scrape_result, "content_bytes", None)
                    # ScrapeResult uses 'message' for errors
                    error_message = getattr(scrape_result, "message", "Unknown error")

                    if scrape_result.type == "error":
                        logger.error(
                            f"{self.name}: Failed to scrape URL '{url_to_fetch}'. Error: {error_message}"
                        )
                        items_to_pass_through.append(item)
                        processed_successfully = (
                            True  # Error is a final state for this item
                        )

                    # Case 1: Markdown content
                    elif (
                        scrape_result.type == "markdown"
                        or (
                            scrape_result.type == "success"
                            and scrape_result.mime_type == "text/markdown"
                        )
                    ) and actual_content_str:  # Use actual_content_str
                        output_items.append(
                            IndexableContent(
                                content=actual_content_str,  # Use actual_content_str
                                embedding_type="fetched_content_markdown",
                                mime_type="text/markdown",
                                source_processor=self.name,
                                metadata=common_metadata,
                            )
                        )
                        processed_successfully = True
                    # Case 2: Text content
                    elif (
                        scrape_result.type == "text"
                        or (
                            scrape_result.type == "success"
                            and scrape_result.mime_type
                            and scrape_result.mime_type.startswith("text/")
                        )
                    ) and actual_content_str:  # Use actual_content_str
                        output_items.append(
                            IndexableContent(
                                content=actual_content_str,  # Use actual_content_str
                                embedding_type="fetched_content_text",
                                mime_type=scrape_result.mime_type or "text/plain",
                                source_processor=self.name,
                                metadata=common_metadata,
                            )
                        )
                        processed_successfully = True
                    # Case 3: Image/Binary content
                    elif (
                        scrape_result.type == "image"
                        or (
                            scrape_result.type == "success"
                            and scrape_result.mime_type
                            and (
                                scrape_result.mime_type.startswith("image/")
                                or scrape_result.mime_type == "application/octet-stream"
                            )
                        )
                    ) and actual_content_bytes:  # Use actual_content_bytes
                        suffix = ""
                        if scrape_result.mime_type:
                            if "jpeg" in scrape_result.mime_type:
                                suffix = ".jpg"
                            elif "png" in scrape_result.mime_type:
                                suffix = ".png"
                            elif "gif" in scrape_result.mime_type:
                                suffix = ".gif"
                            elif "webp" in scrape_result.mime_type:
                                suffix = ".webp"
                        if not suffix:
                            _root, ext = os.path.splitext(
                                urlparse(scrape_result.final_url).path
                            )
                            if ext:
                                suffix = ext

                        with tempfile.NamedTemporaryFile(
                            delete=False, suffix=suffix or ".tmp"
                        ) as tmp_file:
                            tmp_file.write(
                                actual_content_bytes
                            )  # Use actual_content_bytes
                            temp_file_path = tmp_file.name
                        self._temp_files.append(temp_file_path)
                        logger.debug(
                            f"{self.name}: Stored binary content from {scrape_result.final_url} to temp file: {temp_file_path}"
                        )

                        derived_filename_for_binary = (
                            os.path.basename(urlparse(scrape_result.final_url).path)
                            or f"download{suffix}"
                        )
                        binary_metadata = {**common_metadata}
                        # Add derived_filename, ensure original_filename (if from doc) is preserved
                        binary_metadata["derived_filename"] = (
                            derived_filename_for_binary
                        )

                        # If original_filename was not in original_document.metadata (and thus not in common_metadata),
                        # set it to the derived_filename_for_binary as a fallback for the primary 'original_filename' key.
                        if "original_filename" not in binary_metadata:
                            binary_metadata["original_filename"] = (
                                derived_filename_for_binary
                            )

                        output_items.append(
                            IndexableContent(
                                content=None,
                                ref=temp_file_path,
                                embedding_type="fetched_content_binary",
                                mime_type=scrape_result.mime_type
                                or "application/octet-stream",
                                source_processor=self.name,
                                metadata=binary_metadata,
                            )
                        )
                        processed_successfully = True

                    # Case 4: Unhandled or unexpected result (if not error and not processed by cases above)
                    if not processed_successfully:  # This implies it wasn't an error type and didn't match content types
                        logger.warning(
                            f"{self.name}: Unhandled scrape result for URL '{url_to_fetch}'. "
                            f"Type: '{scrape_result.type}', Mime: '{scrape_result.mime_type}', "
                            f"ScrapeResult.content (str): {bool(actual_content_str)}, ScrapeResult.content_bytes (bytes): {bool(actual_content_bytes)}. "
                            "Passing original item."
                        )
                        items_to_pass_through.append(item)

                except Exception as e:
                    logger.error(
                        f"{self.name}: Exception during scraping URL '{url_to_fetch}': {e}",
                        exc_info=True,
                    )
                    items_to_pass_through.append(item)
            else:
                items_to_pass_through.append(item)

        return output_items + items_to_pass_through

    def cleanup_temp_files(self) -> None:
        """
        Deletes any temporary files created by this processor instance.
        This should be called by the pipeline orchestrator after processing.
        """
        if not self._temp_files:
            logger.debug(f"{self.name}: No temporary files to clean up.")
            return

        logger.info(
            f"{self.name}: Cleaning up {len(self._temp_files)} temporary files."
        )
        cleaned_count = 0
        for f_path in self._temp_files:
            try:
                if os.path.exists(f_path):
                    os.remove(f_path)
                    logger.debug(f"{self.name}: Removed temporary file: {f_path}")
                    cleaned_count += 1
                else:
                    logger.warning(
                        f"{self.name}: Temporary file not found for cleanup: {f_path}"
                    )
            except OSError as e:
                logger.error(
                    f"{self.name}: Error removing temporary file {f_path}: {e}"
                )

        logger.info(
            f"{self.name}: Cleaned up {cleaned_count}/{len(self._temp_files)} temporary files."
        )
        self._temp_files.clear()

    def __del__(self) -> None:
        """
        Fallback cleanup for temporary files if explicit cleanup is missed.
        Explicit cleanup via `cleanup_temp_files` is preferred.
        """
        if self._temp_files:
            logger.warning(
                f"{self.name} instance being deleted with {len(self._temp_files)} temporary file(s) still tracked. "
                "This indicates `cleanup_temp_files()` was not called. Attempting cleanup now."
            )
            self.cleanup_temp_files()
