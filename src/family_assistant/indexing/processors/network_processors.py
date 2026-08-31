"""
Content processors that interact with the network, e.g., fetching web content.
"""

import logging
import os
import tempfile
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, cast
from urllib.parse import urlparse

from family_assistant.indexing.pipeline import IndexableContent
from family_assistant.utils.scraping import Scraper, ScrapeResult

if TYPE_CHECKING:
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
        initial_content_ref: "IndexableContent | None",  # Passed for context
        context: "ToolExecutionContext",  # Passed for context
    ) -> list["IndexableContent"]:
        """
        Processes IndexableContent items, fetches URLs, and creates new items.
        """
        output_items: list[IndexableContent] = []
        items_to_pass_through: list[IndexableContent] = []

        for item in current_items:
            if not self._is_fetchable_url(item):
                items_to_pass_through.append(item)
                continue

            url_to_fetch = cast("str", item.content)
            logger.info(
                f"{self.name}: Attempting to fetch URL '{url_to_fetch}' (original item type: {item.embedding_type})."
            )
            try:
                scrape_result = await self.scraper.scrape(url_to_fetch)
                fetched_item = self._create_fetched_item(
                    item, original_document, url_to_fetch, scrape_result
                )
            except Exception as error:
                logger.exception(
                    f"{self.name}: Exception during scraping URL '{url_to_fetch}': {error}"
                )
                items_to_pass_through.append(item)
                continue

            if fetched_item is None:
                items_to_pass_through.append(item)
            else:
                output_items.append(fetched_item)

        return output_items + items_to_pass_through

    def _is_fetchable_url(self, item: IndexableContent) -> bool:
        if item.embedding_type not in self.config.process_embedding_types:
            return False
        if not isinstance(item.content, str):
            return False
        return item.content.startswith(("http://", "https://"))

    def _create_fetched_item(
        self,
        item: IndexableContent,
        original_document: "Document",
        url_to_fetch: str,
        scrape_result: ScrapeResult,
    ) -> IndexableContent | None:
        common_metadata = self._build_common_metadata(
            item, original_document, url_to_fetch, scrape_result
        )
        actual_content_str = getattr(scrape_result, "content", None)
        actual_content_bytes = getattr(scrape_result, "content_bytes", None)
        error_message = getattr(scrape_result, "message", "Unknown error")

        if scrape_result.type == "error":
            logger.error(
                f"{self.name}: Failed to scrape URL '{url_to_fetch}'. Error: {error_message}"
            )
            return None
        if self._is_markdown_result(scrape_result, actual_content_str):
            return self._create_markdown_item(
                cast("str", actual_content_str), common_metadata
            )
        if self._is_text_result(scrape_result, actual_content_str):
            return self._create_text_item(
                cast("str", actual_content_str), scrape_result, common_metadata
            )
        if self._is_binary_result(scrape_result, actual_content_bytes):
            return self._create_binary_item(
                cast("bytes", actual_content_bytes), scrape_result, common_metadata
            )

        logger.warning(
            f"{self.name}: Unhandled scrape result for URL '{url_to_fetch}'. "
            f"Type: '{scrape_result.type}', Mime: '{scrape_result.mime_type}', "
            f"ScrapeResult.content (str): {bool(actual_content_str)}, ScrapeResult.content_bytes (bytes): {bool(actual_content_bytes)}. "
            "Passing original item."
        )
        return None

    @staticmethod
    def _build_common_metadata(
        item: IndexableContent,
        original_document: "Document",
        url_to_fetch: str,
        scrape_result: ScrapeResult,
    ) -> dict[str, object]:
        metadata: dict[str, object] = {}
        doc_meta = getattr(original_document, "metadata", None)
        if original_document and doc_meta:
            for key in ("original_url", "original_filename"):
                if key in doc_meta:
                    metadata[key] = doc_meta[key]

        metadata.update({
            "fetched_url": url_to_fetch,
            "final_url": scrape_result.final_url,
            "source_scraper_description": scrape_result.source_description,
            "original_item_metadata": item.metadata or {},
            "fetched_title": scrape_result.title,
            "mime_type": scrape_result.mime_type,
        })
        metadata.setdefault("original_url", url_to_fetch)
        return metadata

    @staticmethod
    def _is_markdown_result(scrape_result: ScrapeResult, content: str | None) -> bool:
        if not content:
            return False
        return scrape_result.type == "markdown" or (
            scrape_result.type == "success"
            and scrape_result.mime_type == "text/markdown"
        )

    @staticmethod
    def _is_text_result(scrape_result: ScrapeResult, content: str | None) -> bool:
        if not content:
            return False
        if scrape_result.type == "text":
            return True
        return bool(
            scrape_result.type == "success"
            and scrape_result.mime_type
            and scrape_result.mime_type.startswith("text/")
        )

    @staticmethod
    def _is_binary_result(scrape_result: ScrapeResult, content: bytes | None) -> bool:
        if not content:
            return False
        if scrape_result.type == "image":
            return True
        if scrape_result.type != "success" or not scrape_result.mime_type:
            return False
        return scrape_result.mime_type.startswith("image/") or (
            scrape_result.mime_type == "application/octet-stream"
        )

    def _create_markdown_item(
        self, content: str, metadata: dict[str, object]
    ) -> IndexableContent:
        return IndexableContent(
            content=content,
            embedding_type="fetched_content_markdown",
            mime_type="text/markdown",
            source_processor=self.name,
            metadata=metadata,
        )

    def _create_text_item(
        self,
        content: str,
        scrape_result: ScrapeResult,
        metadata: dict[str, object],
    ) -> IndexableContent:
        return IndexableContent(
            content=content,
            embedding_type="fetched_content_text",
            mime_type=scrape_result.mime_type or "text/plain",
            source_processor=self.name,
            metadata=metadata,
        )

    def _create_binary_item(
        self,
        content: bytes,
        scrape_result: ScrapeResult,
        common_metadata: dict[str, object],
    ) -> IndexableContent:
        suffix = self._binary_suffix(scrape_result)
        temp_file_path = self._store_binary_content(content, scrape_result, suffix)
        derived_filename = (
            os.path.basename(urlparse(scrape_result.final_url).path)
            or f"download{suffix}"
        )
        binary_metadata = {
            **common_metadata,
            "derived_filename": derived_filename,
        }
        binary_metadata.setdefault("original_filename", derived_filename)

        return IndexableContent(
            content=None,
            ref=temp_file_path,
            embedding_type="fetched_content_binary",
            mime_type=scrape_result.mime_type or "application/octet-stream",
            source_processor=self.name,
            metadata=binary_metadata,
        )

    @staticmethod
    def _binary_suffix(scrape_result: ScrapeResult) -> str:
        mime_suffixes = {
            "jpeg": ".jpg",
            "png": ".png",
            "gif": ".gif",
            "webp": ".webp",
        }
        if scrape_result.mime_type:
            for mime_fragment, suffix in mime_suffixes.items():
                if mime_fragment in scrape_result.mime_type:
                    return suffix

        _root, extension = os.path.splitext(urlparse(scrape_result.final_url).path)
        return extension

    def _store_binary_content(
        self, content: bytes, scrape_result: ScrapeResult, suffix: str
    ) -> str:
        with tempfile.NamedTemporaryFile(
            delete=False, suffix=suffix or ".tmp"
        ) as tmp_file:
            tmp_file.write(content)
            temp_file_path = tmp_file.name
        self._temp_files.append(temp_file_path)
        logger.debug(
            f"{self.name}: Stored binary content from {scrape_result.final_url} to temp file: {temp_file_path}"
        )
        return temp_file_path

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
                removed = self._remove_temp_file(f_path)
            except OSError as e:
                logger.error(
                    f"{self.name}: Error removing temporary file {f_path}: {e}"
                )
                continue
            if removed:
                logger.debug(f"{self.name}: Removed temporary file: {f_path}")
                cleaned_count += 1
            else:
                logger.warning(
                    f"{self.name}: Temporary file not found for cleanup: {f_path}"
                )

        logger.info(
            f"{self.name}: Cleaned up {cleaned_count}/{len(self._temp_files)} temporary files."
        )
        self._temp_files.clear()

    @staticmethod
    def _remove_temp_file(file_path: str) -> bool:
        if not os.path.exists(file_path):
            return False
        os.remove(file_path)
        return True

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
